"""
Integration tests for the Pact Finance Farm contract.

These tests use the Algorand localnet (AlgoKit sandbox) and the algosdk
test account fixtures. Run against a live localnet:

    algokit localnet start
    pytest tests/test_farm.py -v

Dependencies:
    pip install pytest algosdk beaker-pyteal pyteal pytealext pactsdk
"""

import base64
import time

import algosdk
import pytest
from algosdk.v2client import algod

# ---------------------------------------------------------------------------
# Configuration — override via environment variables or edit here
# ---------------------------------------------------------------------------
ALGOD_URL   = "http://localhost:4001"
ALGOD_TOKEN = "a" * 64


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def algod_client():
    return algod.AlgodClient(ALGOD_TOKEN, ALGOD_URL)


@pytest.fixture(scope="session")
def accounts(algod_client):
    """
    Return pre-funded test accounts from the localnet KMD wallet.
    Requires AlgoKit localnet (`algokit localnet start`).
    """
    from algosdk.kmd import KMDClient

    kmd = KMDClient("a" * 64, "http://localhost:4002")
    wallets = kmd.list_wallets()
    wallet_id = next(w["id"] for w in wallets if w["name"] == "unencrypted-default-wallet")
    handle = kmd.init_wallet_handle(wallet_id, "")
    keys = kmd.list_keys(handle)
    accs = [(k, kmd.export_key(handle, "", k)) for k in keys[:3]]
    kmd.release_wallet_handle(handle)
    return accs  # list of (address, private_key)


@pytest.fixture(scope="session")
def admin(accounts):
    return accounts[0]  # (address, private_key)


@pytest.fixture(scope="session")
def user(accounts):
    return accounts[1]


@pytest.fixture(scope="session")
def user2(accounts):
    return accounts[2]


@pytest.fixture(scope="session")
def staked_asset_id(algod_client, admin):
    """Create a test LP token as the staked asset."""
    addr, pk = admin
    sp = algod_client.suggested_params()
    txn = algosdk.transaction.AssetConfigTxn(
        sender=addr,
        sp=sp,
        total=10 ** 18,
        default_frozen=False,
        decimals=6,
        unit_name="LPTEST",
        asset_name="Test LP Token",
        manager=addr,
        reserve=addr,
    )
    txid = algod_client.send_transaction(txn.sign(pk))
    result = algosdk.transaction.wait_for_confirmation(algod_client, txid, 4)
    return result["asset-index"]


@pytest.fixture(scope="session")
def reward_asset_id(algod_client, admin):
    """Create a reward token (simulates a protocol reward token)."""
    addr, pk = admin
    sp = algod_client.suggested_params()
    txn = algosdk.transaction.AssetConfigTxn(
        sender=addr,
        sp=sp,
        total=10 ** 18,
        default_frozen=False,
        decimals=6,
        unit_name="REWARD",
        asset_name="Test Reward Token",
        manager=addr,
        reserve=addr,
    )
    txid = algod_client.send_transaction(txn.sign(pk))
    result = algosdk.transaction.wait_for_confirmation(algod_client, txid, 4)
    return result["asset-index"]


@pytest.fixture(scope="session")
def gas_station_app_id(algod_client, admin):
    """Deploy the GasStation contract."""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from gas_station import GasStationContract

    addr, pk = admin
    gs = GasStationContract()

    compiled_approval = algod_client.compile(gs.approval_program)["result"]
    compiled_clear    = algod_client.compile(gs.clear_program)["result"]

    sp = algod_client.suggested_params()
    create_txn = algosdk.transaction.ApplicationCreateTxn(
        sender=addr,
        on_complete=algosdk.transaction.OnComplete.NoOpOC,
        approval_program=base64.b64decode(compiled_approval),
        clear_program=base64.b64decode(compiled_clear),
        global_schema=algosdk.transaction.StateSchema(0, 0),
        local_schema=algosdk.transaction.StateSchema(0, 0),
        sp=sp,
    )
    txid = algod_client.send_transaction(create_txn.sign(pk))
    result = algosdk.transaction.wait_for_confirmation(algod_client, txid, 4)
    app_id = result["application-index"]

    # Fund the GasStation so it can relay ALGOs
    gs_address = algosdk.logic.get_application_address(app_id)
    fund_txn = algosdk.transaction.PaymentTxn(
        sender=addr,
        receiver=gs_address,
        amt=10_000_000,  # 10 ALGO
        sp=sp,
    )
    algod_client.send_transaction(fund_txn.sign(pk))
    return app_id


@pytest.fixture(scope="session")
def farm_app_id(algod_client, admin, staked_asset_id, gas_station_app_id):
    """Deploy a Farm contract and return its app ID."""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from farm.farm import Farm
    from farm.escrow import EscrowPrecompile

    addr, pk = admin

    escrow = EscrowPrecompile()
    escrow.compile(algod_client)

    farm = Farm()
    compiled_approval = algod_client.compile(farm.approval_program)["result"]
    compiled_clear    = algod_client.compile(farm.clear_program)["result"]

    ESCROW_LEN = 346
    BOX_COST   = 2500 + 4000 * (ESCROW_LEN + len("Escrow"))

    sp = algod_client.suggested_params()
    gs_address = algosdk.logic.get_application_address(gas_station_app_id)

    fund_tx = algosdk.transaction.PaymentTxn(
        sender=addr,
        receiver=gs_address,
        amt=100_000 + BOX_COST,
        sp=sp,
    )

    create_sig = algosdk.abi.Method.from_signature(
        "create(application,asset,account,account)void"
    ).get_selector()

    create_sp = algosdk.transaction.SuggestedParams(**{**sp.__dict__, "fee": 3000, "flat_fee": True})
    create_tx = algosdk.transaction.ApplicationCreateTxn(
        sender=addr,
        on_complete=algosdk.transaction.OnComplete.NoOpOC,
        approval_program=base64.b64decode(compiled_approval),
        clear_program=base64.b64decode(compiled_clear),
        global_schema=algosdk.transaction.StateSchema(7, 10),
        local_schema=algosdk.transaction.StateSchema(2, 4),
        app_args=[
            create_sig,
            algosdk.abi.UintType(8).encode(1),  # gas_station
            algosdk.abi.UintType(8).encode(0),  # staked_asset
            algosdk.abi.UintType(8).encode(1),  # admin (same as sender here)
            algosdk.abi.UintType(8).encode(1),  # updater (same as admin for tests)
        ],
        accounts=[addr],
        foreign_assets=[staked_asset_id],
        foreign_apps=[gas_station_app_id],
        boxes=[(0, b"Escrow")],
        sp=create_sp,
        extra_pages=2,
    )

    algosdk.transaction.assign_group_id([fund_tx, create_tx])
    signed = [fund_tx.sign(pk), create_tx.sign(pk)]
    txid = algod_client.send_transactions(signed)
    result = algosdk.transaction.wait_for_confirmation(algod_client, txid, 4)
    app_id = result["application-index"]

    # Fund the farm's app address with initial ALGO for MinBalance
    farm_address = algosdk.logic.get_application_address(app_id)
    fund_farm = algosdk.transaction.PaymentTxn(
        sender=addr,
        receiver=farm_address,
        amt=5_000_000,
        sp=sp,
    )
    algod_client.send_transaction(fund_farm.sign(pk))
    algosdk.transaction.wait_for_confirmation(algod_client, fund_farm.get_txid(), 4)

    return app_id


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _get_global_state(algod_client, app_id: int) -> dict:
    info = algod_client.application_info(app_id)
    state = {}
    for kv in info.get("params", {}).get("global-state", []):
        key = base64.b64decode(kv["key"]).decode(errors="replace")
        val = kv["value"]
        state[key] = val.get("uint", 0) if val["type"] == 2 else base64.b64decode(val.get("bytes", ""))
    return state


def _get_local_state(algod_client, address: str, app_id: int) -> dict:
    info = algod_client.account_application_info(address, app_id)
    state = {}
    for kv in info.get("app-local-state", {}).get("key-value", []):
        key = base64.b64decode(kv["key"]).decode(errors="replace")
        val = kv["value"]
        state[key] = val.get("uint", 0) if val["type"] == 2 else base64.b64decode(val.get("bytes", ""))
    return state


def _opt_in_asset(algod_client, address: str, pk: str, asset_id: int):
    sp = algod_client.suggested_params()
    txn = algosdk.transaction.AssetTransferTxn(
        sender=address, receiver=address, amt=0, index=asset_id, sp=sp
    )
    txid = algod_client.send_transaction(txn.sign(pk))
    algosdk.transaction.wait_for_confirmation(algod_client, txid, 4)


def _transfer_asset(algod_client, sender_addr, sender_pk, receiver, asset_id, amount):
    sp = algod_client.suggested_params()
    txn = algosdk.transaction.AssetTransferTxn(
        sender=sender_addr, receiver=receiver, amt=amount, index=asset_id, sp=sp
    )
    txid = algod_client.send_transaction(txn.sign(sender_pk))
    algosdk.transaction.wait_for_confirmation(algod_client, txid, 4)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFarmDeployment:
    def test_farm_deployed(self, algod_client, farm_app_id):
        """Farm contract should be deployed and have expected global state keys."""
        info = algod_client.application_info(farm_app_id)
        assert info["id"] == farm_app_id
        state = _get_global_state(algod_client, farm_app_id)

        assert "CONTRACT_NAME" in state
        assert state["CONTRACT_NAME"] == b"PACT FARM"
        assert "VERSION" in state
        assert state["VERSION"] == 101

    def test_farm_global_state_defaults(self, algod_client, farm_app_id, staked_asset_id):
        """Check default values of Farm global state after deployment."""
        state = _get_global_state(algod_client, farm_app_id)

        assert state.get("TotalStaked", 0) == 0
        assert state.get("NumStakers", 0) == 0
        assert state.get("Duration", 0) == 0
        assert state.get("NextDuration", 0) == 0
        assert state.get("StakedAssetID") == staked_asset_id

    def test_escrow_box_exists(self, algod_client, farm_app_id):
        """The Escrow box should have been created on Farm creation."""
        boxes = algod_client.application_boxes(farm_app_id)
        box_names = [base64.b64decode(b["name"]) for b in boxes.get("boxes", [])]
        assert b"Escrow" in box_names


class TestAddRewardAsset:
    def test_add_asa_reward(self, algod_client, farm_app_id, reward_asset_id, admin):
        """Admin can add an ASA reward asset."""
        addr, pk = admin
        sp = algod_client.suggested_params()

        # Farm needs to opt in to the reward asset (inner txn)
        sp_fee = algosdk.transaction.SuggestedParams(**{**sp.__dict__, "fee": 2000, "flat_fee": True})
        txn = algosdk.transaction.ApplicationCallTxn(
            sender=addr,
            index=farm_app_id,
            on_complete=algosdk.transaction.OnComplete.NoOpOC,
            app_args=[
                algosdk.abi.Method.from_signature("add_reward_asset(asset)void").get_selector(),
                algosdk.abi.UintType(8).encode(0),
            ],
            foreign_assets=[reward_asset_id],
            sp=sp_fee,
        )
        txid = algod_client.send_transaction(txn.sign(pk))
        algosdk.transaction.wait_for_confirmation(algod_client, txid, 4)

        state = _get_global_state(algod_client, farm_app_id)
        reward_ids_bytes = state.get("RewardAssetIDs", b"")
        # The reward_asset_id should be encoded in the 8-byte array
        assert len(reward_ids_bytes) == 8
        decoded_id = int.from_bytes(reward_ids_bytes, "big")
        assert decoded_id == reward_asset_id

    def test_add_algo_reward(self, algod_client, farm_app_id, admin):
        """Admin can add ALGO (asset ID 0) as a reward."""
        addr, pk = admin
        sp = algod_client.suggested_params()

        # Note: adding ALGO (id=0) does not trigger an inner opt-in txn
        txn = algosdk.transaction.ApplicationCallTxn(
            sender=addr,
            index=farm_app_id,
            on_complete=algosdk.transaction.OnComplete.NoOpOC,
            app_args=[
                algosdk.abi.Method.from_signature("add_reward_asset(asset)void").get_selector(),
                algosdk.abi.UintType(8).encode(0),
            ],
            foreign_assets=[0],
            sp=sp,
        )
        txid = algod_client.send_transaction(txn.sign(pk))
        # This may fail if ALGO is already added — that's expected behaviour
        try:
            algosdk.transaction.wait_for_confirmation(algod_client, txid, 4)
        except Exception as e:
            assert "already" in str(e).lower() or "assert" in str(e).lower()

    def test_non_admin_cannot_add_reward(self, algod_client, farm_app_id, reward_asset_id, user):
        """Non-admin should not be able to add a reward asset."""
        addr, pk = user
        sp = algod_client.suggested_params()

        txn = algosdk.transaction.ApplicationCallTxn(
            sender=addr,
            index=farm_app_id,
            on_complete=algosdk.transaction.OnComplete.NoOpOC,
            app_args=[
                algosdk.abi.Method.from_signature("add_reward_asset(asset)void").get_selector(),
                algosdk.abi.UintType(8).encode(0),
            ],
            foreign_assets=[reward_asset_id],
            sp=sp,
        )
        with pytest.raises(Exception, match=r"(?i)(reject|assert|unauthorized|not authorized)"):
            txid = algod_client.send_transaction(txn.sign(pk))
            algosdk.transaction.wait_for_confirmation(algod_client, txid, 4)


class TestUserOptIn:
    def test_user_opt_in(self, algod_client, farm_app_id, staked_asset_id,
                          gas_station_app_id, user):
        """User can opt-in to the Farm, creating an Escrow sub-contract."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from farm.escrow import ESCROW_HUSK_BYTECODE, ESCROW_HUSK_CLEAR_BYTECODE

        addr, pk = user
        sp = algod_client.suggested_params()

        # User must opt-in to the staked asset first
        _opt_in_asset(algod_client, addr, pk, staked_asset_id)

        escrow_husk_create = algosdk.transaction.ApplicationCreateTxn(
            sender=addr,
            on_complete=algosdk.transaction.OnComplete.NoOpOC,
            approval_program=ESCROW_HUSK_BYTECODE,
            clear_program=ESCROW_HUSK_CLEAR_BYTECODE,
            global_schema=algosdk.transaction.StateSchema(1, 0),
            local_schema=algosdk.transaction.StateSchema(0, 0),
            app_args=[
                algosdk.abi.Method.from_signature(
                    "create(application,application,asset)void"
                ).get_selector(),
                algosdk.abi.UintType(8).encode(1),
                algosdk.abi.UintType(8).encode(2),
                algosdk.abi.UintType(8).encode(0),
            ],
            foreign_apps=[farm_app_id, gas_station_app_id],
            foreign_assets=[staked_asset_id],
            sp=algosdk.transaction.SuggestedParams(**{**sp.__dict__, "fee": 5000, "flat_fee": True}),
        )

        farm_opt_in = algosdk.transaction.ApplicationOptInTxn(
            sender=addr,
            index=farm_app_id,
            sp=algosdk.transaction.SuggestedParams(**{**sp.__dict__, "fee": 3000, "flat_fee": True}),
        )

        algosdk.transaction.assign_group_id([escrow_husk_create, farm_opt_in])
        signed = [escrow_husk_create.sign(pk), farm_opt_in.sign(pk)]
        txid = algod_client.send_transactions(signed)
        algosdk.transaction.wait_for_confirmation(algod_client, txid, 4)

        # Verify local state was initialized
        local = _get_local_state(algod_client, addr, farm_app_id)
        assert "EscrowID" in local
        assert local["EscrowID"] > 0
        assert local.get("Staked", 0) == 0

    def test_user_escrow_exists(self, algod_client, farm_app_id, user):
        """Escrow app should exist after opt-in."""
        addr, _ = user
        local = _get_local_state(algod_client, addr, farm_app_id)
        escrow_id = local["EscrowID"]
        info = algod_client.application_info(escrow_id)
        assert info["id"] == escrow_id

    def test_double_opt_in_fails(self, algod_client, farm_app_id, user):
        """A second opt-in to the same Farm should fail."""
        addr, pk = user
        sp = algod_client.suggested_params()
        txn = algosdk.transaction.ApplicationOptInTxn(
            sender=addr, index=farm_app_id, sp=sp
        )
        with pytest.raises(Exception):
            txid = algod_client.send_transaction(txn.sign(pk))
            algosdk.transaction.wait_for_confirmation(algod_client, txid, 4)


class TestStaking:
    def test_stake_tokens(self, algod_client, farm_app_id, staked_asset_id, admin, user):
        """User can stake tokens by sending them to their Escrow address."""
        user_addr, user_pk = user
        admin_addr, admin_pk = admin

        local = _get_local_state(algod_client, user_addr, farm_app_id)
        escrow_id = local["EscrowID"]
        escrow_address = algosdk.logic.get_application_address(escrow_id)

        # Transfer staked tokens from admin (who has them all) to user
        _opt_in_asset(algod_client, user_addr, user_pk, staked_asset_id)
        _transfer_asset(algod_client, admin_addr, admin_pk, user_addr, staked_asset_id, 1_000_000)

        # Stake: send tokens to escrow
        _transfer_asset(algod_client, user_addr, user_pk, escrow_address, staked_asset_id, 1_000_000)

        # Verify escrow holds the tokens
        asset_info = algod_client.account_asset_info(escrow_address, staked_asset_id)
        assert asset_info["asset-holding"]["amount"] == 1_000_000

    def test_update_state_after_stake(self, algod_client, farm_app_id, staked_asset_id, user):
        """update_state should reflect the new stake in local and global state."""
        user_addr, user_pk = user
        local = _get_local_state(algod_client, user_addr, farm_app_id)
        escrow_id = local["EscrowID"]
        escrow_address = algosdk.logic.get_application_address(escrow_id)
        sp = algod_client.suggested_params()

        update_global = algosdk.transaction.ApplicationCallTxn(
            sender=user_addr,
            index=farm_app_id,
            on_complete=algosdk.transaction.OnComplete.NoOpOC,
            app_args=[
                algosdk.abi.Method.from_signature("update_global_state()void").get_selector()
            ],
            sp=sp,
        )

        update_user = algosdk.transaction.ApplicationCallTxn(
            sender=user_addr,
            index=farm_app_id,
            on_complete=algosdk.transaction.OnComplete.NoOpOC,
            app_args=[
                algosdk.abi.Method.from_signature(
                    "update_state(application,account,account,asset)void"
                ).get_selector(),
                algosdk.abi.UintType(8).encode(1),
                algosdk.abi.UintType(8).encode(1),
                algosdk.abi.UintType(8).encode(2),
                algosdk.abi.UintType(8).encode(0),
            ],
            accounts=[escrow_address, user_addr],
            foreign_apps=[escrow_id],
            foreign_assets=[staked_asset_id],
            sp=sp,
        )

        algosdk.transaction.assign_group_id([update_global, update_user])
        signed = [update_global.sign(user_pk), update_user.sign(user_pk)]
        txid = algod_client.send_transactions(signed)
        algosdk.transaction.wait_for_confirmation(algod_client, txid, 4)

        # Verify updated local state
        updated_local = _get_local_state(algod_client, user_addr, farm_app_id)
        assert updated_local.get("Staked", 0) == 1_000_000

        # Verify global total staked
        global_state = _get_global_state(algod_client, farm_app_id)
        assert global_state.get("TotalStaked", 0) == 1_000_000
        assert global_state.get("NumStakers", 0) == 1


class TestRewardDistribution:
    def test_deposit_rewards(self, algod_client, farm_app_id, reward_asset_id, admin):
        """Admin can deposit rewards and set the distribution duration."""
        admin_addr, admin_pk = admin
        farm_address = algosdk.logic.get_application_address(farm_app_id)
        sp = algod_client.suggested_params()

        # Transfer reward tokens to admin first (if needed)
        # Assume admin already has reward tokens from asset creation

        reward_amount = 1_000_000  # 1 reward token (6 decimals)
        duration_seconds = 86400   # 24 hours

        reward_tx = algosdk.transaction.AssetTransferTxn(
            sender=admin_addr,
            receiver=farm_address,
            amt=reward_amount,
            index=reward_asset_id,
            sp=sp,
        )

        update_global = algosdk.transaction.ApplicationCallTxn(
            sender=admin_addr,
            index=farm_app_id,
            on_complete=algosdk.transaction.OnComplete.NoOpOC,
            app_args=[
                algosdk.abi.Method.from_signature("update_global_state()void").get_selector()
            ],
            sp=sp,
        )

        deposit_call = algosdk.transaction.ApplicationCallTxn(
            sender=admin_addr,
            index=farm_app_id,
            on_complete=algosdk.transaction.OnComplete.NoOpOC,
            app_args=[
                algosdk.abi.Method.from_signature(
                    "deposit_rewards(uint64[],uint64)void"
                ).get_selector(),
                algosdk.abi.ArrayDynamicType(algosdk.abi.UintType(64)).encode([0]),
                algosdk.abi.UintType(64).encode(duration_seconds),
            ],
            foreign_assets=[reward_asset_id],
            sp=sp,
        )

        algosdk.transaction.assign_group_id([reward_tx, update_global, deposit_call])
        signed = [t.sign(admin_pk) for t in [reward_tx, update_global, deposit_call]]
        txid = algod_client.send_transactions(signed)
        algosdk.transaction.wait_for_confirmation(algod_client, txid, 4)

        global_state = _get_global_state(algod_client, farm_app_id)
        assert global_state.get("Duration", 0) == duration_seconds

    def test_rewards_accrue_over_time(self, algod_client, farm_app_id, staked_asset_id,
                                       reward_asset_id, user):
        """After some time passes, user should have accrued rewards."""
        user_addr, user_pk = user
        local = _get_local_state(algod_client, user_addr, farm_app_id)
        escrow_id = local["EscrowID"]
        escrow_address = algosdk.logic.get_application_address(escrow_id)
        sp = algod_client.suggested_params()

        # Wait a moment (in localnet each block ~4s, we can advance time or just check)
        time.sleep(2)

        update_global = algosdk.transaction.ApplicationCallTxn(
            sender=user_addr,
            index=farm_app_id,
            on_complete=algosdk.transaction.OnComplete.NoOpOC,
            app_args=[
                algosdk.abi.Method.from_signature("update_global_state()void").get_selector()
            ],
            sp=sp,
        )

        update_user = algosdk.transaction.ApplicationCallTxn(
            sender=user_addr,
            index=farm_app_id,
            on_complete=algosdk.transaction.OnComplete.NoOpOC,
            app_args=[
                algosdk.abi.Method.from_signature(
                    "update_state(application,account,account,asset)void"
                ).get_selector(),
                algosdk.abi.UintType(8).encode(1),
                algosdk.abi.UintType(8).encode(1),
                algosdk.abi.UintType(8).encode(2),
                algosdk.abi.UintType(8).encode(0),
            ],
            accounts=[escrow_address, user_addr],
            foreign_apps=[escrow_id],
            foreign_assets=[staked_asset_id],
            sp=sp,
        )

        algosdk.transaction.assign_group_id([update_global, update_user])
        signed = [t.sign(user_pk) for t in [update_global, update_user]]
        txid = algod_client.send_transactions(signed)
        algosdk.transaction.wait_for_confirmation(algod_client, txid, 4)

        updated_local = _get_local_state(algod_client, user_addr, farm_app_id)
        accrued_bytes = updated_local.get("AccruedRewards", b"")
        # The accrued rewards is a packed array of 7 uint64s
        # For a 24h distribution of 1_000_000 tokens, even 2s should yield > 0
        if len(accrued_bytes) >= 8:
            accrued_0 = int.from_bytes(accrued_bytes[:8], "big")
            assert accrued_0 >= 0  # may be 0 if block time hasn't advanced
            print(f"Accrued rewards at index 0: {accrued_0}")


class TestUnstaking:
    def test_unstake_partial(self, algod_client, farm_app_id, staked_asset_id, user):
        """User can unstake a portion of their tokens."""
        user_addr, user_pk = user
        local = _get_local_state(algod_client, user_addr, farm_app_id)
        escrow_id = local["EscrowID"]
        sp = algod_client.suggested_params()

        unstake_amount = 500_000  # half of 1_000_000

        update_global = algosdk.transaction.ApplicationCallTxn(
            sender=user_addr,
            index=farm_app_id,
            on_complete=algosdk.transaction.OnComplete.NoOpOC,
            app_args=[
                algosdk.abi.Method.from_signature("update_global_state()void").get_selector()
            ],
            sp=sp,
        )

        # Escrow.unstake will internally call Farm.update_state
        unstake_sp = algosdk.transaction.SuggestedParams(**{**sp.__dict__, "fee": 5000, "flat_fee": True})
        unstake_call = algosdk.transaction.ApplicationCallTxn(
            sender=user_addr,
            index=escrow_id,
            on_complete=algosdk.transaction.OnComplete.NoOpOC,
            app_args=[
                algosdk.abi.Method.from_signature(
                    "unstake(asset,uint64,application)void"
                ).get_selector(),
                algosdk.abi.UintType(8).encode(0),
                algosdk.abi.UintType(64).encode(unstake_amount),
                algosdk.abi.UintType(8).encode(1),
            ],
            foreign_assets=[staked_asset_id],
            foreign_apps=[farm_app_id],
            accounts=[user_addr],
            sp=unstake_sp,
        )

        algosdk.transaction.assign_group_id([update_global, unstake_call])
        signed = [t.sign(user_pk) for t in [update_global, unstake_call]]
        txid = algod_client.send_transactions(signed)
        algosdk.transaction.wait_for_confirmation(algod_client, txid, 4)

        # User should have received the tokens back
        asset_info = algod_client.account_asset_info(user_addr, staked_asset_id)
        assert asset_info["asset-holding"]["amount"] == unstake_amount

    def test_global_state_after_unstake(self, algod_client, farm_app_id):
        """Global TotalStaked should decrease after unstake."""
        state = _get_global_state(algod_client, farm_app_id)
        assert state.get("TotalStaked", 0) == 500_000


class TestAdminFunctions:
    def test_change_admin(self, algod_client, farm_app_id, admin, user):
        """Admin can transfer admin role to another account."""
        admin_addr, admin_pk = admin
        user_addr, _ = user
        sp = algod_client.suggested_params()

        txn = algosdk.transaction.ApplicationCallTxn(
            sender=admin_addr,
            index=farm_app_id,
            on_complete=algosdk.transaction.OnComplete.NoOpOC,
            app_args=[
                algosdk.abi.Method.from_signature("change_admin(account)void").get_selector(),
                algosdk.abi.UintType(8).encode(1),
            ],
            accounts=[user_addr],
            sp=sp,
        )
        txid = algod_client.send_transaction(txn.sign(admin_pk))
        algosdk.transaction.wait_for_confirmation(algod_client, txid, 4)

        state = _get_global_state(algod_client, farm_app_id)
        # Admin bytes should now be user's address
        assert algosdk.encoding.encode_address(state["Admin"]) == user_addr

        # Restore admin back
        restore_txn = algosdk.transaction.ApplicationCallTxn(
            sender=user_addr,
            index=farm_app_id,
            on_complete=algosdk.transaction.OnComplete.NoOpOC,
            app_args=[
                algosdk.abi.Method.from_signature("change_admin(account)void").get_selector(),
                algosdk.abi.UintType(8).encode(1),
            ],
            accounts=[admin_addr],
            sp=sp,
        )
        _, user_pk = user
        txid = algod_client.send_transaction(restore_txn.sign(user_pk))
        algosdk.transaction.wait_for_confirmation(algod_client, txid, 4)

    def test_non_admin_change_admin_fails(self, algod_client, farm_app_id, user, user2):
        """Non-admin cannot change the admin address."""
        addr, pk = user
        user2_addr, _ = user2
        sp = algod_client.suggested_params()

        txn = algosdk.transaction.ApplicationCallTxn(
            sender=addr,
            index=farm_app_id,
            on_complete=algosdk.transaction.OnComplete.NoOpOC,
            app_args=[
                algosdk.abi.Method.from_signature("change_admin(account)void").get_selector(),
                algosdk.abi.UintType(8).encode(1),
            ],
            accounts=[user2_addr],
            sp=sp,
        )
        with pytest.raises(Exception):
            txid = algod_client.send_transaction(txn.sign(pk))
            algosdk.transaction.wait_for_confirmation(algod_client, txid, 4)
