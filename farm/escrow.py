import base64

from algosdk.v2client.algod import AlgodClient
from beaker import AppPrecompile, Precompile
from beaker.application import get_method_signature
from pyteal import (
    App,
    Assert,
    Balance,
    Btoi,
    Bytes,
    Cond,
    Err,
    Global,
    If,
    Int,
    MethodSignature,
    MinBalance,
    Mode,
    Not,
    OnComplete,
    Seq,
    Suffix,
    Txn,
    TxnType,
    compileTeal,
)
from pytealext import (
    GlobalState,
    MakeInnerApplicationCallTxn,
    MakeInnerAssetTransferTxn,
    MakeInnerPaymentTxn,
    MakeInnerTxn,
)

from ..gas_station import GasStationContract


def get_pyteal_method_signature(fn) -> MethodSignature:
    return MethodSignature(get_method_signature(fn))


class EscrowMethods:
    # Retrieve algos from the account
    WITHDRAW_ALGOS = MethodSignature("withdraw_algos()void")
    # Reduce stake by the specified amount, asset should be the STAKED_ASSET,
    # application must be the MASTER
    UNSTAKE = MethodSignature("unstake(asset,uint64,application)void")
    # Send a 0-value Algos payment to an arbitrary account with a given note
    SEND_MESSAGE = MethodSignature("send_message(account,string)void")
    # application (1) should be the MASTER, application (2) should be the GAS_STATION
    # asset must be the STAKED_ASSET
    CREATE = MethodSignature("create(application,application,asset)void")


MASTER_UPDATE = MethodSignature("update_state(application,account,account,asset)void")
GS_MASTER_KEY = "MasterAppID"
MASTER = GlobalState(GS_MASTER_KEY)


"""
While this code is not needed for the functioning of the Escrow contract,
it's there due to legacy reasons - the previously deployed farms have this version of the contract.
See Escrow's husk below for more details.
"""
on_create = Seq(
    # Store the MASTER_FARM app ID for later use
    MASTER.put(Txn.applications[Btoi(Txn.application_args[1])]),
    # call GAS_STATION to receive algos deposited there earlier
    MakeInnerApplicationCallTxn(
        application_id=Txn.applications[Btoi(Txn.application_args[2])],
        application_args=[get_pyteal_method_signature(GasStationContract.withdraw)],
        fee=Int(0),
    ),
    # Opt-in to the STAKED_ASSET
    MakeInnerAssetTransferTxn(
        asset_receiver=Global.current_application_address(),
        xfer_asset=Txn.assets[Btoi(Txn.application_args[3])],
        asset_amount=Int(0),
        fee=Int(0),
    ),
)

on_withdraw_algos = Seq(
    MakeInnerPaymentTxn(
        receiver=Global.creator_address(),
        amount=Balance(Global.current_application_address())
        - MinBalance(Global.current_application_address()),
        fee=Int(0),
    )
)

on_unstake = Seq(
    # NOTE: this also allows the creator to make 0-value transfers
    # to themseves with an arbitrary asset
    MakeInnerAssetTransferTxn(
        asset_receiver=Global.creator_address(),
        xfer_asset=Txn.assets[Btoi(Txn.application_args[1])],
        asset_amount=Btoi(Txn.application_args[2]),
        fee=Int(0),
    ),
    MakeInnerApplicationCallTxn(
        application_id=MASTER.get(),
        # Args:
        # [0]: ABI selector
        # [1]: (uint8) MicroStaking app ID will be accessible in applications[1]
        #           That's because at index 0 there will be Farm itself
        # [2]: (uint8) Microfarm's account is the caller to Farm, hence the index is 0
        # [3]: (uint8) User's account will be in accounts[1]
        # [4]: (uint8) Staked asset will be in assets[0], cause it's the only passed asset
        application_args=[
            MASTER_UPDATE,
            Bytes(b"\x01"),
            Bytes(b"\x00"),
            Bytes(b"\x01"),
            Bytes(b"\x00"),
        ],
        accounts=[Global.creator_address()],
        applications=[Global.current_application_id()],
        assets=[Txn.assets[Btoi(Txn.application_args[1])]],
        fee=Int(0),
    ),
)

on_send_message = Seq(
    # The ABI compliant string's first two bytes are the length of the string
    # so we need to cut them off
    MakeInnerTxn(
        type_enum=TxnType.Payment,
        receiver=Txn.accounts[Btoi(Txn.application_args[1])],
        fee=Int(0),
        note=Suffix(Txn.application_args[2], Int(2)),
    )
)

on_noop = Seq(
    If(Txn.application_id())
    .Then(
        Cond(
            [Txn.application_args[0] == EscrowMethods.UNSTAKE, on_unstake],
            [Txn.application_args[0] == EscrowMethods.WITHDRAW_ALGOS, on_withdraw_algos],
            [Txn.application_args[0] == EscrowMethods.SEND_MESSAGE, on_send_message],
        )
    )
    .Else(Seq(Assert(Txn.application_args[0] == EscrowMethods.CREATE), on_create)),
)

on_delete = Seq(
    Assert(Not(App.optedIn(Global.creator_address(), MASTER.get()))),
    MakeInnerAssetTransferTxn(
        xfer_asset=Txn.assets[0],
        asset_close_to=Global.creator_address(),
        fee=Int(0),
    ),
    MakeInnerPaymentTxn(
        close_remainder_to=Global.creator_address(),
        fee=Int(0),
    ),
)

program = Seq(
    Assert(Global.creator_address() == Txn.sender()),
    If(Txn.on_completion())
    .Then(If(Txn.on_completion() == OnComplete.DeleteApplication).Then(on_delete).Else(Err()))
    .Else(on_noop),
    Int(1),
)

COMPILED_MICRO_FARM = compileTeal(program, Mode.Application, version=8)
COMPILED_MICRO_FARM_CLEAR = compileTeal(Int(1), Mode.Application, version=8)

"""
The Escrow's husk is the initial code of the contract that will be deployed to the chain.
The contract is intended to be updated immediatelly by the Farm.
This is a workaround to make our contracts work with the ledger.
"""
# escrow_husk_ast = If(
#     Txn.application_id(),
#     Int(1),  # If the app is already created, allow anything
#     Seq(on_create, Int(1)),
# )
# COMPILED_ESCROW_HUSK = compileTeal(escrow_husk_ast, Mode.Application, version=8)
# COMPILED_ESCROW_HUSK_CLEAR = COMPILED_MICRO_FARM_CLEAR

# only the bytecode is exposed for maximum compatibility, see comment above to reproduce the result below
ESCROW_HUSK_BYTECODE = base64.b64decode(
    "CCACAAExGEAASYALTWFzdGVyQXBwSUQ2GgEXwDJnsYEGshA2GgIXwDKyGIAEtzVf0bIaIrIBs7GBBLIQMgqyFCKyEjYaAxfAMLIRIrIBsyNCAAEjQw=="
)
ESCROW_HUSK_CLEAR_BYTECODE = base64.b64decode("CIEBQw==")


class EscrowPrecompile(AppPrecompile):
    """Specialized AppPrecompile adapter for Escrow contract"""

    def __init__(self):
        self.approval = Precompile(COMPILED_MICRO_FARM)
        self.clear = Precompile(COMPILED_MICRO_FARM_CLEAR)

    def compile(self, client: AlgodClient):
        if self.approval._binary is None:
            self.approval.assemble(client)
        if self.clear._binary is None:
            self.clear.assemble(client)
