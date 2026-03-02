# Deployment Guide — Pact Finance Farms

This guide walks you through deploying a Pact Farm from scratch on Algorand (localnet, testnet, or mainnet).

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Environment Setup](#2-environment-setup)
3. [Deploy the Gas Station](#3-deploy-the-gas-station)
4. [Create the Staked Asset (testnet / localnet)](#4-create-the-staked-asset-testnet--localnet)
5. [Deploy the Farm Contract](#5-deploy-the-farm-contract)
6. [Post-Deploy: Add Reward Assets](#6-post-deploy-add-reward-assets)
7. [Deposit Rewards](#7-deposit-rewards)
8. [User Flows (Opt-in & Staking)](#8-user-flows-opt-in--staking)
9. [Claiming Rewards](#9-claiming-rewards)
10. [Unstaking](#10-unstaking)
11. [Upgrading the Farm Contract](#11-upgrading-the-farm-contract)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. Prerequisites

### Software

| Tool | Minimum version | Install |
|---|---|---|
| Python | 3.10 | `brew install python` / apt / pyenv |
| AlgoKit | 2.x | `pip install algokit` |
| algosdk | 2.x | `pip install algosdk` |
| beaker-pyteal | 1.x | `pip install beaker-pyteal` |
| pyteal | 0.20+ | `pip install pyteal` |
| pytealext | latest | `pip install pytealext` |
| pactsdk (pactdk) | latest | `pip install pactsdk` |

```bash
pip install algosdk beaker-pyteal pyteal pytealext pactsdk
```

### Algorand node

For local development use AlgoKit's built-in localnet:

```bash
algokit localnet start
# Algod: http://localhost:4001   Token: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
# KMD:  http://localhost:4002   Token: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
```

For testnet/mainnet use a public node or your own:
- [AlgoNode](https://algonode.io) — free public nodes
- [Nodely](https://nodely.io)

---

## 2. Environment Setup

Create a `settings.py` (or use environment variables) with your configuration:

```python
# settings.py
import os

ALGORAND_ALGOD_URL   = os.getenv("ALGOD_URL",   "http://localhost:4001")
ALGORAND_ALGOD_TOKEN = os.getenv("ALGOD_TOKEN",  "a" * 64)

# Admin wallet — has permission to add reward assets and deposit rewards
ALGORAND_MULTISIG_ADMIN_ADDRESS = os.getenv("ADMIN_ADDRESS", "<your-admin-address>")
ADMIN_PRIVATE_KEY               = os.getenv("ADMIN_PRIVATE_KEY", "<base64-private-key>")

# Updater wallet — has permission to upgrade the contract
UPDATER_ADDRESS     = os.getenv("UPDATER_ADDRESS",     ALGORAND_MULTISIG_ADMIN_ADDRESS)
UPDATER_PRIVATE_KEY = os.getenv("UPDATER_PRIVATE_KEY", ADMIN_PRIVATE_KEY)

# Gas Station app ID (deploy once, reuse across all farms)
GAS_STATION_APP_ID = int(os.getenv("GAS_STATION_APP_ID", "0"))
```

Initialize the algod client:

```python
import algosdk
from algosdk.v2client import algod

client = algod.AlgodClient(
    settings.ALGORAND_ALGOD_TOKEN,
    settings.ALGORAND_ALGOD_URL,
)
```

---

## 3. Deploy the Gas Station

The Gas Station is a shared contract. Deploy it once per environment and reuse its app ID for all farms.

```python
import base64
import algosdk
from beaker import localnet
from gas_station import GasStationContract

def deploy_gas_station(sender: str, signer_pk: str) -> int:
    """Deploy the Gas Station contract and return its app ID."""
    gs = GasStationContract()
    gs.build()  # compile via beaker

    sp = client.suggested_params()

    # Fund the app's address before creating so it can cover MinBalance
    fund_tx = algosdk.transaction.PaymentTxn(
        sender=sender,
        receiver=algosdk.logic.get_application_address(0),  # placeholder
        amt=1_000_000,  # 1 ALGO — adjust as needed
        sp=sp,
    )

    create_tx = algosdk.transaction.ApplicationCreateTxn(
        sender=sender,
        on_complete=algosdk.transaction.OnComplete.NoOpOC,
        approval_program=base64.b64decode(
            client.compile(gs.approval_program)["result"]
        ),
        clear_program=base64.b64decode(
            client.compile(gs.clear_program)["result"]
        ),
        global_schema=algosdk.transaction.StateSchema(0, 0),
        local_schema=algosdk.transaction.StateSchema(0, 0),
        sp=sp,
    )

    signed = create_tx.sign(signer_pk)
    txid = client.send_transaction(signed)
    result = algosdk.transaction.wait_for_confirmation(client, txid, 4)
    app_id = result["application-index"]
    print(f"Gas Station deployed: app_id={app_id}")
    return app_id
```

> **Note**: On Pact's production deployment the Gas Station is already live. Check Pact's official docs or the `pactsdk` library for the canonical app ID on mainnet/testnet.

---

## 4. Create the Staked Asset (testnet / localnet)

Skip this step if you already have a staked asset ID (e.g., an existing Pact LP token).

```python
def create_test_asset(sender: str, signer_pk: str, name: str = "FARM-LP", total: int = 10**15) -> int:
    """Create a test ASA to use as the staked token."""
    sp = client.suggested_params()
    txn = algosdk.transaction.AssetConfigTxn(
        sender=sender,
        sp=sp,
        total=total,
        default_frozen=False,
        decimals=6,
        unit_name=name[:8],
        asset_name=name,
        manager=sender,
        reserve=sender,
    )
    signed = txn.sign(signer_pk)
    txid = client.send_transaction(signed)
    result = algosdk.transaction.wait_for_confirmation(client, txid, 4)
    asset_id = result["asset-index"]
    print(f"Staked asset created: asset_id={asset_id}")
    return asset_id
```

---

## 5. Deploy the Farm Contract

The `deploy_farm.py` script handles compilation and deployment. Below is a self-contained version:

```python
import base64
import algosdk
from farm.farm import Farm
from farm.escrow import EscrowPrecompile

# Constants from deploy_farm.py
ESCROW_LEN = 346
BOX_COST   = 2500 + 4000 * (ESCROW_LEN + len("Escrow"))

def deploy_farm(
    sender: str,
    signer_pk: str,
    staked_asset_id: int,
    gas_station_app_id: int,
    admin_address: str,
    updater_address: str,
) -> int:
    """
    Deploy a new Farm contract.

    Returns:
        int: The new Farm application ID.
    """
    farm = Farm()

    # Compile escrow precompile first (needed by Farm's approval program)
    escrow = EscrowPrecompile()
    escrow.compile(client)

    # Compile Farm approval & clear programs
    compiled_approval = client.compile(farm.approval_program)["result"]
    compiled_clear    = client.compile(farm.clear_program)["result"]

    sp = client.suggested_params()

    # The gas station's app address receives the ALGO for escrow box storage
    gs_app_info = client.application_info(gas_station_app_id)
    gs_address  = algosdk.logic.get_application_address(gas_station_app_id)

    # [Txn 0] Fund the gas station so it can pay for the Escrow box
    fund_tx = algosdk.transaction.PaymentTxn(
        sender=sender,
        receiver=gs_address,
        amt=100_000 + BOX_COST,  # MinBalance + box cost
        sp=sp,
    )

    # ABI selector for create(application,asset,account,account)void
    create_sig = algosdk.abi.Method.from_signature(
        "create(application,asset,account,account)void"
    ).get_selector()

    # [Txn 1] Create the Farm application
    # app_args encoding: [selector, gas_station_ref, staked_asset_ref, admin_ref, updater_ref]
    create_tx = algosdk.transaction.ApplicationCreateTxn(
        sender=sender,
        on_complete=algosdk.transaction.OnComplete.NoOpOC,
        approval_program=base64.b64decode(compiled_approval),
        clear_program=base64.b64decode(compiled_clear),
        global_schema=algosdk.transaction.StateSchema(7, 10),  # 7 uint64, 10 bytes
        local_schema=algosdk.transaction.StateSchema(2, 4),    # 2 uint64, 4 bytes
        app_args=[
            create_sig,
            algosdk.abi.UintType(8).encode(1),  # gas_station at foreign_apps[1]
            algosdk.abi.UintType(8).encode(0),  # staked_asset at foreign_assets[0]
            algosdk.abi.UintType(8).encode(1),  # admin at accounts[1]
            algosdk.abi.UintType(8).encode(2),  # updater at accounts[2]
        ],
        accounts=[admin_address, updater_address],
        foreign_assets=[staked_asset_id],
        foreign_apps=[gas_station_app_id],
        boxes=[(0, b"Escrow")],
        sp=algosdk.transaction.SuggestedParams(
            **{**sp.__dict__, "fee": 3000, "flat_fee": True}
        ),
        extra_pages=2,
    )

    # Group and sign
    algosdk.transaction.assign_group_id([fund_tx, create_tx])
    signed_group = [
        fund_tx.sign(signer_pk),
        create_tx.sign(signer_pk),
    ]

    txid = client.send_transactions(signed_group)
    result = algosdk.transaction.wait_for_confirmation(client, txid, 4)
    app_id = result["application-index"]
    print(f"Farm deployed: app_id={app_id}")
    return app_id
```

### Required minimum balance

Before the Farm can hold reward assets you must fund its app address:

```python
def fund_farm(sender: str, signer_pk: str, farm_app_id: int, amount_algos: float = 1.0):
    farm_address = algosdk.logic.get_application_address(farm_app_id)
    sp = client.suggested_params()
    txn = algosdk.transaction.PaymentTxn(
        sender=sender,
        receiver=farm_address,
        amt=int(amount_algos * 1_000_000),
        sp=sp,
    )
    txid = client.send_transaction(txn.sign(signer_pk))
    algosdk.transaction.wait_for_confirmation(client, txid, 4)
    print(f"Farm funded with {amount_algos} ALGO")
```

---

## 6. Post-Deploy: Add Reward Assets

Only the **admin** can add reward assets (max 7). Each asset must be registered before rewards can be deposited.

```python
def add_reward_asset(
    farm_app_id: int,
    reward_asset_id: int,  # pass 0 for native ALGO rewards
    admin_address: str,
    admin_pk: str,
):
    sp = client.suggested_params()
    # fee=2000 covers the inner opt-in transaction
    sp.fee = 2000
    sp.flat_fee = True

    txn = algosdk.transaction.ApplicationCallTxn(
        sender=admin_address,
        index=farm_app_id,
        on_complete=algosdk.transaction.OnComplete.NoOpOC,
        app_args=[
            algosdk.abi.Method.from_signature(
                "add_reward_asset(asset)void"
            ).get_selector(),
            algosdk.abi.UintType(8).encode(0),  # reward_asset at foreign_assets[0]
        ],
        foreign_assets=[reward_asset_id] if reward_asset_id != 0 else [],
        sp=sp,
    )
    txid = client.send_transaction(txn.sign(admin_pk))
    algosdk.transaction.wait_for_confirmation(client, txid, 4)
    print(f"Reward asset {reward_asset_id} added to farm {farm_app_id}")
```

---

## 7. Deposit Rewards

Rewards are deposited as a group: one transfer per reward asset followed by `deposit_rewards`.

```python
def deposit_rewards(
    farm_app_id: int,
    reward_asset_ids: list[int],    # Algorand ASA IDs (0 = ALGO)
    reward_amounts: list[int],      # micro-units
    duration_seconds: int,
    admin_address: str,
    admin_pk: str,
    staked_asset_id: int,
):
    farm_address = algosdk.logic.get_application_address(farm_app_id)
    sp = client.suggested_params()
    txns = []

    # One transfer txn per reward asset
    for asset_id, amount in zip(reward_asset_ids, reward_amounts):
        if asset_id == 0:
            txns.append(algosdk.transaction.PaymentTxn(
                sender=admin_address,
                receiver=farm_address,
                amt=amount,
                sp=sp,
            ))
        else:
            txns.append(algosdk.transaction.AssetTransferTxn(
                sender=admin_address,
                receiver=farm_address,
                amt=amount,
                index=asset_id,
                sp=sp,
            ))

    # update_global_state first (required by deposit_rewards)
    update_sp = algosdk.transaction.SuggestedParams(**{**sp.__dict__, "fee": 1000, "flat_fee": True})
    txns.append(algosdk.transaction.ApplicationCallTxn(
        sender=admin_address,
        index=farm_app_id,
        on_complete=algosdk.transaction.OnComplete.NoOpOC,
        app_args=[
            algosdk.abi.Method.from_signature("update_global_state()void").get_selector()
        ],
        sp=update_sp,
    ))

    # deposit_rewards call — reward_ids are indices into RewardAssetIDs array
    reward_indices = list(range(len(reward_asset_ids)))
    deposit_sp = algosdk.transaction.SuggestedParams(**{**sp.__dict__, "fee": 1000, "flat_fee": True})
    txns.append(algosdk.transaction.ApplicationCallTxn(
        sender=admin_address,
        index=farm_app_id,
        on_complete=algosdk.transaction.OnComplete.NoOpOC,
        app_args=[
            algosdk.abi.Method.from_signature(
                "deposit_rewards(uint64[],uint64)void"
            ).get_selector(),
            algosdk.abi.ArrayDynamicType(algosdk.abi.UintType(64)).encode(reward_indices),
            algosdk.abi.UintType(64).encode(duration_seconds),
        ],
        foreign_assets=reward_asset_ids,
        sp=deposit_sp,
    ))

    algosdk.transaction.assign_group_id(txns)
    signed = [t.sign(admin_pk) for t in txns]
    txid = client.send_transactions(signed)
    algosdk.transaction.wait_for_confirmation(client, txid, 4)
    print(f"Deposited rewards: {list(zip(reward_asset_ids, reward_amounts))} over {duration_seconds}s")
```

---

## 8. User Flows (Opt-in & Staking)

### 8a. Opt-in to the Farm

A user opts in by creating a bare "escrow husk" contract and then calling `Farm.opt_in` in the same group.

```python
from farm.escrow import ESCROW_HUSK_BYTECODE, ESCROW_HUSK_CLEAR_BYTECODE

def user_opt_in(
    farm_app_id: int,
    user_address: str,
    user_pk: str,
    staked_asset_id: int,
    gas_station_app_id: int,
):
    sp = client.suggested_params()
    farm_address = algosdk.logic.get_application_address(farm_app_id)

    # [Txn 0] Create the escrow "husk" — bare minimal bytecode
    # The Farm will immediately upgrade it to the full Escrow bytecode
    escrow_husk_create = algosdk.transaction.ApplicationCreateTxn(
        sender=user_address,
        on_complete=algosdk.transaction.OnComplete.NoOpOC,
        approval_program=ESCROW_HUSK_BYTECODE,
        clear_program=b"\x08\x81\x01\x43",  # ESCROW_HUSK_CLEAR_BYTECODE
        global_schema=algosdk.transaction.StateSchema(1, 0),  # MasterAppID
        local_schema=algosdk.transaction.StateSchema(0, 0),
        app_args=[
            algosdk.abi.Method.from_signature(
                "create(application,application,asset)void"
            ).get_selector(),
            algosdk.abi.UintType(8).encode(1),  # farm at foreign_apps[1]
            algosdk.abi.UintType(8).encode(2),  # gas_station at foreign_apps[2]
            algosdk.abi.UintType(8).encode(0),  # staked_asset at foreign_assets[0]
        ],
        foreign_apps=[farm_app_id, gas_station_app_id],
        foreign_assets=[staked_asset_id],
        sp=algosdk.transaction.SuggestedParams(**{**sp.__dict__, "fee": 5000, "flat_fee": True}),
    )

    # [Txn 1] Opt-in to the Farm (triggers Farm to upgrade the escrow)
    farm_opt_in = algosdk.transaction.ApplicationOptInTxn(
        sender=user_address,
        index=farm_app_id,
        sp=algosdk.transaction.SuggestedParams(**{**sp.__dict__, "fee": 3000, "flat_fee": True}),
    )

    algosdk.transaction.assign_group_id([escrow_husk_create, farm_opt_in])
    signed = [escrow_husk_create.sign(user_pk), farm_opt_in.sign(user_pk)]
    txid = client.send_transactions(signed)
    algosdk.transaction.wait_for_confirmation(client, txid, 4)
    print(f"User {user_address} opted in to farm {farm_app_id}")
```

### 8b. Stake tokens

After opting in, the user sends staked tokens directly to their Escrow's address.

```python
def stake(
    farm_app_id: int,
    user_address: str,
    user_pk: str,
    staked_asset_id: int,
    amount: int,
):
    # Retrieve the user's escrow app ID from local state
    local_state = client.account_application_info(user_address, farm_app_id)
    escrow_app_id = None
    for kv in local_state["app-local-state"]["key-value"]:
        if base64.b64decode(kv["key"]) == b"EscrowID":
            escrow_app_id = kv["value"]["uint"]
            break

    if escrow_app_id is None:
        raise ValueError("User is not opted in or escrow not found")

    escrow_address = algosdk.logic.get_application_address(escrow_app_id)
    sp = client.suggested_params()

    txn = algosdk.transaction.AssetTransferTxn(
        sender=user_address,
        receiver=escrow_address,
        amt=amount,
        index=staked_asset_id,
        sp=sp,
    )
    txid = client.send_transaction(txn.sign(user_pk))
    algosdk.transaction.wait_for_confirmation(client, txid, 4)
    print(f"Staked {amount} of asset {staked_asset_id} in escrow {escrow_app_id}")
```

---

## 9. Claiming Rewards

```python
def claim_rewards(
    farm_app_id: int,
    user_address: str,
    caller_pk: str,        # can be anyone, not just the user
    escrow_app_id: int,
    reward_asset_ids: list[int],
    staked_asset_id: int,
):
    escrow_address = algosdk.logic.get_application_address(escrow_app_id)
    sp = client.suggested_params()

    # Number of inner transfer txns = number of reward assets
    fee = 1000 * (1 + len(reward_asset_ids))

    # [Txn 0] update_global_state
    # [Txn 1] update_state  (syncs user's accrued rewards)
    # [Txn 2] claim_rewards (sends rewards to user)
    update_global = algosdk.transaction.ApplicationCallTxn(
        sender=user_address,
        index=farm_app_id,
        on_complete=algosdk.transaction.OnComplete.NoOpOC,
        app_args=[
            algosdk.abi.Method.from_signature("update_global_state()void").get_selector()
        ],
        sp=sp,
    )

    update_user = algosdk.transaction.ApplicationCallTxn(
        sender=user_address,
        index=farm_app_id,
        on_complete=algosdk.transaction.OnComplete.NoOpOC,
        app_args=[
            algosdk.abi.Method.from_signature(
                "update_state(application,account,account,asset)void"
            ).get_selector(),
            algosdk.abi.UintType(8).encode(1),  # escrow at foreign_apps[1]
            algosdk.abi.UintType(8).encode(1),  # escrow_account at accounts[1]
            algosdk.abi.UintType(8).encode(2),  # user at accounts[2]
            algosdk.abi.UintType(8).encode(0),  # staked_asset at foreign_assets[0]
        ],
        accounts=[escrow_address, user_address],
        foreign_apps=[escrow_app_id],
        foreign_assets=[staked_asset_id],
        sp=sp,
    )

    reward_indices = list(range(len(reward_asset_ids)))
    claim_sp = algosdk.transaction.SuggestedParams(**{**sp.__dict__, "fee": fee, "flat_fee": True})
    claim = algosdk.transaction.ApplicationCallTxn(
        sender=user_address,
        index=farm_app_id,
        on_complete=algosdk.transaction.OnComplete.NoOpOC,
        app_args=[
            algosdk.abi.Method.from_signature(
                "claim_rewards(account,uint64[])void"
            ).get_selector(),
            algosdk.abi.UintType(8).encode(1),  # account at accounts[1]
            algosdk.abi.ArrayDynamicType(algosdk.abi.UintType(64)).encode(reward_indices),
        ],
        accounts=[user_address],
        foreign_assets=reward_asset_ids,
        sp=claim_sp,
    )

    algosdk.transaction.assign_group_id([update_global, update_user, claim])
    caller_kp = (user_address, caller_pk)
    signed = [t.sign(caller_pk) for t in [update_global, update_user, claim]]
    txid = client.send_transactions(signed)
    algosdk.transaction.wait_for_confirmation(client, txid, 4)
    print("Rewards claimed successfully")
```

---

## 10. Unstaking

```python
def unstake(
    farm_app_id: int,
    user_address: str,
    user_pk: str,
    escrow_app_id: int,
    staked_asset_id: int,
    amount: int,
):
    escrow_address = algosdk.logic.get_application_address(escrow_app_id)
    sp = client.suggested_params()

    # [Txn 0] update_global_state
    update_global = algosdk.transaction.ApplicationCallTxn(
        sender=user_address,
        index=farm_app_id,
        on_complete=algosdk.transaction.OnComplete.NoOpOC,
        app_args=[
            algosdk.abi.Method.from_signature("update_global_state()void").get_selector()
        ],
        sp=sp,
    )

    # [Txn 1] Call Escrow.unstake — the escrow will internally call Farm.update_state
    unstake_sp = algosdk.transaction.SuggestedParams(**{**sp.__dict__, "fee": 5000, "flat_fee": True})
    unstake_call = algosdk.transaction.ApplicationCallTxn(
        sender=user_address,
        index=escrow_app_id,
        on_complete=algosdk.transaction.OnComplete.NoOpOC,
        app_args=[
            algosdk.abi.Method.from_signature(
                "unstake(asset,uint64,application)void"
            ).get_selector(),
            algosdk.abi.UintType(8).encode(0),    # staked_asset at foreign_assets[0]
            algosdk.abi.UintType(64).encode(amount),
            algosdk.abi.UintType(8).encode(1),    # master (farm) at foreign_apps[1]
        ],
        foreign_assets=[staked_asset_id],
        foreign_apps=[farm_app_id],
        accounts=[user_address],
        sp=unstake_sp,
    )

    algosdk.transaction.assign_group_id([update_global, unstake_call])
    signed = [update_global.sign(user_pk), unstake_call.sign(user_pk)]
    txid = client.send_transactions(signed)
    algosdk.transaction.wait_for_confirmation(client, txid, 4)
    print(f"Unstaked {amount} tokens from escrow {escrow_app_id}")
```

---

## 11. Upgrading the Farm Contract

Contract upgrades require two transactions in the same group:

```python
def upgrade_farm(
    farm_app_id: int,
    new_approval_teal: str,   # compiled TEAL source
    new_clear_teal: str,
    updater_address: str,
    updater_pk: str,
):
    sp = client.suggested_params()
    new_approval = base64.b64decode(client.compile(new_approval_teal)["result"])
    new_clear    = base64.b64decode(client.compile(new_clear_teal)["result"])

    # [Txn 0] update — verifies post_update call is next in group
    update_sp = algosdk.transaction.SuggestedParams(**{**sp.__dict__, "fee": 1000, "flat_fee": True})
    update_txn = algosdk.transaction.ApplicationUpdateTxn(
        sender=updater_address,
        index=farm_app_id,
        approval_program=new_approval,
        clear_program=new_clear,
        app_args=[
            algosdk.abi.Method.from_signature("update()void").get_selector()
        ],
        sp=update_sp,
    )

    # [Txn 1] post_update — runs migration logic, validates escrow bytecode is unchanged
    post_update_txn = algosdk.transaction.ApplicationCallTxn(
        sender=updater_address,
        index=farm_app_id,
        on_complete=algosdk.transaction.OnComplete.NoOpOC,
        app_args=[
            algosdk.abi.Method.from_signature("post_update()void").get_selector()
        ],
        boxes=[(farm_app_id, b"Escrow")],
        sp=sp,
    )

    algosdk.transaction.assign_group_id([update_txn, post_update_txn])
    signed = [update_txn.sign(updater_pk), post_update_txn.sign(updater_pk)]
    txid = client.send_transactions(signed)
    algosdk.transaction.wait_for_confirmation(client, txid, 4)
    print(f"Farm {farm_app_id} upgraded successfully")
```

> **Warning**: The escrow bytecode must remain unchanged across Farm upgrades. The `post_update` method will fail if the Escrow box content differs from the compiled bytecode.

---

## 12. Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `overspend` | Insufficient ALGO in sender or app address | Fund the Farm app address; pool fees correctly |
| `assert failed: Escrow validation failed` | Wrong escrow app ID or bytecode mismatch | Pass the correct escrow app ID; ensure user is opted in |
| `assert failed: creator is not opted in` | User has not called `Farm.opt_in` | Have the user opt-in first |
| `assert failed: Update was already done` | `post_update` called twice | Only call `post_update` once per upgrade |
| `assert failed: Escrow's bytecode must remain unchanged` | Escrow code changed in upgrade | Escrow TEAL cannot change; revert or redeploy |
| `assert failed: You must deposit Algos for the opt-in` | ALGO reward balance insufficient | Top up the Farm contract's ALGO balance |
| `assert failed: Previous transaction must be an Escrow creation` | Group format wrong during opt-in | Ensure escrow creation is txn[N-1] and opt-in is txn[N] |
| `invalid group id` | Transactions not grouped before signing | Call `assign_group_id` before signing |
| `fee too small` | Inner txns need pooled fees | Increase fee on the outer txn (e.g. fee=3000 for 2 inner txns) |
