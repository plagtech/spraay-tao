# Spraay TAO

**Batch payments for the Bittensor ecosystem.**

Spraay TAO lets subnet operators, DAOs, and teams distribute TAO to hundreds of recipients in a single atomic transaction using Substrate's `utility.batch_all` pallet.

> ✅ **Testnet validated** — Live batch transfer executed on Bittensor testnet. [See proof below.](#testnet-proof)

## Why Spraay TAO?

Bittensor has 128+ active subnets emitting millions in TAO rewards daily, but **no native batch payment tool exists**. Subnet operators currently send transfers one by one using `btcli` or manual scripts. Spraay TAO fixes this.

**One transaction. All recipients. Atomic.**

| Feature | Details |
|---------|---------|
| Batch transfers | Up to 199 recipients per transaction |
| Atomic execution | All succeed or all revert (`batch_all`) |
| Auto-chunking | 200+ recipients split automatically |
| Fee estimation | Preview costs before confirming |
| CSV/JSON input | Flexible recipient list formats |
| Dry-run mode | Validate without executing |
| CLI + Python API | Use from terminal or import in scripts |

## Quick Start

### Install

```bash
git clone https://github.com/plagtech/spraay-tao.git
cd spraay-tao
pip install -e .
```

### Create a recipient list

```bash
spraay-tao generate-template --format csv
```

This creates `recipients_template.csv`:

```csv
address,amount,label
5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty,1.5,Alice
5FLSigC9HGRKVhB9FiEo4Y3koPsNmBmLJbpXg2mp1hXcS59Y,2.0,Bob
5DAAnrj7VHTznn2AWBemMuyBwZWs6FNFjdyVXUeYum3PTXFy,0.75,Charlie
```

### Validate

```bash
spraay-tao validate --file recipients.csv
```

### Estimate fees

```bash
spraay-tao estimate --wallet my_wallet --file recipients.csv --network finney
```

### Transfer

```bash
spraay-tao transfer --wallet my_wallet --file recipients.csv --network finney
```

Add `--dry-run` to simulate without executing. Add `--network test` for testnet.

## How It Works

```
┌──────────────────────────────────────────────────┐
│              Spraay TAO CLI / API                 │
│                                                  │
│  1. Parse recipients from CSV/JSON               │
│  2. Validate addresses & amounts                 │
│  3. Chunk into batches of 199                    │
│  4. Build utility.batch_all call                 │
│     ├── Balances.transfer_keep_alive (recipient) │
│     ├── Balances.transfer_keep_alive (recipient) │
│     ├── ...                                      │
│     └── Balances.transfer_keep_alive (fee)       │
│  5. Sign with wallet coldkey                     │
│  6. Submit to Subtensor                          │
│  7. Wait for finalization                        │
└──────────────────────────────────────────────────┘
```

All transfers in a `batch_all` call are **atomic** — if any single transfer fails (e.g., insufficient balance), the entire batch reverts. No partial payments.

## Python API

```python
from spraay_tao.batch import (
    Recipient,
    parse_recipients,
    validate_recipients,
    batch_transfer,
    estimate_fee,
)

# Load recipients
recipients = parse_recipients("recipients.csv")

# Validate
errors = validate_recipients(recipients)
if errors:
    print(f"Errors: {errors}")

# Estimate fees
estimate = estimate_fee("my_wallet", recipients, network="finney")
print(estimate.summary())

# Execute
results = batch_transfer("my_wallet", recipients, network="finney")
for r in results:
    print(r.summary())
```

## Service Fee

Spraay TAO includes a transparent 0.3% service fee on each batch. The fee is:

- Appended as an additional transfer within the same `batch_all` call
- Shown upfront in fee estimates before you confirm
- Skipped entirely for batches where the fee would be below 0.001 TAO
- Visible in the source code — no hidden charges

Network fees on Bittensor are negligible (~$0.01 per 200-recipient batch).

## Use Cases

- **Subnet operators** — Distribute mining/validation rewards to participants
- **DAOs** — Execute grant payments to multiple recipients
- **Hackathon organizers** — Pay all prize winners at once
- **Community managers** — Airdrop TAO to contributors
- **Bounty programs** — Settle multiple bounties in one transaction
- **Payroll** — Recurring team payments via scheduled scripts

## Testnet Proof

First batch transfer executed on Bittensor testnet:

```
Network:    Bittensor Testnet (test.finney.opentensor.ai)
SDK:        bittensor v10.1.0
Tx Type:    Utility.batch_all -> 3x Balances.transfer_keep_alive
Recipients: 3 (0.1 + 0.15 + 0.05 TAO)
Total:      0.3 TAO
Net Fee:    0.000033714 TAO
Result:     SUCCESS
Duration:   32.3s (incl. finalization)
Extrinsic:  0xfbe43c26da4f30e1fdd55c68a5db342d776f1a222d2d75277f1fe04e7f3a6308
```

## CLI Reference

| Command | Description |
|---------|-------------|
| `spraay-tao transfer` | Execute batch TAO transfers |
| `spraay-tao estimate` | Estimate fees without executing |
| `spraay-tao validate` | Validate a recipient list |
| `spraay-tao generate-template` | Generate sample CSV/JSON |

### Transfer options

| Flag | Description |
|------|-------------|
| `--wallet` | Wallet name (default: `default`) |
| `--file` | Path to recipients CSV/JSON |
| `--network` | `finney`, `test`, or `local` (default: `finney`) |
| `--dry-run` | Simulate without executing |
| `--yes` | Skip confirmation prompt |
| `--best-effort` | Use `batch` instead of `batch_all` (partial failures allowed) |
| `--allow-death` | Allow sender balance to go to zero |
| `--finalize` | Wait for block finalization |

## Requirements

- Python 3.9+
- `bittensor >= 10.0.0`
- A Bittensor wallet with TAO balance

## Links

- [Spraay](https://spraay.app) — Multi-chain batch payments
- [Bittensor](https://bittensor.com) — Decentralized AI network
- [TAO.app](https://tao.app) — Subnet explorer

## License

MIT

---

Built by [Spraay](https://spraay.app) 
