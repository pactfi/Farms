"""Fixed point arithmetics on numbers with 64 bit precision."""


from pyteal import (
    Btoi,
    Bytes,
    BytesAdd,
    BytesDiv,
    BytesMinus,
    BytesMul,
    BytesZero,
    Concat,
    Expr,
    Extract,
    If,
    Int,
    Itob,
    Len,
    Seq,
    Subroutine,
    TealType,
)

PRECISION = 2**64


def _to_big_int(val: int) -> Bytes:
    """
    Converts a python integer to a an expression evaluating to a big endian integer encoded as bytes.
    """
    return Bytes(val.to_bytes((val.bit_length() + 7) // 8, byteorder="big"))


ZERO = Bytes(b"")  # the smallest representation of 0
ONE = _to_big_int(PRECISION)  # one and 64 binary zeros


@Subroutine(TealType.bytes)
def _truncate_8(value: Expr) -> Expr:
    """
    Trim 8 bytes from the end of `value`.

    If `value` is shorter than `tail_length`, "" (empty byte string) is returned.

    This saves about 4 operations compared to bytes division by 2**64.

    Args:
        value: The byte string to truncate
    """
    return Seq(
        #
        If(Len(value) <= Int(8))
        .Then(ZERO)
        .Else(Extract(value, Int(0), Len(value) - Int(8)))
    )


def _right_pad_8(value: Expr) -> Expr:
    """Append 8 bytes after the end of value.

    Expands an integer into a 64-bit precision fixed point number
    """
    return Concat(value, BytesZero(Int(8)))


@Subroutine(TealType.bytes)
def from_int(val: Expr) -> Expr:
    """Convert an integer to a fixed point number."""
    return _right_pad_8(Itob(val))


def to_int(val: Expr) -> Expr:
    """Convert a fixed point number to an integer by truncating the fractional part."""
    return Btoi(_truncate_8(val))


def from_big_int(val: Expr) -> Expr:
    """Convert a big integer to a fixed point number."""
    return _right_pad_8(val)


def mul(lhs: Expr, rhs: Expr) -> Expr:
    """Multiply two fixed point numbers

    Args:
        lhs: The first factor
        rhs: The second factor
    """
    return _truncate_8(BytesMul(lhs, rhs))


@Subroutine(TealType.bytes)
def div(lhs: Expr, rhs: Expr) -> Expr:
    """Divide two fixed point numbers.
    Firstly, the dividend is expanded by 64 bits.

    Args:
        lhs: The dividend
        rhs: The divisor
    """
    return BytesDiv(_right_pad_8(lhs), rhs)


def add(lhs: Expr, rhs: Expr) -> Expr:
    """Add two fixed point numbers

    Args:
        lhs: The first addend
        rhs: The second addend
    """
    return BytesAdd(lhs, rhs)


def sub(lhs: Expr, rhs: Expr) -> Expr:
    """Subtract two fixed point numbers

    Args:
        lhs: The minuend
        rhs: The subtrahend
    """
    return BytesMinus(lhs, rhs)
