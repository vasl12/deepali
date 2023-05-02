r"""Type annotations for torch functions."""

from dataclasses import Field
from pathlib import Path
import re
from typing import Any, Dict, List, Mapping, NamedTuple
from typing import Optional, Sequence, Tuple, TypeVar, Union
from typing_extensions import Protocol

import torch

from torch import Tensor

RE_OUTPUT_KEY_INDEX = re.compile(r"\[([0-9]+)\]")


T = TypeVar("T")
ScalarOrTuple = Union[T, Tuple[T, ...]]
ScalarOrTuple1d = Union[T, Tuple[T]]
ScalarOrTuple2d = Union[T, Tuple[T, T]]
ScalarOrTuple3d = Union[T, Tuple[T, T, T]]
ScalarOrTuple4d = Union[T, Tuple[T, T, T, T]]
ScalarOrTuple5d = Union[T, Tuple[T, T, T, T, T]]
ScalarOrTuple6d = Union[T, Tuple[T, T, T, T, T, T]]

# Cannot use Sequence type annotation when using torch.jit.ScriptModule
ListOrTuple = Union[List[T], Tuple[T, ...]]

Device = torch.device  # Union[torch.device, str], PyTorch uses Optional[torch.device] annotation
DType = torch.dtype
Name = Optional[str]  # Optional tensor dimension name
Size = ScalarOrTuple[int]  # Order of spatial dimensions: (X, ...)
Shape = ScalarOrTuple[int]  # Order of spatial dimensions: (..., X)
Scalar = Union[int, float, Tensor]
Array = Union[Sequence[Scalar], Tensor]

PathStr = Union[Path, str]


class Dataclass(Protocol):
    r"""Type annotation for any dataclass."""

    __dataclass_fields__: Dict[str, Any]


# While a Batch and Sample have the same type, the difference is in their values
# - Sample: Values must be without batch dimension if tensors/arrays, but can also be of other
#           types such as a file Path or SimpleITK.Image, or None if optional.
# - Batch: Values must be tensors with batch dimension (first dimension) or None if optional.
Batch = Union[Dataclass, Dict[str, Any], NamedTuple]
Sample = Union[Dataclass, Dict[str, Any], NamedTuple]

TensorMapOrSequence = Union[Mapping[str, Tensor], Sequence[Tensor]]

TensorCollection = Union[
    TensorMapOrSequence,
    Mapping[str, TensorMapOrSequence],
    Sequence[TensorMapOrSequence],
]


def tensor_collection_entry(output: TensorCollection, key: str) -> Union[TensorCollection, Tensor]:
    r"""Get specified output entry."""
    key = RE_OUTPUT_KEY_INDEX.sub(r".\1", key)
    for index in key.split("."):
        if isinstance(output, (list, tuple)):
            try:
                index = int(index)
            except TypeError:
                raise KeyError(f"invalid output key {key}")
        elif not index or not isinstance(output, dict):
            raise KeyError(f"invalid output key {key}")
        output = output[index]
    return output


def get_tensor(output: TensorCollection, key: str) -> Tensor:
    r"""Get tensor at specified output entry."""
    item = tensor_collection_entry(output, key)
    if not isinstance(item, Tensor):
        raise TypeError(f"get_output_tensor() entry {key} must be Tensor")
    return item


def is_bool_dtype(dtype: DType) -> bool:
    r"""Checks if ``dtype`` of given NumPy array or PyTorch tensor is boolean type."""
    return dtype in (torch.bool,)


def is_float_dtype(dtype: DType) -> bool:
    r"""Checks if ``dtype`` of given tensor is a floating point type."""
    return dtype in (torch.float16, torch.float32, torch.float64)


def is_int_dtype(dtype: DType) -> bool:
    r"""Checks if ``dtype`` of given tensor is a signed integer type."""
    return dtype in (torch.int8, torch.int16, torch.int32, torch.int64)


def is_uint_dtype(dtype: DType) -> bool:
    r"""Checks if ``dtype`` of given tensor is an unsigned integer type."""
    return dtype in (torch.uint8,)


def is_namedtuple(arg: Any) -> bool:
    r"""Check if given object is a named tuple."""
    return isinstance(arg, tuple) and hasattr(arg, "_fields")


def is_optional_field(field: Field) -> bool:
    r"""Whether given dataclass field type is ``Optional[T] = Union[T, NoneType]``."""
    return is_optional_type_hint(field.type)


def is_optional_type_hint(type_hint: Any) -> bool:
    r"""Whether given type hint is ``Optional[T] = Union[T, NoneType]``."""
    type_origin = getattr(type_hint, "__origin__", None)
    if type_origin is Union:
        return type(None) in type_hint.__args__


def is_path_str(arg: Any) -> bool:
    r"""Whether given object is of type ``pathlib.Path`` or ``str`."""
    return isinstance(arg, (Path, str))


def is_path_str_type_hint(type_hint: Any, required: bool = False) -> bool:
    r"""Check if given type annotation is ``pathlib.Path``, ``PathStr = Union[pathlib.Path, str]``.

    Args:
        type_hint: Type annotation, e.g., ``dataclasses.Field.type``.
        required: Whether path argument is required. If ``False``, ``type(None)`` in the
            type hint is ignore, i.e., also ``Optional[T]`` is considered valid.

    Returns:
        Whether type hint is ``pathlib.Path``, ``Union[pathlib.Path, str]``, or
        ``Union[str, pathlib.Path]``. When ``required=False``, type annotations
        ``Optional[T] = Union[T, None]`` where ``T`` is one of the aforementioned
        path string types also results in a return value of ``True``.

    """
    if type_hint in (Path, "Path", "Optional[Path]", "PathStr", "Optional[PathStr]"):
        return True
    type_origin = getattr(type_hint, "__origin__", None)
    if type_origin is Union:
        type_args = set(type_hint.__args__)
        if not required:
            type_args.discard(type(None))
            type_args.discard("None")
        type_args.discard(str)
        type_args.discard("str")
        if not type_args:
            return False
        for type_arg in type_args:
            if type_arg not in (Path, "Path", "PathStr"):
                return False
        return True
    return False


def is_path_str_field(field: Field, required: bool = False) -> bool:
    r"""Check if given dataclass field type is ``pathlib.Path``, ``PathStr = Union[pathlib.Path, str]``."""
    return is_path_str_type_hint(field.type, required=required)
