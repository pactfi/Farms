# Pact Finance — Farms

A configurable **staking / yield-farming protocol** built on the [Algorand](https://algorand.com) blockchain using [Beaker](https://github.com/algorand-devrel/beaker) (a PyTEAL framework).

Users stake an ASA (Algorand Standard Asset) — typically a Pact LP token — and earn up to **7 reward assets** distributed linearly over a configurable duration.

---

## Table of Contents

- [Architecture](#architecture)
- [Contract Overview](#contract-overview)
- [Global State](#global-state)
- [User (Local) State](#user-local-state)
- [Key Flows](#key-flows)
- [Repository Structure](#repository-structure)
- [Quick Start](#quick-start)
- [Deployment](#deployment)
- [Configuration](#configuration)
- [Security Notes](#security-notes)

---

## Architecture

```
┌──────────────────────────────────────────┐
│               Farm Contract              │
│  (one per staked-asset / reward set)     │
│                                          │
│  Global state: RPT, pending rewards,     │
│  total staked, duration, …               │
└────────────┬─────────────────────────────┘
             │  inner txn: update_state
             ▼
┌──────────────────────────────────────────┐
│        Escrow Contract  (per user)       │
│  Holds the user's staked tokens          │
│  Creator == user's wallet                │
│  Only callable by user or Farm           │
└──────────────────────────────────────────┘
             │  inner txn: withdraw / GS
             ▼
┌──────────────────────────────────────────┐
│           Gas Station Contract           │
│  Boosts opcode budget; funds new         │
│  escrow deployments; one global instance │
└──────────────────────────────────────────┘
```

### Why a per-user Escrow?

Algorand limits how much can be read from another account's local state in a single transaction group. By giving each user a dedicated mini-contract (the "Escrow"), the Farm can inspect the escrow's asset balance and auth-address directly, enabling trustless stake tracking without relying solely on local-state writes.

---

## Contract Overview

| Contract | File | Purpose |
|---|---|---|
| `Farm` | `farm/farm.py` | Main staking contract |
| `Escrow` | `farm/escrow.py` | Per-user asset custody contract |
| `GasStation` | `gas_station.py` | Opcode-budget booster & ALGO fund relay |

### Farm contract methods

| Method | Caller | Description |
|---|---|---|
| `create(gas_station, staked_asset, admin, updater)` | deployer | Deploy + initialize the farm |
| `update()` + `post_update()` | updater | Two-step contract upgrade |
| `opt_in()` | user | User joins the farm; deploys their Escrow |
| `close_out()` | user | User leaves (requires 0 stake & 0 accrued rewards) |
| `clear_state()` | user | Emergency exit; forfeits unclaimed rewards |
| `add_reward_asset(asset)` | admin | Register a new reward token (max 7) |
| `deposit_rewards(reward_ids, duration)` | admin | Fund a new reward period |
| `update_global_state()` | anyone | Advance the RPT accumulators |
| `update_state(escrow, escrow_account, user, staked_asset)` | anyone | Sync a user's accrued rewards |
| `claim_rewards(account, reward_ids)` | anyone | Send accrued rewards to a user |
| `change_admin(new_admin)` | admin | Rotate admin address |
| `change_updater(new_updater)` | updater | Rotate updater address |

### Escrow contract methods

| Method | Caller | Description |
|---|---|---|
| `create(master, gas_station, staked_asset)` | Farm (inner txn) | Initialize escrow, opt-in to staked asset |
| `unstake(asset, amount, master)` | user | Withdraw stake from escrow |
| `withdraw_algos()` | user | Withdraw surplus ALGO from escrow |
| `send_message(account, note)` | user | Send a 0-ALGO payment with a note (e.g. for on-chain messages) |
| delete | user | Close escrow (must be opted-out of Farm first) |

---

## Global State

| Key | Type | Description |
|---|---|---|
| `CONTRACT_NAME` | bytes | `"PACT FARM"` |
| `VERSION` | uint64 | Contract version (XYY format) |
| `Admin` | bytes | Admin address |
| `Updater` | bytes | Updater address |
| `StakedAssetID` | uint64 | ASA ID of the token users stake |
| `TotalStaked` | uint64 | Sum of all users' staked amounts |
| `NumStakers` | uint64 | Number of accounts with non-zero stake |
| `UpdatedAt` | uint64 | UNIX timestamp of last global update |
| `Duration` | uint64 | Seconds remaining in current reward period |
| `NextDuration` | uint64 | Duration of the queued next reward period |
| `RPT` | bytes | Array of 7×uint64 — reward-per-token accumulators |
| `RPT_frac` | bytes | Fractional parts of RPT (128-bit precision) |
| `PendingRewards` | bytes | Array of 7×uint64 — rewards yet to be distributed |
| `NextRewards` | bytes | Array of 7×uint64 — queued next-period rewards |
| `TotalRewards` | bytes | Array of 7×uint64 — cumulative distributed rewards |
| `ClaimedRewards` | bytes | Array of 7×uint64 — cumulative claimed rewards |
| `RewardAssetIDs` | bytes | Packed uint64 array of reward ASA IDs (0 = ALGO) |

---

## User (Local) State

| Key | Type | Description |
|---|---|---|
| `Staked` | uint64 | User's currently staked amount |
| `EscrowID` | uint64 | App ID of user's Escrow contract |
| `RPT` | bytes | Snapshot of global RPT at last update |
| `RPT_frac` | bytes | Snapshot of global RPT_frac at last update |
| `AccruedRewards` | bytes | Rewards ready to claim |
| `ClaimedRewards` | bytes | Cumulative rewards claimed since opt-in |

---

## Key Flows

### 1. Opt-in (staking setup)

```
User wallet
  │
  ├─[1] ApplicationCreateTxn (escrow husk) ──► creates bare Escrow app
  └─[2] ApplicationCallTxn   (Farm.opt_in) ──► Farm upgrades Escrow bytecode
                                                Farm stores EscrowID in local state
```

### 2. Staking

```
User wallet
  │
  └─[1] AssetTransferTxn ──► send staked tokens directly to Escrow address
```
The stake is picked up lazily during the next `update_state` call (escrow balance is read on-chain).

### 3. Updating rewards

```
Anyone
  │
  ├─[1] ApplicationCallTxn (Farm.update_global_state) ──► advance RPT
  └─[2] ApplicationCallTxn (Farm.update_state)        ──► sync user's accrued rewards
```

### 4. Claiming

```
Anyone (typically the user)
  │
  └─[1] ApplicationCallTxn (Farm.claim_rewards, reward_ids=[0,1,...]) ──► sends reward tokens to user
```
Fees for inner transfers must be covered by the caller via fee pooling.

### 5. Unstaking

```
User wallet
  │
  ├─[1] ApplicationCallTxn (update_global_state)
  ├─[2] ApplicationCallTxn (update_state)
  └─[3] ApplicationCallTxn (Escrow.unstake, amount=X) ──► Escrow sends tokens back to user
                                                           Escrow calls Farm.update_state internally
```

### 6. Depositing rewards (admin)

```
Admin wallet
  │
  ├─[1] AssetTransferTxn  ──► reward asset 0 → Farm address
  ├─[2] AssetTransferTxn  ──► reward asset 1 → Farm address  (optional, up to 7)
  ├─[N] ApplicationCallTxn (Farm.update_global_state)
  └─[N+1] ApplicationCallTxn (Farm.deposit_rewards, reward_ids=[0,1,...], duration=<seconds>)
```

---

## Repository Structure

```
pactfi/Farms/
├── farm/
│   ├── __init__.py
│   ├── farm.py            # Main Farm Beaker application
│   ├── escrow.py          # Per-user Escrow contract (PyTEAL)
│   └── rpt_calculator.py  # Fixed-point reward-per-token math
├── helpers/
│   ├── __init__.py
│   ├── abi.py             # ABI encoding/decoding helpers
│   ├── assets.py          # Asset balance helpers
│   ├── common.py          # Addw / Mulw 128-bit arithmetic expressions
│   ├── fixed_point_64.py  # Fixed-point arithmetic utilities
│   ├── state.py           # Cached state variable wrappers
│   ├── transaction.py     # Inner transaction helpers (Send*, validate*)
│   └── validation.py      # Transfer validation subroutines
├── gas_station.py         # GasStation Beaker application
├── deploy_farm.py         # Farm deployment script
└── LICENSE                # GPL-3.0
```

---

## Quick Start

### Prerequisites

| Tool | Version |
|---|---|
| Python | ≥ 3.10 |
| `algosdk` | ≥ 2.x |
| `beaker-pyteal` | ≥ 1.0 |
| `pyteal` | ≥ 0.20 |
| `pytealext` | latest |
| `pactsdk` (pactdk) | latest |
| Algorand node / AlgoKit sandbox | any |

```bash
pip install algosdk beaker-pyteal pyteal pytealext pactsdk
```

Or use [AlgoKit](https://github.com/algorandfoundation/algokit-cli):

```bash
algokit localnet start
```

### Clone and install

```bash
git clone https://github.com/pactfi/Farms.git
cd Farms
pip install -r requirements.txt   # if provided, else install deps above
```

---

## Deployment

See [DEPLOYMENT.md](DEPLOYMENT.md) for the full step-by-step guide.

---

## Configuration

The `deploy_farm.py` script reads a `settings` object. At minimum you need:

| Setting | Description |
|---|---|
| `ALGORAND_ALGOD_URL` | URL of an Algorand algod node |
| `ALGORAND_ALGOD_TOKEN` | algod API token |
| `ALGORAND_MULTISIG_ADMIN_ADDRESS` | Admin wallet address |
| `STAKED_ASSET_ID` | ASA ID of the LP / staking token |

---

## Security Notes

- The **Escrow** contract is owner-only: only the creator address (user's wallet) can call it.
- The Farm validates escrow integrity on every state update via `validate_escrow`, which checks:
  - The escrow's approval program matches the expected bytecode.
  - The escrow was created by the claimed user.
  - The escrow's `MasterAppID` global state equals the Farm's app ID.
  - The Farm's local state for the user matches the escrow's app ID.
- Reward token balances are validated against pending/total/claimed accounting to prevent double-spending.
- The two-step upgrade (`update` + `post_update`) includes a bytecode-equality failsafe — the escrow bytecode **must remain unchanged** across Farm upgrades to preserve compatibility with previously deployed user escrows.
- Fee pooling is used throughout inner transactions; callers must cover fees.
