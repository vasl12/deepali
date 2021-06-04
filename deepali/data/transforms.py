r"""Data transforms.

Note that data transforms are included in the ``data`` package to avoid cyclical imports
between modules defining specialized tensor types (e.g., ``data.image``) and datasets
defined in ``data.dataset`` which also use these transforms to read and preprocess the
loaded data (c.f., ``ImageDataset``). The data transforms can also be imported from the
top-level ``transforms`` package instead of from ``data.transforms``.

"""

from pathlib import Path
from typing import List, Mapping, Optional, Sequence, Union

import torch
from torch import Tensor
from torch.nn import Module

from ..core.enum import PaddingMode, Sampling
from ..core.transforms import ItemTransform, ItemwiseTransform
from ..core.types import PathStr, ScalarOrTuple, Transform

from .image import Image


__all__ = (
    "AvgPoolImage",
    "CastImage",
    "CenterCropImage",
    "CenterPadImage",
    "ClampImage",
    "ImageToTensor",
    "NarrowImage",
    "NormalizeImage",
    "ReadImage",
    "ResampleImage",
    "RescaleImage",
    "ImageTransformConfig",
    "config_has_read_image_transform",
    "prepend_read_image_transform",
    "image_transform",
    "image_transforms",
)


class AvgPoolImage(ItemwiseTransform, Module):
    r"""Downsample image using average pooling."""

    def __init__(
        self,
        kernel_size: ScalarOrTuple[int],
        stride: Optional[ScalarOrTuple[int]] = None,
        padding: ScalarOrTuple[int] = 0,
        ceil_mode: bool = False,
        count_include_pad: bool = True,
    ) -> None:
        super().__init__()
        self.kernel_size = kernel_size
        self.stride = stride
        self.padding = padding
        self.ceil_mode = ceil_mode
        self.count_include_pad = count_include_pad

    def forward(self, data: Image) -> Image:
        if not isinstance(data, Image):
            raise TypeError(f"{type(self).__name__}() 'data' must be Image")
        return data.avg_pool(
            self.kernel_size,
            stride=self.stride,
            padding=self.padding,
            ceil_mode=self.ceil_mode,
            count_include_pad=self.count_include_pad,
        )

    def __repr__(self) -> str:
        return (
            type(self).__name__
            + f"(kernel_size={self.kernel_size!r},"
            + f" stride={self.stride!r},"
            + f" padding={self.padding!r},"
            + f" ceil_mode={self.ceil_mode},"
            + f" count_include_pad={self.count_include_pad})"
        )


class CastImage(ItemwiseTransform, Module):
    r"""Cast image data to specified type."""

    def __init__(self, dtype: torch.dtype) -> None:
        super().__init__()
        self.dtype = dtype

    def forward(self, data: Image) -> Image:
        if not isinstance(data, Image):
            raise TypeError(f"{type(self).__name__}() 'data' must be Image")
        return data.type(self.dtype)

    def __repr__(self) -> str:
        return type(self).__name__ + f"(dtype={self.dtype!r})"


class CenterCropImage(ItemwiseTransform, Module):
    r"""Crop image to specified maximum output size."""

    def __init__(self, size: Union[int, Sequence[int]]) -> None:
        super().__init__()
        self.size = size

    def forward(self, data: Image) -> Image:
        if not isinstance(data, Image):
            raise TypeError(f"{type(self).__name__}() 'data' must be Image")
        return data.center_crop(self.size)

    def __repr__(self) -> str:
        return type(self).__name__ + f"(size={self.size!r})"


class CenterPadImage(ItemwiseTransform, Module):
    r"""Pad image to specified minimum output size."""

    def __init__(
        self,
        size: Union[int, Sequence[int]],
        mode: Union[PaddingMode, str] = PaddingMode.CONSTANT,
        value: float = 0,
    ) -> None:
        super().__init__()
        self.size = size
        self.mode = PaddingMode(mode)
        self.value = float(value)

    def forward(self, data: Image) -> Image:
        if not isinstance(data, Image):
            raise TypeError(f"{type(self).__name__}() 'data' must be Image")
        return data.center_pad(self.size, mode=self.mode, value=self.value)

    def __repr__(self) -> str:
        return (
            type(self).__name__
            + f"(size={self.size!r}, mode={self.mode.value!r}, value={self.value!r})"
        )


class ClampImage(ItemwiseTransform, Module):
    r"""Clamp image intensities to specified minimum and/or maximum value."""

    def __init__(
        self, min: Optional[float] = None, max: Optional[float] = None, inplace: bool = False
    ) -> None:
        super().__init__()
        self.min = min
        self.max = max
        self.inplace = bool(inplace)

    def forward(self, data: Image) -> Image:
        if not isinstance(data, Image):
            raise TypeError(f"{type(self).__name__}() 'data' must be Image")
        clamp_fn = data.clamp_ if self.inplace else data.clamp
        data = clamp_fn(self.min, self.max)
        return data

    def __repr__(self) -> str:
        return (
            type(self).__name__ + f"(min={self.min!r}, max={self.max!r}, inplace={self.inplace!r})"
        )


class ImageToTensor(ItemwiseTransform, Module):
    r"""Convert image to data tensor."""

    def forward(self, data: Image) -> Tensor:
        if not isinstance(data, Image):
            raise TypeError(f"{type(self).__name__}() 'data' must be Image")
        return data.tensor()

    def __repr__(self) -> str:
        return type(self).__name__ + "()"


class NarrowImage(ItemwiseTransform, Module):
    r"""Return image with data tensor narrowed along specified dimension."""

    def __init__(self, dim: int, start: int, length: int = 1) -> None:
        super().__init__()
        if dim != 0:
            raise NotImplementedError(
                "NarrowImage() 'dim' must be zero at the moment."
                "Extend implementation to adjust image.grid() for other image dimensions."
            )
        self.dim = dim
        self.start = start
        self.length = length

    def forward(self, data: Image) -> Tensor:
        if not isinstance(data, Image):
            raise TypeError(f"{type(self).__name__}() 'data' must be Image")
        tensor = data.tensor().narrow(self.dim, self.start, self.length)
        return data.tensor_(tensor)

    def __repr__(self) -> str:
        return type(self).__name__ + "()"


class NormalizeImage(ItemwiseTransform, Module):
    r"""Normalize and clamp image intensities in [min, max]."""

    def __init__(
        self,
        min: Optional[float] = None,
        max: Optional[float] = None,
        mode: str = "unit",
        inplace: bool = False,
    ) -> None:
        super().__init__()
        if mode not in ("center", "unit", "zscore", "z-score"):
            raise ValueError("NormalizeImage() 'mode' must be 'center', 'unit', or 'zscore'")
        self.min = min
        self.max = max
        self.mode = mode
        self.inplace = inplace

    def forward(self, data: Image) -> Image:
        if not isinstance(data, Image):
            raise TypeError(f"{type(self).__name__}() 'data' must be Image")
        normalize_fn = data.normalize_ if self.inplace else data.normalize
        return normalize_fn(mode=self.mode, min=self.min, max=self.max)

    def __repr__(self) -> str:
        return (
            type(self).__name__
            + f"(mode={self.mode!r}, min={self.min!r}, max={self.max!r}, inplace={self.inplace!r})"
        )


class ReadImage(ItemwiseTransform, Module):
    r"""Read image data from file path."""

    def __init__(
        self,
        dtype: Optional[Union[torch.dtype, str]] = None,
        device: Optional[Union[str, torch.device]] = None,
    ) -> None:
        super().__init__()
        if isinstance(dtype, str):
            attr = dtype
            dtype = getattr(torch, attr, None)
            if dtype is None:
                raise ValueError(f"ReadImage() module torch has no 'dtype' named torch.{attr}")
        if dtype is not None and not isinstance(dtype, torch.dtype):
            raise TypeError("ReadImage() 'dtype' must by None or torch.dtype")
        self.dtype = dtype
        self.device = torch.device(device or "cpu")

    def forward(self, path: PathStr) -> Image:
        if not isinstance(path, (str, Path)):
            raise TypeError(f"{type(self).__name__}() 'path' must be Path or str")
        return Image.read(path, dtype=self.dtype, device=self.device)

    def __repr__(self) -> str:
        return type(self).__name__ + f"(dtype={self.dtype}, device='{self.device!s}')"


class ResampleImage(ItemwiseTransform, Module):
    r"""Resample image to specified voxel size."""

    def __init__(
        self,
        spacing: Union[float, Sequence[float], str],
        mode: Union[Sampling, str] = Sampling.LINEAR,
    ) -> None:
        super().__init__()
        self.spacing = spacing
        self.mode = Sampling(mode)

    def forward(self, data: Image) -> Image:
        if not isinstance(data, Image):
            raise TypeError(f"{type(self).__name__}() 'data' must be Image")
        return data.resample(self.spacing, mode=self.mode)

    def __repr__(self) -> str:
        return type(self).__name__ + f"(spacing={self.spacing!r}, mode={self.mode.value!r})"


class RescaleImage(ItemwiseTransform, Module):
    r"""Linearly rescale image data."""

    def __init__(
        self,
        min: Optional[float] = None,
        max: Optional[float] = None,
        mul: Optional[float] = None,
        add: Optional[float] = None,
        inplace: bool = False,
    ) -> None:
        super().__init__()
        if mul is not None or add is not None:
            if min is not None or max is not None:
                raise ValueError(
                    "RescaleImage() 'min'/'max' and 'add'/'mul' are mutually exclusive"
                )
            self.min = None
            self.max = None
            self.mul = 1 if mul is None else float(mul)
            self.add = 0 if add is None else float(add)
        else:
            self.min = min
            self.max = max
            self.mul = None
            self.add = None
        self.inplace = bool(inplace)

    def forward(self, data: Image) -> Image:
        if not isinstance(data, Image):
            raise TypeError(f"{type(self).__name__}() 'data' must be Image")
        if self.mul is not None or self.add is not None:
            assert self.min is None and self.max is None
            if self.mul != 1:
                mul_fn = data.mul_ if self.inplace else data.mul
                data = mul_fn(self.mul)
            if self.add != 0:
                add_fn = data.add_ if self.inplace else data.add
                data = add_fn(self.add)
        else:
            rescale_fn = data.rescale_ if self.inplace else data.rescale
            data = rescale_fn(min=self.min, max=self.max)
        return data

    def __repr__(self) -> str:
        s = type(self).__name__ + "("
        if self.mul is not None or self.add is not None:
            s += f"mul={self.mul!r}, add={self.add!r}"
        else:
            s += f"min={self.min!r}, max={self.max!r}"
        s += ", inplace={self.inplace!r})"
        return s


ImageTransformMapping = Mapping[str, Union[Sequence, Mapping]]
ImageTransformConfig = Union[
    str, ImageTransformMapping, Sequence[Union[str, ImageTransformMapping]]
]


IMAGE_TRANSFORM_TYPES = {
    "avgpool": AvgPoolImage,
    "cast": CastImage,
    "centercrop": CenterCropImage,
    "centerpad": CenterPadImage,
    "clamp": ClampImage,
    "narrow": NarrowImage,
    "normalize": NormalizeImage,
    "read": ReadImage,
    "rescale": RescaleImage,
    "resample": ResampleImage,
}

INPLACE_IMAGE_TRANSFORMS = {"clamp", "normalize", "rescale"}


def config_has_read_image_transform(config: ImageTransformConfig) -> bool:
    r"""Whether image data transformation configuration contains a "read" image transform."""
    if isinstance(config, str):
        return config.lower() == "read"
    if isinstance(config, Mapping):
        for name in config:
            if name.lower() == "read":
                return True
        return False
    if isinstance(config, Sequence):
        for item in config:
            if isinstance(item, Sequence) and not isinstance(item, str):
                raise ValueError(
                    "config_has_read_image_transform() 'config' Sequence cannot be nested"
                )
            if config_has_read_image_transform(item):
                return True
        return False
    raise TypeError("config_has_read_image_transform() 'config' must be str, Mapping, or Sequence")


def prepend_read_image_transform(
    config: ImageTransformConfig, dtype: Optional[str] = None, device: Optional[str] = None
) -> ImageTransformConfig:
    r"""Insert a "read" image transform before any other image data transform."""
    if config_has_read_image_transform(config):
        return config
    read_transform_config = {"read": dict(dtype=dtype, device=device)}
    if isinstance(config, str):
        return [read_transform_config, config]
    if isinstance(config, Mapping):
        return {**read_transform_config, **config}
    if isinstance(config, Sequence):
        return [read_transform_config] + list(config)
    raise TypeError("prepend_read_image_transform() 'config' must be str, Mapping, or Sequence")


def image_transform(
    name: str, *args, key: Optional[str] = None, inplace: bool = True, **kwargs
) -> ItemwiseTransform:
    r"""Create image data transform given its name."""
    cls = IMAGE_TRANSFORM_TYPES.get(name.lower())
    if cls is None:
        raise ValueError(f"image_transform() unknown image data transform '{name}'")
    if name in INPLACE_IMAGE_TRANSFORMS:
        kwargs["inplace"] = inplace
    transform = cls(*args, **kwargs)
    if key:
        transform = ItemTransform(transform, key=key)
    return transform


def image_transforms(config: ImageTransformConfig, key: Optional[str] = None) -> List[Transform]:
    r"""Create image data transforms from configuration."""
    transforms = []
    if isinstance(config, str):
        transforms.append(image_transform(config, key=key))
    elif isinstance(config, Mapping):
        for name, value in config.items():
            if value is None:
                transform = image_transform(name, key=key)
            elif isinstance(value, (list, tuple)):
                transform = image_transform(name, *value, key=key)
            elif isinstance(value, Mapping):
                transform = image_transform(name, key=key, **value)
            else:
                transform = image_transform(name, value, key=key)
            transforms.append(transform)
    elif isinstance(config, Sequence):
        for item in config:
            if isinstance(item, Sequence) and not isinstance(item, str):
                raise ValueError("image_transform() 'config' Sequence cannot be nested")
            transforms.extend(image_transforms(item, key=key))
    else:
        raise TypeError("image_transforms() 'config' must be str, Mapping, or Sequence")
    return transforms