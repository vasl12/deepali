r"""Decorator for tensors of flow fields."""

from __future__ import annotations

from copy import copy as shallow_copy
from typing import Optional, Type, TypeVar, Union, overload

import torch
from torch import Tensor

from ..core.enum import PaddingMode, Sampling
from ..core import flow as U
from ..core.grid import Domain, Grid
from ..core.tensor import move_dim
from ..core.types import Array, Device, PathStr

from .image import Image, ImageBatch


TFlowField = TypeVar("TFlowField", bound="FlowField")
TFlowFields = TypeVar("TFlowFields", bound="FlowFields")


__all__ = ("FlowField", "FlowFields")


class FlowFields(ImageBatch):
    r"""Batch of flow fields."""

    __slots__ = ("_domain",)

    def __init__(
        self: TFlowFields,
        data: Union[Array, Image],
        grid: Optional[Grid] = None,
        domain: Optional[Domain] = None,
        device: Optional[Device] = None,
    ) -> None:
        r"""Initialize flow fields.

        Args:
            data: Batch data tensor of shape (N, C, ...X), where N is the batch size, and C must
                be equal the number of spatial dimensions. The order of the image channels
                must be such that vector components are in the order X, Y,...
            grid: Common flow field sampling grid. If not otherwise specified, this attribute
                defines the fixed target image domain on which to resample a moving source image.
            domain: Domain with respect to which vectors are defined. By default, it is assumed that
                vectors are with respect to the unit ``grid`` cube in ``[-1, 1]^D``, where D are the
                number of spatial dimensions. If ``grid.align_corners() == False``, the extrema
                ``(-1, 1)`` refer to the boundary of the vector field ``grid``. Otherwise, the
                extrema coincide with the corner points of the sampling grid.
            device: Device on which to store flow field ``data`` tensor.

        """
        if isinstance(data, Image):
            if grid is None:
                grid = data._grid
                data = data._tensor
        super().__init__(data=data, grid=grid, device=device)
        if self._tensor.shape[1] != self._tensor.ndim - 2:
            raise ValueError(
                f"FlowFields nchannels={self._tensor.shape[1]} must be equal spatial ndim={self._tensor.ndim - 2}"
            )
        if domain is None:
            domain = Domain.from_grid(self._grid[0])
        self._domain = Domain.from_arg(domain)

    def __getitem__(self: TFlowFields, index: int) -> FlowField:
        r"""Get flow field at specified batch index."""
        return FlowField(data=self._tensor[index], grid=self._grid[index])

    def tensor_(
        self: TFlowFields,
        data: Array,
        grid: Optional[Grid] = None,
        domain: Optional[Domain] = None,
        device: Optional[Device] = None,
    ) -> TFlowFields:
        r"""Change data tensor of this batch of flow fields."""
        super().tensor_(data, grid=grid, device=device)
        if domain is not None:
            self._domain = Domain.from_arg(domain)
        return self

    @overload
    def domain(self: TFlowFields) -> Domain:
        r"""Get domain with respect to which flow vectors are defined."""
        ...

    @overload
    def domain(self: TFlowFields, domain: Domain) -> TFlowFields:
        r"""Get new batch of flow fields with flow vectors defined with respect to specified domain."""
        ...

    def domain(self: TFlowFields, domain: Optional[Domain] = None) -> Union[Domain, TFlowFields]:
        r"""Rescale and reorient vectors."""
        if domain is None:
            return self._domain
        copy = shallow_copy(self)
        return copy.domain_(domain)

    def domain_(self: TFlowFields, domain: Domain) -> TFlowFields:
        r"""Rescale and reorient vectors of this vector field."""
        data = self.tensor()
        data = move_dim(data, 1, -1)
        data = tuple(
            g.transform_vectors(data[i : i + 1], domain=self._domain, codomain=domain)
            for i, g in enumerate(self._grid)
        )
        data = torch.cat(data, dim=0)
        data = move_dim(data, -1, 1)
        return self.tensor_(data, domain=domain)

    def curl(self: TFlowFields, mode: str = "central") -> ImageBatch:
        if self.ndim not in (2, 3):
            raise RuntimeError("Cannot compute curl of {self.ndim}-dimensional flow field")
        spacing = self.spacing()
        data = U.curl(self._tensor, spacing=spacing, mode=mode)
        return ImageBatch(data=data, grid=self._grid)

    def exp(self: TFlowFields, **kwargs) -> TFlowFields:
        r"""Group exponential maps of flow fields computed using scaling and squaring."""
        copy = shallow_copy(self)
        return copy.exp_(**kwargs)

    def exp_(self: TFlowFields, **kwargs) -> TFlowFields:
        r"""Group exponential maps of flow fields computed using scaling and squaring."""
        domain = self._domain
        align_corners = domain == Domain.CUBE_CORNERS
        self.domain_(Domain.CUBE_CORNERS if align_corners else Domain.CUBE)
        data = U.expv(self._tensor, align_corners=align_corners, **kwargs)
        self.tensor_(data, domain=self._domain)
        self.domain_(domain)  # restore original domain
        return self

    @overload
    def warp(
        self: TFlowFields,
        image: Image,
        sampling: Optional[Union[Sampling, str]] = None,
        padding: Optional[Union[PaddingMode, str]] = None,
    ) -> Union[Image, ImageBatch]:
        r"""Deform given image using this batch of vector fields."""
        ...

    @overload
    def warp(
        self: TFlowFields,
        image: ImageBatch,
        sampling: Optional[Union[Sampling, str]] = None,
        padding: Optional[Union[PaddingMode, str]] = None,
    ) -> ImageBatch:
        r"""Deform given image batch using this batch of vector fields."""
        ...

    def warp(
        self: TFlowFields,
        image: Union[Image, ImageBatch],
        sampling: Optional[Union[Sampling, str]] = None,
        padding: Optional[Union[PaddingMode, str]] = None,
    ) -> Union[Image, ImageBatch]:
        r"""Deform given image (batch) using this batch of vector fields.

        Args:
            image: Single image or image batch. If a single ``Image`` is given, it is deformed by
                all the displacement fields in this batch. If an ``ImageBatch`` is given, the number
                of images in the batch must match the number of displacement fields in this batch.
            sampling: Interpolation mode for sampling values from ``image`` at deformed grid points.
            padding: Extrapolation mode for sampling values outside ``image`` domain.

        Returns:
            Batch of input images deformed by the vector fields of this batch.

        """
        if isinstance(image, Image):
            image = image.batch()
        align_corners = self._domain == Domain.CUBE_CORNERS
        grid = (g.coords(align_corners=align_corners, device=self.device) for g in self._grid)
        grid = torch.cat(tuple(g.unsqueeze(0) for g in grid), dim=0)
        flow = self.domain(Domain.CUBE_CORNERS if align_corners else Domain.CUBE)
        flow = flow.tensor()
        flow = move_dim(flow, 1, -1)
        data = image.tensor()
        data = U.warp_image(
            data,
            grid,
            flow=flow,
            mode=sampling,
            padding=padding,
            align_corners=align_corners,
        )
        return image.tensor(data, grid=self._grid)


class FlowField(Image):
    r"""Flow field image.

    A (dense) flow field is a vector image where the number of channels equals the number of spatial dimensions.
    The starting points of the vectors are defined on a regular oriented sampling grid positioned in world space.
    Orientation and scale of the vectors are defined with respect to a specified regular grid domain, which either
    coincides with the sampling grid, the world coordinate system, or the unit cube with side length 2 centered at
    the center of the sampling grid with axes parallel to the sampling grid. This unit cube domain is used by the
    ``torch.nn.functional.grid_sample()`` and ``torch.nn.functional.interpolate()`` functions.

    When a flow field is convert to a ``SimpleITK.Image``, the vectors are by default reoriented and rescaled such
    that these are with respect to the world coordinate system, a format common to ITK functions and other toolkits.

    """

    __slots__ = ("_domain",)

    def __init__(
        self: TFlowField,
        data: Array,
        grid: Optional[Grid] = None,
        domain: Optional[Domain] = None,
        device: Optional[Device] = None,
    ) -> None:
        r"""Initialize flow field.

        Args:
            data: Flow field data tensor of shape (C, ...X), where C must be equal the number of spatial dimensions.
                The order of the image channels must be such that vector components are in the order X, Y,...
            grid: Flow field sampling grid. If not otherwise specified, this attribute often also defines the fixed
                target image domain on which to resample a moving source image.
            domain: Domain with respect to which vectors are defined. By default, it is assumed that vectors are with
                respect to the unit ``grid`` cube in ``[-1, 1]^D``, where D are the number of spatial dimensions.
                If ``None`` and ``grid.align_corners() == False``, the extrema ``(-1, 1)`` refer to the boundary of
                the vector field ``grid``, and coincide with the grid corner points otherwise.
            device: Device on which to store flow field ``data`` tensor.

        """
        super().__init__(data=data, grid=grid, device=device)
        if self.nchannels != self._grid.ndim:
            raise ValueError(
                f"FlowField nchannels={self.nchannels} must be equal grid.ndim={self._grid.ndim}"
            )
        if domain is None:
            domain = Domain.from_grid(self._grid)
        self._domain = Domain.from_arg(domain)

    @classmethod
    def from_image(
        cls: Type[TFlowField], image: Image, domain: Optional[Domain] = None
    ) -> TFlowField:
        r"""Create flow field from image instance."""
        return cls(image._tensor, grid=image._grid, domain=domain)

    def batch(self: TFlowField) -> FlowFields:
        r"""Batch of flow fields containing only this flow field."""
        data: Tensor = self._tensor.rename(None)
        return FlowFields(data=data.unsqueeze(0), grid=self._grid, domain=self._domain)

    def tensor_(
        self: TFlowField,
        data: Array,
        grid: Optional[Grid] = None,
        domain: Optional[Domain] = None,
        **kwargs,
    ) -> TFlowField:
        r"""Change data tensor of this vector field."""
        super().tensor_(data, grid=grid, **kwargs)
        if domain is not None:
            self._domain = Domain.from_arg(domain)
        return self

    @overload
    def domain(self: TFlowField) -> Domain:
        r"""Get domain with respect to which flow vectors are defined."""
        ...

    @overload
    def domain(self: TFlowField, domain: Domain) -> TFlowField:
        r"""Get new flow field with flow vectors defined with respect to specified domain."""
        ...

    def domain(self: TFlowField, domain: Optional[Domain] = None) -> Union[Domain, TFlowField]:
        r"""Rescale and reorient vectors with respect to specified domain."""
        if domain is None:
            return self._domain
        copy = shallow_copy(self)
        return copy.domain_(domain)

    def domain_(self: TFlowField, domain: Domain) -> TFlowField:
        r"""Rescale and reorient vectors of this vector field with respect to specified domain."""
        batch = self.batch()
        batch.domain_(domain)
        tensor = batch.tensor()
        self._tensor = tensor.squeeze(0)
        self._domain = batch.domain()
        return self

    @classmethod
    def from_sitk(
        cls: Type[TFlowField], image: sitk.Image, domain: Optional[Domain] = None
    ) -> TFlowField:
        r"""Create vector field from ``SimpleITK.Image``."""
        image = super().from_sitk(image)
        return cls.from_image(image, domain=domain or Domain.WORLD)

    def sitk(self: TFlowField, domain: Optional[Domain] = None) -> sitk.Image:
        r"""Create ``SimpleITK.Image`` from this vector field."""
        disp = self.detach()
        disp = disp.domain(domain or Domain.WORLD)
        return Image.sitk(disp)

    @classmethod
    def read(cls: Type[TFlowField], path: PathStr, domain: Optional[Domain] = None) -> TFlowField:
        r"""Read image data from file."""
        image = cls._read_sitk(path)
        return cls.from_sitk(image, domain=domain)

    def exp(self: TFlowField, **kwargs) -> TFlowField:
        r"""Group exponential map of vector field computed using scaling and squaring."""
        batch = self.batch()
        flow = batch.exp(**kwargs)[0]
        cls: FlowField = type(self)
        return cls.from_image(flow)

    def curl(self: TFlowField, **kwargs) -> Image:
        r"""Compute curl of vector field."""
        batch = self.batch()
        rotvec = batch.curl(**kwargs)
        return rotvec[0]

    @overload
    def warp(self: TFlowField, image: Image, **kwargs) -> Image:
        r"""Deform given image using this displacement field."""
        ...

    @overload
    def warp(self: TFlowField, image: ImageBatch, **kwargs) -> ImageBatch:
        r"""Deform images in batch using this displacement field."""
        ...

    def warp(
        self: TFlowField, image: Union[Image, ImageBatch], **kwargs
    ) -> Union[Image, ImageBatch]:
        r"""Deform given image (batch) using this displacement field.

        Args:
            image: Single image or batch of images.
            kwargs: Keyword arguments to pass on to ``ImageBatch.warp()``.

        Returns:
            If ``image`` is an ``ImageBatch``, each image in the batch is deformed by this flow field
            and a batch of deformed images is returned. Otherwise, a single deformed image is returned.

        """
        batch = self.batch()
        result = batch.warp(image, **kwargs)
        if isinstance(image, Image) and len(result) == 1:
            return result[0]
        return result
