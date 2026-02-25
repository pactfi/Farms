from dataclasses import dataclass
from typing import Union, cast

import algosdk
from pyteal import Btoi, Bytes, Expr, Extract, Int, Suffix, Txn, TxnArray, TxnObject


def abi_extract_string_value(abi_string: Expr) -> Expr:
    """
    Extracts string value.

    Takes suffix starting at index 2 to ignore string length data.
    """
    return Suffix(abi_string, Int(2))


def abi_extract_length_from_vector(bytes_vector: Expr) -> Expr:
    """
    Extract length of bytes vector.

    Converts first to 2 bytes of verctor to uint64.
    """
    return Btoi(Extract(bytes_vector, Int(0), Int(2)))


def abi_extract_uint64_from_vector(bytes_vector: Expr, position: Union[int, Expr]) -> Expr:
    """
    Extract uint64 with index i from byte-encoded uint64[] vector.
    No need to check bounds, because extract opcode will do that.

    ABI encoding of uint64 vector (variable length array) is as follows:
    | Length (2B) | Value[0] (8B) | Value[1] (8B) | ... | Value[n-1] (8B) |
    """
    if isinstance(position, int):
        # with pyteal 0.10, in this case Extract will be very efficient
        position = Int(position * 8 + 2)
    else:
        position = position * Int(8) + Int(2)
    # position is guaranteed to be an Expr
    position = cast(Expr, position)

    return Extract(bytes_vector, position, Int(8))


def abi_make_uint8(value: int) -> Bytes:
    """Create a bytes representation of a uint8 value

    Guaranteed to use a single opcode, but takes up extra space in bytecblock
    """
    return Bytes(algosdk.abi.UintType(8).encode(value))


@dataclass
class SwapArguments:
    source_asset: Expr
    target_asset: Expr
    app_id: Expr
    account: Expr
    interface: Expr


def extract_swap_arguments(
    app_args: TxnArray, position: Union[int, Expr], txn: TxnObject = Txn
) -> SwapArguments:
    """
    Extracts arguments from txn arguments for swap starting at index `position` of `app_args`.
    Packs arguments to `SwapArgument` class.

    SWAP router method signature ensures all arguments presence.
    """
    if isinstance(position, int):
        position = Int(position)

    return SwapArguments(
        source_asset=txn.assets[Btoi(app_args[position])],
        target_asset=txn.assets[Btoi(app_args[position + Int(1)])],
        app_id=txn.applications[Btoi(app_args[position + Int(2)])],
        account=txn.accounts[Btoi(app_args[position + Int(3)])],
        interface=abi_extract_string_value(app_args[position + Int(4)]),
    )


def extract_first_swap(app_args: TxnArray, txn: TxnObject = Txn) -> SwapArguments:
    """
    Extracts first swap from `app_args`.

    Assumes based on SWAP method signature SWAP has 5 arguments.
    """
    return extract_swap_arguments(app_args, Int(1), txn)


def extract_last_swap(app_args: TxnArray, txn: TxnObject = Txn) -> SwapArguments:
    """
    Extracts last swap from `app_args`.

    Assumes based on SWAP method signature SWAP has 5 arguments
    and at the end there is minimum expected amount.
    """
    return extract_swap_arguments(app_args, app_args.length() - Int(6), txn)


def extract_minimum_expected(app_args: TxnArray) -> Expr:
    """
    Extracts minimum expected from SWAP app call application args.

    Takes last agrument in `app_args` and converts to uint64.
    """
    return Btoi(app_args[app_args.length() - Int(1)])
