"""
Core batch transfer logic for Spraay TAO.

Uses Substrate's utility.batch_all pallet to bundle multiple TAO transfers
into a single atomic transaction. If any transfer fails, the entire batch
is reverted — ensuring no partial payments.

Supports:
- Batch TAO transfers to multiple ss58 recipients
- Dry-run mode with fee estimation
- CSV/JSON recipient list parsing
- Configurable batch sizes for large recipient lists
- Both sync and async execution
"""

from __future__ import annotations

import asyncio
import csv
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

import bittensor as bt
from bittensor.core.extrinsics.pallets import Balances
from bittensor.utils import is_valid_bittensor_address_or_public_key
from bittensor.utils.balance import Balance


# Maximum recipients per batch transaction.
# Substrate utility.batch has no hard limit, but larger batches
# consume more weight. 199 to leave room for the fee transfer.
MAX_BATCH_SIZE = 199

# Minimum transfer amount in TAO (existential deposit protection)
MIN_TRANSFER_TAO = 0.0005  # 500,000 RAO

# ── Spraay Service Fee ──────────────────────────────────────────
# A small transparent fee is appended as an additional transfer
# within each batch. Shown upfront in fee estimates.
# Set to 0.0 to disable (e.g. for grant-funded deployments).
SPRAAY_FEE_PERCENT = 0.3  # 0.3% of total batch amount
SPRAAY_FEE_WALLET = "5CZjqeHFjmj39KuXanRApouyKFXokjazeor6h3bPoCzuzmJC"
SPRAAY_MIN_FEE_TAO = 0.001  # Minimum fee per batch (below this, no fee charged)


class BatchMode(Enum):
    """Batch execution modes."""

    BATCH_ALL = "batch_all"  # Atomic — all succeed or all revert
    BATCH = "batch"  # Best-effort — failures don't revert others


@dataclass
class Recipient:
    """A single payment recipient."""

    address: str
    amount: float  # in TAO
    label: str = ""  # optional label/note

    def validate(self) -> list[str]:
        """Validate this recipient. Returns list of error strings."""
        errors = []
        if not is_valid_bittensor_address_or_public_key(self.address):
            errors.append(f"Invalid ss58 address: {self.address}")
        if self.amount <= 0:
            errors.append(f"Amount must be positive, got {self.amount}")
        if self.amount < MIN_TRANSFER_TAO:
            errors.append(
                f"Amount {self.amount} TAO below minimum {MIN_TRANSFER_TAO} TAO"
            )
        return errors

    @property
    def amount_rao(self) -> int:
        """Amount in RAO (1 TAO = 1e9 RAO)."""
        return Balance.from_tao(self.amount).rao


@dataclass
class BatchResult:
    """Result of a batch transfer operation."""

    success: bool
    message: str
    block_hash: Optional[str] = None
    extrinsic_hash: Optional[str] = None
    total_amount: float = 0.0
    total_fee: float = 0.0  # network fee
    spraay_fee: float = 0.0  # Spraay service fee
    recipient_count: int = 0
    duration_seconds: float = 0.0
    failed_recipients: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """Human-readable summary of the batch result."""
        status = "SUCCESS" if self.success else "FAILED"
        lines = [
            f"=== Spraay TAO Batch Transfer — {status} ===",
            f"Recipients: {self.recipient_count}",
            f"Total amount: {self.total_amount:.4f} TAO",
            f"Network fee: {self.total_fee:.6f} TAO",
        ]
        if self.spraay_fee > 0:
            lines.append(f"Spraay fee: {self.spraay_fee:.6f} TAO")
        lines.append(f"Duration: {self.duration_seconds:.1f}s")
        if self.block_hash:
            lines.append(f"Block hash: {self.block_hash}")
        if self.extrinsic_hash:
            lines.append(f"Extrinsic hash: {self.extrinsic_hash}")
        if not self.success:
            lines.append(f"Error: {self.message}")
        if self.failed_recipients:
            lines.append(f"Failed recipients: {', '.join(self.failed_recipients)}")
        return "\n".join(lines)


@dataclass
class FeeEstimate:
    """Fee estimate for a batch transfer."""

    estimated_fee: float  # network fee in TAO
    spraay_fee: float  # Spraay service fee in TAO
    total_amount: float  # in TAO (to recipients)
    total_cost: float  # amount + network fee + spraay fee
    recipient_count: int
    batch_count: int  # number of batch transactions needed
    balance_sufficient: bool
    current_balance: float

    def summary(self) -> str:
        """Human-readable fee estimate."""
        status = "SUFFICIENT" if self.balance_sufficient else "INSUFFICIENT"
        lines = [
            "=== Spraay TAO — Fee Estimate ===",
            f"Recipients: {self.recipient_count}",
            f"Batch transactions needed: {self.batch_count}",
            f"Total transfer amount: {self.total_amount:.4f} TAO",
            f"Network fee (est.): {self.estimated_fee:.6f} TAO",
        ]
        if self.spraay_fee > 0:
            lines.append(
                f"Spraay service fee ({SPRAAY_FEE_PERCENT}%): {self.spraay_fee:.6f} TAO"
            )
        lines.extend([
            f"Total cost: {self.total_cost:.6f} TAO",
            f"Current balance: {self.current_balance:.4f} TAO",
            f"Balance: {status}",
        ])
        return "\n".join(lines)


def parse_recipients_csv(filepath: str | Path) -> list[Recipient]:
    """
    Parse a CSV file of recipients.

    Expected format:
        address,amount[,label]
        5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty,10.5,Alice
        5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY,5.0,Bob
    """
    recipients = []
    filepath = Path(filepath)

    with open(filepath, "r", newline="") as f:
        reader = csv.DictReader(f)

        # Normalize header names
        if reader.fieldnames is None:
            raise ValueError("CSV file is empty or has no headers")

        headers = [h.strip().lower() for h in reader.fieldnames]

        for row_num, row in enumerate(reader, start=2):
            # Normalize keys
            normalized = {k.strip().lower(): v.strip() for k, v in row.items()}

            address = normalized.get("address", "")
            amount_str = normalized.get("amount", "0")
            label = normalized.get("label", normalized.get("name", ""))

            if not address:
                raise ValueError(f"Row {row_num}: missing address")

            try:
                amount = float(amount_str)
            except ValueError:
                raise ValueError(
                    f"Row {row_num}: invalid amount '{amount_str}'"
                )

            recipients.append(Recipient(
                address=address,
                amount=amount,
                label=label,
            ))

    return recipients


def parse_recipients_json(filepath: str | Path) -> list[Recipient]:
    """
    Parse a JSON file of recipients.

    Expected format:
        [
            {"address": "5FHne...", "amount": 10.5, "label": "Alice"},
            {"address": "5Grwv...", "amount": 5.0}
        ]
    """
    filepath = Path(filepath)
    with open(filepath, "r") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError("JSON must contain a list of recipient objects")

    recipients = []
    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise ValueError(f"Entry {i}: must be an object")
        if "address" not in entry:
            raise ValueError(f"Entry {i}: missing 'address' field")
        if "amount" not in entry:
            raise ValueError(f"Entry {i}: missing 'amount' field")

        recipients.append(Recipient(
            address=str(entry["address"]),
            amount=float(entry["amount"]),
            label=str(entry.get("label", "")),
        ))

    return recipients


def parse_recipients(filepath: str | Path) -> list[Recipient]:
    """Auto-detect file format and parse recipients."""
    filepath = Path(filepath)
    suffix = filepath.suffix.lower()
    if suffix == ".json":
        return parse_recipients_json(filepath)
    elif suffix in (".csv", ".tsv", ".txt"):
        return parse_recipients_csv(filepath)
    else:
        # Try CSV first, then JSON
        try:
            return parse_recipients_csv(filepath)
        except Exception:
            return parse_recipients_json(filepath)


def validate_recipients(recipients: list[Recipient]) -> tuple[bool, list[str]]:
    """
    Validate all recipients. Returns (is_valid, list_of_errors).
    Also checks for duplicate addresses.
    """
    errors = []
    seen_addresses = {}

    for i, r in enumerate(recipients):
        r_errors = r.validate()
        for err in r_errors:
            errors.append(f"Recipient {i + 1} ({r.label or r.address[:12]}...): {err}")

        # Check duplicates
        if r.address in seen_addresses:
            prev = seen_addresses[r.address]
            errors.append(
                f"Duplicate address at positions {prev + 1} and {i + 1}: {r.address[:16]}..."
            )
        seen_addresses[r.address] = i

    return len(errors) == 0, errors


def chunk_recipients(
    recipients: list[Recipient], max_size: int = MAX_BATCH_SIZE
) -> list[list[Recipient]]:
    """Split recipients into chunks for batch processing."""
    return [
        recipients[i: i + max_size]
        for i in range(0, len(recipients), max_size)
    ]


def calculate_spraay_fee(recipients: list[Recipient]) -> Optional[Recipient]:
    """
    Calculate the Spraay service fee for a batch of recipients.

    Returns a Recipient representing the fee transfer to the Spraay wallet,
    or None if fees are disabled or below the minimum threshold.

    The fee is transparent and included in fee estimates shown to the user
    before they confirm any transaction.
    """
    if not SPRAAY_FEE_WALLET or SPRAAY_FEE_PERCENT <= 0:
        return None

    total_amount = sum(r.amount for r in recipients)
    fee_amount = total_amount * (SPRAAY_FEE_PERCENT / 100.0)

    if fee_amount < SPRAAY_MIN_FEE_TAO:
        return None

    return Recipient(
        address=SPRAAY_FEE_WALLET,
        amount=round(fee_amount, 9),  # TAO has 9 decimal places (RAO)
        label="Spraay service fee",
    )


def _build_batch_call(
    subtensor: bt.Subtensor,
    recipients: list[Recipient],
    keep_alive: bool = True,
    mode: BatchMode = BatchMode.BATCH_ALL,
    include_fee: bool = True,
) -> "GenericCall":
    """
    Build a utility.batch_all or utility.batch call containing
    multiple balance transfers, plus the Spraay service fee transfer.
    """
    balances = Balances(subtensor)

    # Build individual transfer calls
    transfer_fn = "transfer_keep_alive" if keep_alive else "transfer_allow_death"

    # Start with user recipients
    all_recipients = list(recipients)

    # Append Spraay fee as an additional transfer
    if include_fee:
        fee_recipient = calculate_spraay_fee(recipients)
        if fee_recipient:
            all_recipients.append(fee_recipient)

    calls = []
    for r in all_recipients:
        call = getattr(balances, transfer_fn)(
            dest=r.address,
            value=r.amount_rao,
        )
        calls.append(call)

    # Wrap in utility.batch_all (atomic) or utility.batch (best-effort)
    batch_call = subtensor.compose_call(
        call_module="Utility",
        call_function=mode.value,
        call_params={"calls": calls},
    )

    return batch_call


def estimate_fee(
    wallet_name: str,
    recipients: list[Recipient],
    network: str = "finney",
    keep_alive: bool = True,
) -> FeeEstimate:
    """
    Estimate the fee for a batch transfer without executing it.
    """
    subtensor = bt.Subtensor(network=network)
    wallet = bt.Wallet(name=wallet_name)

    total_amount = sum(r.amount for r in recipients)
    chunks = chunk_recipients(recipients)

    # Calculate Spraay service fee across all chunks
    total_spraay_fee = 0.0
    for chunk in chunks:
        fee_recipient = calculate_spraay_fee(chunk)
        if fee_recipient:
            total_spraay_fee += fee_recipient.amount

    # Estimate network fee for the first chunk (representative)
    sample_call = _build_batch_call(subtensor, chunks[0], keep_alive)

    # Get fee estimate using the substrate interface
    fee_info = subtensor.substrate.get_payment_info(
        call=sample_call,
        keypair=wallet.coldkey,
    )
    fee_per_batch = Balance.from_rao(fee_info["partial_fee"]).tao if fee_info else 0.001
    total_network_fee = fee_per_batch * len(chunks)

    current_balance = subtensor.get_balance(wallet.coldkeypub.ss58_address).tao
    total_cost = total_amount + total_network_fee + total_spraay_fee

    return FeeEstimate(
        estimated_fee=total_network_fee,
        spraay_fee=total_spraay_fee,
        total_amount=total_amount,
        total_cost=total_cost,
        recipient_count=len(recipients),
        batch_count=len(chunks),
        balance_sufficient=current_balance >= total_cost,
        current_balance=current_balance,
    )


def batch_transfer(
    wallet_name: str,
    recipients: list[Recipient],
    network: str = "finney",
    keep_alive: bool = True,
    mode: BatchMode = BatchMode.BATCH_ALL,
    wait_for_inclusion: bool = True,
    wait_for_finalization: bool = False,
) -> list[BatchResult]:
    """
    Execute batch TAO transfers.

    Splits recipients into chunks of MAX_BATCH_SIZE and submits each
    as a separate utility.batch_all transaction. Each batch is atomic —
    all transfers in the batch succeed or all revert.

    Parameters:
        wallet_name: Name of the Bittensor wallet to use (must be unlocked).
        recipients: List of Recipient objects with addresses and amounts.
        network: Bittensor network ('finney' for mainnet, 'test' for testnet).
        keep_alive: If True, use transfer_keep_alive to protect existential deposits.
        mode: BATCH_ALL (atomic) or BATCH (best-effort).
        wait_for_inclusion: Wait for the transaction to be included in a block.
        wait_for_finalization: Wait for the transaction to be finalized.

    Returns:
        List of BatchResult objects, one per batch chunk.
    """
    # Validate recipients first
    is_valid, errors = validate_recipients(recipients)
    if not is_valid:
        return [BatchResult(
            success=False,
            message=f"Validation failed with {len(errors)} errors:\n" + "\n".join(errors),
            recipient_count=len(recipients),
        )]

    subtensor = bt.Subtensor(network=network)
    wallet = bt.Wallet(name=wallet_name)
    wallet.unlock_coldkey()

    total_amount = sum(r.amount for r in recipients)

    # Calculate total Spraay fee across all chunks
    chunks = chunk_recipients(recipients)
    total_spraay_fee = 0.0
    for chunk in chunks:
        fee_r = calculate_spraay_fee(chunk)
        if fee_r:
            total_spraay_fee += fee_r.amount

    # Check balance (must cover transfers + spraay fee + network fees)
    balance = subtensor.get_balance(wallet.coldkeypub.ss58_address)
    required = total_amount + total_spraay_fee
    if balance.tao < required:
        return [BatchResult(
            success=False,
            message=(
                f"Insufficient balance: {balance.tao:.4f} TAO available, "
                f"but {required:.4f} TAO needed "
                f"({total_amount:.4f} transfers + {total_spraay_fee:.6f} Spraay fee)."
            ),
            total_amount=total_amount,
            spraay_fee=total_spraay_fee,
            recipient_count=len(recipients),
        )]

    # Split into chunks
    results = []

    for chunk_idx, chunk in enumerate(chunks):
        start_time = time.time()
        chunk_amount = sum(r.amount for r in chunk)
        chunk_spraay_fee = 0.0
        fee_r = calculate_spraay_fee(chunk)
        if fee_r:
            chunk_spraay_fee = fee_r.amount

        try:
            # Build the batch call
            batch_call = _build_batch_call(subtensor, chunk, keep_alive, mode)

            # Sign and submit
            response = subtensor.sign_and_send_extrinsic(
                call=batch_call,
                wallet=wallet,
                wait_for_inclusion=wait_for_inclusion,
                wait_for_finalization=wait_for_finalization,
            )

            duration = time.time() - start_time

            if response.success:
                block_hash = subtensor.get_block_hash()
                results.append(BatchResult(
                    success=True,
                    message=f"Batch {chunk_idx + 1}/{len(chunks)} completed successfully",
                    block_hash=block_hash,
                    extrinsic_hash=getattr(response, "extrinsic_hash", None),
                    total_amount=chunk_amount,
                    total_fee=getattr(response, "transaction_tao_fee", 0),
                    spraay_fee=chunk_spraay_fee,
                    recipient_count=len(chunk),
                    duration_seconds=duration,
                ))
            else:
                results.append(BatchResult(
                    success=False,
                    message=f"Batch {chunk_idx + 1}/{len(chunks)} failed: {response.message}",
                    total_amount=chunk_amount,
                    spraay_fee=chunk_spraay_fee,
                    recipient_count=len(chunk),
                    duration_seconds=duration,
                ))

        except Exception as e:
            duration = time.time() - start_time
            results.append(BatchResult(
                success=False,
                message=f"Batch {chunk_idx + 1}/{len(chunks)} exception: {str(e)}",
                total_amount=chunk_amount,
                spraay_fee=chunk_spraay_fee,
                recipient_count=len(chunk),
                duration_seconds=duration,
            ))

    return results


async def async_batch_transfer(
    wallet_name: str,
    recipients: list[Recipient],
    network: str = "finney",
    keep_alive: bool = True,
    mode: BatchMode = BatchMode.BATCH_ALL,
    wait_for_inclusion: bool = True,
    wait_for_finalization: bool = False,
) -> list[BatchResult]:
    """
    Async version of batch_transfer.

    Same functionality as batch_transfer but uses AsyncSubtensor
    for non-blocking execution. Preferred for web server integrations.
    """
    # Validate recipients
    is_valid, errors = validate_recipients(recipients)
    if not is_valid:
        return [BatchResult(
            success=False,
            message=f"Validation failed with {len(errors)} errors:\n" + "\n".join(errors),
            recipient_count=len(recipients),
        )]

    async with bt.AsyncSubtensor(network=network) as subtensor:
        wallet = bt.Wallet(name=wallet_name)
        wallet.unlock_coldkey()

        total_amount = sum(r.amount for r in recipients)

        # Check balance
        balance = await subtensor.get_balance(wallet.coldkeypub.ss58_address)
        if balance.tao < total_amount:
            return [BatchResult(
                success=False,
                message=(
                    f"Insufficient balance: {balance.tao:.4f} TAO available, "
                    f"but {total_amount:.4f} TAO needed."
                ),
                total_amount=total_amount,
                recipient_count=len(recipients),
            )]

        chunks = chunk_recipients(recipients)
        results = []

        for chunk_idx, chunk in enumerate(chunks):
            start_time = time.time()
            chunk_amount = sum(r.amount for r in chunk)

            try:
                balances_pallet = Balances(subtensor)
                transfer_fn = "transfer_keep_alive" if keep_alive else "transfer_allow_death"

                calls = []
                for r in chunk:
                    call = await getattr(balances_pallet, transfer_fn)(
                        dest=r.address,
                        value=r.amount_rao,
                    )
                    calls.append(call)

                batch_call = await subtensor.compose_call(
                    call_module="Utility",
                    call_function=mode.value,
                    call_params={"calls": calls},
                )

                response = await subtensor.sign_and_send_extrinsic(
                    call=batch_call,
                    wallet=wallet,
                    wait_for_inclusion=wait_for_inclusion,
                    wait_for_finalization=wait_for_finalization,
                )

                duration = time.time() - start_time

                if response.success:
                    block_hash = await subtensor.get_block_hash()
                    results.append(BatchResult(
                        success=True,
                        message=f"Batch {chunk_idx + 1}/{len(chunks)} completed",
                        block_hash=block_hash,
                        total_amount=chunk_amount,
                        recipient_count=len(chunk),
                        duration_seconds=duration,
                    ))
                else:
                    results.append(BatchResult(
                        success=False,
                        message=f"Batch {chunk_idx + 1}/{len(chunks)} failed: {response.message}",
                        total_amount=chunk_amount,
                        recipient_count=len(chunk),
                        duration_seconds=duration,
                    ))

            except Exception as e:
                duration = time.time() - start_time
                results.append(BatchResult(
                    success=False,
                    message=f"Batch {chunk_idx + 1}/{len(chunks)} exception: {str(e)}",
                    total_amount=chunk_amount,
                    recipient_count=len(chunk),
                    duration_seconds=duration,
                ))

    return results
