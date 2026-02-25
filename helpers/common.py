from typing import Iterable, Iterator

import algosdk
from pyteal import (
    Bytes,
    CompileOptions,
    Expr,
    Op,
    ScratchVar,
    TealBlock,
    TealOp,
    TealSimpleBlock,
    TealType,
)
from pyteal.types import require_type
from pytealext import assemble_steps


class Addw(Expr):
    """
    Addw calculates the expression m1 + m2,
    where m1 and m2 are TealType.uint64.
    The result of this operation is the carry-bit and the low-order 64 bits.
    """

    def __init__(
        self,
        m1: Expr,
        m2: Expr,
        result_high: ScratchVar = ScratchVar(),
        result_low: ScratchVar = ScratchVar(),
    ):
        """Calculate the m1 + m2

        Args:
            m1 (TealType.uint64): addend
            m2 (TealType.uint64): addend
            high (ScratchVar): carry-bit 64 bits
            low (ScratchVar): low-order 64 bits
        """
        super().__init__()
        # make sure that argument expressions have the correct return type
        require_type(m1, TealType.uint64)
        require_type(m2, TealType.uint64)
        self.m1 = m1
        self.m2 = m2
        self.high = result_high
        self.low = result_low

    def _get_steps(self) -> Iterator[Expr | TealOp]:
        yield self.m1
        yield self.m2
        yield TealOp(self, Op.addw)
        yield self.low.slot.store()
        yield self.high.slot.store()

    def __teal__(self, options: CompileOptions) -> tuple[TealBlock, TealSimpleBlock]:
        return assemble_steps(self._get_steps(), options)

    def __str__(self):
        return f"(Addw {self.m1} {self.m2})"

    def type_of(self):
        return TealType.none

    def has_return(self):
        return False


class Mulw(Expr):
    """
    Mulw calculates the expression m1 * m2,
    where m1 and m2 are TealType.uint64.
    The result of this operation is the carry-bit and the low-order 64 bits.
    """

    def __init__(
        self,
        m1: Expr,
        m2: Expr,
        result_high: ScratchVar = ScratchVar(),
        result_low: ScratchVar = ScratchVar(),
    ):
        """Calculate the m1 * m2

        Args:
            m1 (TealType.uint64): addend
            m2 (TealType.uint64): addend
            high (ScratchVar): carry-bit 64 bits
            low (ScratchVar): low-order 64 bits
        """
        super().__init__()
        # make sure that argument expressions have the correct return type
        require_type(m1, TealType.uint64)
        require_type(m2, TealType.uint64)
        self.m1 = m1
        self.m2 = m2
        self.high = result_high
        self.low = result_low

    def _get_steps(self) -> Iterator[Expr | TealOp]:
        yield self.m1
        yield self.m2
        yield TealOp(self, Op.mulw)
        yield self.low.slot.store()
        yield self.high.slot.store()

    def __teal__(self, options: CompileOptions) -> tuple[TealBlock, TealSimpleBlock]:
        return assemble_steps(self._get_steps(), options)

    def __str__(self):
        return f"(Mulw {self.m1} {self.m2})"

    def type_of(self):
        return TealType.none

    def has_return(self):
        return False


def to_uint_64_array(ints: Iterable[int]):
    return Bytes(b"".join([algosdk.abi.UintType(64).encode(value) for value in ints]))
