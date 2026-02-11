#!/usr/bin/env python3
"""
Spraay TAO — CLI for batch payments on the Bittensor network.

Usage:
    spraay-tao transfer --wallet <name> --file <path> [--network <net>] [--dry-run]
    spraay-tao estimate --wallet <name> --file <path> [--network <net>]
    spraay-tao validate --file <path>
    spraay-tao generate-template --output <path> [--format csv|json] [--count <n>]

Examples:
    # Batch transfer TAO to recipients from a CSV file (testnet)
    spraay-tao transfer --wallet my_wallet --file recipients.csv --network test

    # Estimate fees before executing
    spraay-tao estimate --wallet my_wallet --file recipients.csv

    # Validate a recipient list without transferring
    spraay-tao validate --file recipients.csv

    # Generate a template CSV file
    spraay-tao generate-template --output recipients.csv --count 5
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from spraay_tao import __version__
from spraay_tao.batch import (
    BatchMode,
    BatchResult,
    Recipient,
    SPRAAY_FEE_PERCENT,
    batch_transfer,
    calculate_spraay_fee,
    estimate_fee,
    parse_recipients,
    validate_recipients,
)


BANNER = r"""
   ___                           _____  _    ___
  / __|_ __ _ _ __ _ __ _ _  _ |_   _|/_\  / _ \
  \__ \ '_ \ '_/ _` / _` | || |  | | / _ \| (_) |
  |___/ .__/_| \__,_\__,_|\_, |  |_|/_/ \_\\___/
      |_|                 |__/
  Batch Payments for Bittensor — by Spraay
"""


def cmd_transfer(args: argparse.Namespace) -> int:
    """Execute batch transfer."""
    print(BANNER)

    # Parse recipients
    try:
        recipients = parse_recipients(args.file)
    except Exception as e:
        print(f"Error parsing file: {e}")
        return 1

    print(f"Loaded {len(recipients)} recipients from {args.file}")
    print(f"Network: {args.network}")
    print(f"Wallet: {args.wallet}")
    print(f"Mode: {'atomic (batch_all)' if args.atomic else 'best-effort (batch)'}")
    print()

    # Validate
    is_valid, errors = validate_recipients(recipients)
    if not is_valid:
        print("Validation errors:")
        for err in errors:
            print(f"  ✗ {err}")
        return 1

    total = sum(r.amount for r in recipients)
    spraay_fee = calculate_spraay_fee(recipients)
    fee_display = f" + {spraay_fee.amount:.6f} TAO Spraay fee ({SPRAAY_FEE_PERCENT}%)" if spraay_fee else ""
    print(f"Total to transfer: {total:.4f} TAO across {len(recipients)} recipients{fee_display}")

    # Dry run
    if args.dry_run:
        print("\n[DRY RUN] Estimating fees without executing...")
        try:
            fee_est = estimate_fee(
                wallet_name=args.wallet,
                recipients=recipients,
                network=args.network,
            )
            print()
            print(fee_est.summary())
        except Exception as e:
            print(f"Fee estimation error: {e}")
            return 1
        return 0

    # Confirm
    if not args.yes:
        response = input(f"\nProceed with transfer of {total:.4f} TAO? [y/N]: ")
        if response.lower() not in ("y", "yes"):
            print("Aborted.")
            return 0

    # Execute
    mode = BatchMode.BATCH_ALL if args.atomic else BatchMode.BATCH

    print("\nExecuting batch transfer...")
    results = batch_transfer(
        wallet_name=args.wallet,
        recipients=recipients,
        network=args.network,
        keep_alive=not args.allow_death,
        mode=mode,
        wait_for_inclusion=True,
        wait_for_finalization=args.finalize,
    )

    # Print results
    print()
    all_success = True
    for result in results:
        print(result.summary())
        print()
        if not result.success:
            all_success = False

    if all_success:
        total_transferred = sum(r.total_amount for r in results)
        total_fees = sum(r.total_fee for r in results)
        total_spraay = sum(r.spraay_fee for r in results)
        print(f"All batches completed successfully!")
        print(f"Total transferred: {total_transferred:.4f} TAO")
        print(f"Total network fees: {total_fees:.6f} TAO")
        if total_spraay > 0:
            print(f"Total Spraay fees: {total_spraay:.6f} TAO")
    else:
        failed = sum(1 for r in results if not r.success)
        print(f"WARNING: {failed}/{len(results)} batches failed!")

    return 0 if all_success else 1


def cmd_estimate(args: argparse.Namespace) -> int:
    """Estimate fees for a batch transfer."""
    print(BANNER)

    try:
        recipients = parse_recipients(args.file)
    except Exception as e:
        print(f"Error parsing file: {e}")
        return 1

    print(f"Estimating fees for {len(recipients)} recipients...")

    try:
        fee_est = estimate_fee(
            wallet_name=args.wallet,
            recipients=recipients,
            network=args.network,
        )
        print()
        print(fee_est.summary())
    except Exception as e:
        print(f"Error: {e}")
        return 1

    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """Validate a recipient list."""
    print(BANNER)

    try:
        recipients = parse_recipients(args.file)
    except Exception as e:
        print(f"Error parsing file: {e}")
        return 1

    print(f"Loaded {len(recipients)} recipients from {args.file}")

    is_valid, errors = validate_recipients(recipients)

    if is_valid:
        total = sum(r.amount for r in recipients)
        print(f"\n✓ All {len(recipients)} recipients are valid")
        print(f"  Total amount: {total:.4f} TAO")
        print(f"  Average per recipient: {total / len(recipients):.4f} TAO")
        print(f"  Min: {min(r.amount for r in recipients):.4f} TAO")
        print(f"  Max: {max(r.amount for r in recipients):.4f} TAO")

        # Show preview
        print(f"\nPreview (first 5):")
        for r in recipients[:5]:
            label = f" ({r.label})" if r.label else ""
            print(f"  {r.address[:16]}...{r.address[-8:]} → {r.amount:.4f} TAO{label}")
        if len(recipients) > 5:
            print(f"  ... and {len(recipients) - 5} more")

        return 0
    else:
        print(f"\n✗ Found {len(errors)} validation errors:")
        for err in errors:
            print(f"  ✗ {err}")
        return 1


def cmd_generate_template(args: argparse.Namespace) -> int:
    """Generate a template recipient file."""
    print(BANNER)

    count = args.count
    output = Path(args.output)

    # Sample addresses (these are well-known test addresses from Substrate)
    sample_addresses = [
        "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",  # Alice
        "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty",  # Bob
        "5FLSigC9HGRKVhB9FiEo4Y3koPsNmBmLJbpXg2mp1hXcS59Y",  # Charlie
        "5DAAnrj7VHTznn2AWBemMuyBwZWs6FNFjdyVXUeYum3PTXFy",  # Dave
        "5HGjWAeFDfFCWPsjFQdVV2Msvz2XtMktvgocEZcCj68kUMaw",  # Eve
    ]

    recipients = []
    labels = ["Alice", "Bob", "Charlie", "Dave", "Eve", "Miner_1", "Miner_2",
              "Validator_1", "Contributor_1", "Bounty_Winner"]
    for i in range(count):
        addr = sample_addresses[i % len(sample_addresses)]
        label = labels[i] if i < len(labels) else f"Recipient_{i + 1}"
        recipients.append({
            "address": addr,
            "amount": round(1.0 + (i * 0.5), 2),
            "label": label,
        })

    fmt = args.format
    if fmt == "json":
        with open(output, "w") as f:
            json.dump(recipients, f, indent=2)
    else:
        with open(output, "w", newline="") as f:
            f.write("address,amount,label\n")
            for r in recipients:
                f.write(f"{r['address']},{r['amount']},{r['label']}\n")

    print(f"Generated template with {count} recipients: {output}")
    print(f"Format: {fmt.upper()}")
    print(f"\nEdit the file with your actual recipient addresses and amounts,")
    print(f"then run: spraay-tao validate --file {output}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="spraay-tao",
        description="Spraay TAO — Batch payments for the Bittensor ecosystem",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Built by Spraay (spraay.app) | GitHub: plagtech",
    )
    parser.add_argument(
        "--version", action="version", version=f"spraay-tao {__version__}"
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Transfer command
    transfer_parser = subparsers.add_parser(
        "transfer", help="Execute batch TAO transfers"
    )
    transfer_parser.add_argument(
        "--wallet", "-w", required=True, help="Bittensor wallet name"
    )
    transfer_parser.add_argument(
        "--file", "-f", required=True, help="Path to recipient list (CSV or JSON)"
    )
    transfer_parser.add_argument(
        "--network", "-n", default="finney",
        help="Bittensor network (finney, test, local). Default: finney"
    )
    transfer_parser.add_argument(
        "--dry-run", action="store_true",
        help="Estimate fees without executing transfers"
    )
    transfer_parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip confirmation prompt"
    )
    transfer_parser.add_argument(
        "--atomic", action="store_true", default=True,
        help="Use batch_all (atomic — all succeed or all revert). Default: True"
    )
    transfer_parser.add_argument(
        "--best-effort", action="store_false", dest="atomic",
        help="Use batch (best-effort — individual failures don't revert others)"
    )
    transfer_parser.add_argument(
        "--allow-death", action="store_true",
        help="Allow transfers that may reduce accounts below existential deposit"
    )
    transfer_parser.add_argument(
        "--finalize", action="store_true",
        help="Wait for transaction finalization (slower but more certain)"
    )

    # Estimate command
    estimate_parser = subparsers.add_parser(
        "estimate", help="Estimate batch transfer fees"
    )
    estimate_parser.add_argument(
        "--wallet", "-w", required=True, help="Bittensor wallet name"
    )
    estimate_parser.add_argument(
        "--file", "-f", required=True, help="Path to recipient list"
    )
    estimate_parser.add_argument(
        "--network", "-n", default="finney", help="Bittensor network"
    )

    # Validate command
    validate_parser = subparsers.add_parser(
        "validate", help="Validate a recipient list"
    )
    validate_parser.add_argument(
        "--file", "-f", required=True, help="Path to recipient list"
    )

    # Generate template command
    template_parser = subparsers.add_parser(
        "generate-template", help="Generate a template recipient file"
    )
    template_parser.add_argument(
        "--output", "-o", default="recipients.csv", help="Output file path"
    )
    template_parser.add_argument(
        "--format", choices=["csv", "json"], default="csv", help="File format"
    )
    template_parser.add_argument(
        "--count", "-c", type=int, default=5, help="Number of sample recipients"
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 0

    commands = {
        "transfer": cmd_transfer,
        "estimate": cmd_estimate,
        "validate": cmd_validate,
        "generate-template": cmd_generate_template,
    }

    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
