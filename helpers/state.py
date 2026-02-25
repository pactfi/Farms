"""State variables and State Manager"""


from abc import ABC, abstractmethod
from typing import Any, Iterable, Optional

from pyteal import (
    App,
    Assert,
    Bytes,
    Concat,
    Expr,
    Extract,
    ExtractUint64,
    Int,
    Itob,
    Len,
    ScratchVar,
    Seq,
    Subroutine,
    TealType,
)
from pytealext.state import State, StateArray


@Subroutine(TealType.anytype)
def app_global_get_ex_safe(
    app_id: Expr,
    key: Expr,
):
    """Safe wrapper around App.globalGetEx"""
    return Seq(
        (res := App.globalGetEx(app_id, key)),
        Assert(res.hasValue()),
        res.value(),
    )


class CachedStateVariable(ABC):
    """
    Class representing a fixed-length variable that will be cached during runtime
    and stored only at the end of operation.
    The stored variables are compacted together with other CachedStateVariables.
    They need to be a part of a schema and don't do anything on their own
    """

    def __init__(self, storage_type: TealType, slot_id: Optional[int] = None):
        self._cache = ScratchVar(storage_type, slot_id)

    def put(self, val: Expr) -> Expr:
        """Write new value to this variable"""
        return self._cache.store(val)

    def get(self) -> Expr:
        """Get the contents of this variable"""
        return self._cache.load()

    @abstractmethod
    def serialize(self) -> Expr:
        """Serialize this value to bytes"""

    @abstractmethod
    def deserialize(self, val: Expr, offset: Expr) -> Expr:
        """Deserialize this value from bytes.

        Args:
            val: The source string containing the bytes. Must evaluate to bytes.
            offset: The index of the first byte to deserialize.
                Must evaluate to an integer less than Len(val).
        """

    @abstractmethod
    def length(self) -> int:
        """Get the length of this value in bytes"""

    def get_storage_schema(self) -> dict[str, Any]:
        """Get the storage schema for this variable"""
        return {
            "type": type(self).__name__,
            "length": self.length(),
        }


class CachedUInt(CachedStateVariable):
    """Mimicks an integer in the global state, must be serialized and saved."""

    def __init__(self):
        super().__init__(TealType.uint64)

    def serialize(self) -> Expr:
        """Serialize this value to bytes"""
        return Itob(self._cache.load())

    def deserialize(self, val: Expr, offset: Expr) -> Expr:
        """Deserialize this value from bytes"""
        return ExtractUint64(val, offset)

    def length(self) -> int:
        return 8


class CachedFixedBytes(CachedStateVariable):
    """Mimicks a fixed-length byte string in the global state."""

    def __init__(self, length: int) -> None:
        super().__init__(TealType.bytes)
        self._length = length

    def serialize(self) -> Expr:
        """Serialize this value to bytes

        Performs a runtime check to validate that the length of the serialized value is equal
        to the expected length.

        Returns:
            (TealType.bytes): the cached value (unchanged)
        """
        return Seq(
            Assert(Len(self._cache.load()) == Int(self._length)),
            self._cache.load(),
        )

    def deserialize(self, val: Expr, offset: Expr) -> Expr:
        return Extract(val, offset, Int(self._length))

    def length(self) -> int:
        return self._length


class CachedAddress(CachedFixedBytes):
    """32 bytes variable pretending to be a GlobalState variable"""

    def __init__(self) -> None:
        super().__init__(32)


class Schema(ABC):
    """Compact multiple global state integers into a single bytes slot"""

    def __init__(self, storage: State) -> None:
        if not isinstance(storage, State):
            raise TypeError(f"storage must be an instance of State, actual: {type(storage)}")
        self._storage = storage

    def _get_vars(self) -> Iterable[tuple[str, CachedStateVariable]]:
        members = vars(self).items()
        members = filter(lambda m: isinstance(m[1], CachedStateVariable), members)  # type: ignore
        return members

    @abstractmethod
    def initialize(self) -> Expr:
        """Initialize the global state value by setting default values"""

    def store(self) -> Expr:
        """Serialize members and saves them to the provided slot in the global state"""
        serialized = map(lambda u: u[1].serialize(), self._get_vars())
        return self._storage.put(Concat(*serialized))

    def load(self) -> Expr:
        """Load members from the provided global state slot"""
        vs = self._get_vars()
        raw = ScratchVar(TealType.bytes)
        offset = 0
        exprs = [raw.store(self._storage.get())]
        for _, v in vs:
            exprs.append(v.put(v.deserialize(raw.load(), Int(offset))))
            offset += v.length()

        return Seq(*exprs)

    def get_storage_schema(self) -> dict[str, Any]:
        """Get the storage definition"""
        # get the storage schema for each cached variable
        member_schemas = dict(map(lambda v: (v[0], v[1].get_storage_schema()), self._get_vars()))
        offset = 0
        for schema in member_schemas.values():
            schema["offset"] = offset
            offset += schema["length"]

        return {
            "type": "Schema",
            "members": dict(member_schemas),
        }


def _extract_key_from_state(state: State) -> str:
    # This will only work if State was created with str input as key
    if isinstance(state._name, Bytes):  # pylint: disable=protected-access
        key: str = state._name.byte_str  # pylint: disable=protected-access
        # strip leading and trailing quotes added by escapeStr
        return key[1:-1]
    raise ValueError("State has a dynamic name (generated by a pyteal expression)")


class StateManager(ABC):
    """Abstraction layer for application's global and local state."""

    @classmethod
    def _get_schemas(cls) -> Iterable[tuple[str, Schema]]:
        schemas = filter(lambda v: isinstance(v[1], Schema), vars(cls).items())
        return schemas

    @classmethod
    def store(cls) -> Expr:
        """Finalize all state changes by writing them to the global state"""
        store_exprs = map(lambda schema: schema[1].store(), cls._get_schemas())

        return Seq(*store_exprs)

    @classmethod
    def load(cls) -> Expr:
        """Load compacted state variables into slots"""
        load_exprs = map(lambda schema: schema[1].load(), cls._get_schemas())

        return Seq(*load_exprs)

    @classmethod
    def get_storage_schema(cls) -> dict[str, dict[str, Any]]:
        """Get the schema describing the utilization of the global or local state

        The following supported member types (as well as their subclasses) will be encoded:
            - Schema
            - pytealext.State
            - pytealext.StateArray

        Generated schema should look like this:
        ```
        {
            "config": {"type": "State"},
            "balances": {"type": "StateArray"},
            "amp": {
                "type": "Schema",
                "members": {
                    "val": {"type": "CachedUInt", "length": 8, "offset": 0},
                    "addr": {"type": "CachedAddress", "length": 32, "offset": 8},
                },
            }
        }
        ```
        """
        members = vars(cls).items()
        # get pytealext state & state arrays
        pytealext_state = filter(
            lambda m: isinstance(m[1], State), members
        )  # type: Iterable[tuple[str,State]]
        pytealext_state_arrays = filter(
            lambda m: isinstance(m[1], StateArray), members
        )  # type: Iterable[tuple[str,StateArray]]
        schemas = filter(
            lambda m: isinstance(m[1], Schema), members
        )  # type: Iterable[tuple[str,Schema]]

        result = {}  # type: dict[str, dict[str, Any]]
        for name, state in pytealext_state:
            key = _extract_key_from_state(state)
            if key in result:
                raise ValueError(f"State {key=} is defined multiple times")
            result[key] = {"type": "State"}

        for name, state_array in pytealext_state_arrays:
            key = state_array._prefix  # pylint: disable=protected-access
            if not isinstance(key, str):
                raise ValueError(
                    f"Member StateArray {name} has a dynamic name"
                    " (generated by a pyteal expression)"
                )
            if key in result:
                raise ValueError(f"State {key=} is defined multiple times")
            result[key] = {"type": "StateArray"}

        for name, schema in schemas:
            key = _extract_key_from_state(schema._storage)  # pylint: disable=protected-access
            if key in result:
                raise ValueError(f"State {key=} is defined multiple times")
            result[key] = schema.get_storage_schema()

        return result

    @classmethod
    @abstractmethod
    def initialize(cls) -> Expr:
        """Initialize state by setting default values"""
