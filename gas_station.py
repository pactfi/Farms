from beaker import Application, external
from pyteal import (
    Balance,
    Bytes,
    Expr,
    For,
    Global,
    Int,
    MinBalance,
    OnComplete,
    ScratchVar,
    Txn,
    abi,
)
from pytealext import MakeInnerApplicationCallTxn, MakeInnerPaymentTxn

from .helpers.transaction import DUMMY_APPROVAL_PROGRAM


class GasStationContract(Application):
    @external
    def increase_opcode_quota(self, count: abi.Uint64, fee_per_call: abi.Uint64) -> Expr:
        """Increase opcode quota in a given transaction group by roughly ({count}+1)*700."""
        i = ScratchVar()
        return For(i.store(count.get()), i.load(), i.store(i.load() - Int(1))).Do(
            MakeInnerApplicationCallTxn(
                approval_program=Bytes("base64", DUMMY_APPROVAL_PROGRAM),
                # Clear state doesn't matter, so we'll just use the same program
                clear_state_program=Bytes("base64", DUMMY_APPROVAL_PROGRAM),
                on_completion=OnComplete.DeleteApplication,
                fee=fee_per_call.get(),
            )
        )

    @external
    def withdraw(self) -> Expr:
        """Withdraw all funds from the contract."""
        addr = Global.current_application_address()
        return MakeInnerPaymentTxn(  # transfer algos from SSC controlled address to the caller
            receiver=Txn.sender(), amount=Balance(addr) - MinBalance(addr), fee=Int(0)
        )
