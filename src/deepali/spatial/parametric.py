r"""Mix-ins for spatial transformations that have (optimizable) parameters."""

from __future__ import annotations

from copy import copy as shallow_copy
from typing import Callable, Optional, Union, cast, overload

import torch
from torch import Tensor
from torch.nn import Parameter, init

from ..core.grid import Grid

from .base import ReadOnlyParameters, TSpatialTransform


class ParametricTransform:
    r"""Mix-in for spatial transformations that have (optimizable) parameters.

    This mix-in adds property 'params' to a SpatialTransform class, which can be either one
    of the following. In addition, functional setter and getter functions are added. These
    functions check the type and shape of its arguments.

    - ``None``: Can be specified when initializing a spatial transformation whose parameters
        will be set at a later time point, e.g., to the output of a neural network. An exception
        is raised by functions which attempt to access yet uninitialized transformation parameters.
    - ``Parameter``: Tensor of optimizable parameters, e.g., for classic registration.
        To temporarily disable optimization of the parameters, set ``params.requires_grad = False``.
    - ``Tensor``: Tensor of fixed parameters which are thus not be returned by ``Module.parameters()``.
    - ``Callable``: A callable such as a function or ``torch.nn.Module``. Function ``update()``, which is
        registered as pre-forward hook, invokes this callable to obtain the current transformation parameters
        with arguments set and obtained by ``SpatialTransform.condition()``. For example, an input batch of
        a neural network can be passed to a ``torch.nn.Module`` this way to infer parameters from this input.

    """

    def __init__(
        self: Union[TSpatialTransform, ParametricTransform],
        grid: Grid,
        groups: Optional[int] = None,
        params: Optional[Union[bool, Tensor, Callable]] = True,
    ) -> None:
        r"""Initialize transformation parameters.

        Args:
            grid: Grid domain on which transformation is defined.
            groups: Number of transformations. A given image batch can either be deformed by a
                single transformation, or a separate transformation for each image in the batch, e.g.,
                for group-wise or batched registration. The default is one transformation for all images
                in the batch, or the batch length of the ``params`` tensor if provided.
            params: Initial parameters. If a tensor is given, it is only registered as optimizable module
                parameters when of type ``torch.nn.Parameter``. When a callable is given instead, it will be
                called by ``self.update()`` with ``SpatialTransform.condition()`` arguments. When a boolean
                argument is given, a new zero-initialized tensor is created. If ``True``, this tensor is
                registered as optimizable module parameter. If ``None``, parameters must be set using
                ``self.data()`` or ``self.data_()`` before this transformation is evaluated.

        """
        if isinstance(params, Tensor) and params.ndim < 2:
            raise ValueError(
                f"{type(self).__name__}() 'params' tensor must be at least 2-dimensional"
            )
        super().__init__(grid)
        if groups is None:
            groups = params.shape[0] if isinstance(params, Tensor) else 1
        shape = (groups,) + self.data_shape
        if params is None:
            self.params = None
        elif isinstance(params, bool):
            data = torch.empty(shape, dtype=torch.float)
            if params:
                self.params = Parameter(data)
            else:
                self.register_buffer("params", data, persistent=True)
            self.reset_parameters()
        elif isinstance(params, Tensor):
            if shape and params.shape != shape:
                raise ValueError(
                    f"{type(self).__name__}() 'params' must be tensor of shape {shape!r}"
                )
            if isinstance(params, Parameter):
                self.params = params
            else:
                self.register_buffer("params", params, persistent=True)
        elif callable(params):
            self.params = params
            self.register_buffer("p", torch.empty(shape), persistent=False)
            self.reset_parameters()
        else:
            raise TypeError(
                f"{type(self).__name__}() 'params' must be bool, Callable, Tensor, or None"
            )

    def has_parameters(self) -> bool:
        r"""Whether this transformation has optimizable parameters."""
        return isinstance(self.params, Parameter)

    @torch.no_grad()
    def reset_parameters(self: Union[TSpatialTransform, ParametricTransform]) -> None:
        r"""Reset transformation parameters."""
        params = self.params  # Note: May be None!
        if params is None:
            return
        if callable(params):
            params = self.p
        init.constant_(params, 0.0)
        self.clear_buffers()

    @property
    def data_shape(self) -> torch.Size:
        r"""Get required shape of transformation parameters tensor, excluding batch dimension."""
        raise NotImplementedError(f"{type(self).__name__}.data_shape")

    @overload
    def data(self) -> Tensor:
        r"""Get (buffered) transformation parameters."""
        ...

    @overload
    def data(self: TSpatialTransform, arg: Tensor) -> TSpatialTransform:
        r"""Get shallow copy with specified parameters."""
        ...

    def data(self: Union[TSpatialTransform, ParametricTransform], arg: Optional[Tensor] = None) -> Union[TSpatialTransform, Tensor]:
        r"""Get transformation parameters or shallow copy with specified parameters, respectively."""
        params = self.params  # Note: May be None!
        if arg is None:
            if params is None:
                raise AssertionError(f"{type(self).__name__}.data() 'params' must be set first")
            if callable(params):
                params = getattr(self, "p")
            return params
        if not isinstance(arg, Tensor):
            raise TypeError(f"{type(self).__name__}.data() 'arg' must be tensor")
        shape = self.data_shape
        if arg.ndim != len(shape) + 1:
            raise ValueError(
                f"{type(self).__name__}.data() 'arg' must be {len(shape) + 1}-dimensional tensor"
            )
        shape = (arg.shape[0],) + shape
        if arg.shape != shape:
            raise ValueError(f"{type(self).__name__}.data() 'arg' must have shape {shape!r}")
        copy = shallow_copy(self)
        if callable(params):
            delattr(copy, "p")
        if isinstance(params, Parameter) and not isinstance(arg, Parameter):
            copy.params = Parameter(arg, params.requires_grad)
        else:
            copy.params = arg
        copy.clear_buffers()
        return copy

    def data_(
        self: Union[TSpatialTransform, ParametricTransform], arg: Tensor
    ) -> TSpatialTransform:
        r"""Replace transformation parameters.

        Args:
            arg: Tensor of transformation parameters with shape matching ``self.data_shape``,
                excluding the batch dimension whose size may be different from the current tensor.

        Returns:
            Reference to this in-place modified transformation module.

        Raises:
            ReadOnlyParameters: When ``self.params`` is a callable which provides the parameters.

        """
        params = self.params  # Note: May be None!
        if callable(params):
            raise ReadOnlyParameters(
                f"Cannot replace parameters, try {type(self).__name__}.data() instead."
            )
        if not isinstance(arg, Tensor):
            raise TypeError(f"{type(self).__name__}.data_() 'arg' must be tensor, not {type(arg)}")
        shape = self.data_shape
        if arg.ndim != len(shape) + 1:
            raise ValueError(
                f"{type(self).__name__}.data_() 'arg' must be {len(shape) + 1}-dimensional tensor"
                f", but arg.ndim={arg.ndim}"
            )
        shape = (arg.shape[0],) + shape
        if arg.shape != shape:
            raise ValueError(
                f"{type(self).__name__}.data_() 'arg' must have shape {shape!r}, not {arg.shape!r}"
            )
        if isinstance(params, Parameter) and not isinstance(arg, Parameter):
            self.params = Parameter(arg, params.requires_grad)
        else:
            self.params = arg
        self.clear_buffers()
        return self

    def _data(self: Union[TSpatialTransform, ParametricTransform]) -> Tensor:
        r"""Get most recent transformation parameters.

        When transformation parameters are obtained from a callable, this function invokes
        this callable with ``self.condition()`` as arguments if set, and returns the parameter
        obtained returned by this callable function or module. Otherwise, it simply returns a
        reference to the ``self.params`` tensor.

        Returns:
            Reference to ``self.params`` tensor or callable return value, respectively.

        """
        params = self.params
        if params is None:
            raise AssertionError(f"{type(self).__name__}._data() 'params' must be set first")
        if isinstance(params, type(self)):
            assert isinstance(params, ParametricTransform)
            return cast(ParametricTransform, params).data()
        if callable(params):
            args, kwargs = self.condition()
            pred = params(*args, **kwargs)
            if not isinstance(pred, Tensor):
                raise TypeError(f"{type(self).__name__}.params() value must be tensor")
            shape = self.data_shape
            if pred.ndim != len(shape) + 1:
                raise ValueError(
                    f"{type(self).__name__}.params() tensor must be {len(shape) + 1}-dimensional"
                )
            shape = (pred.shape[0],) + shape
            if pred.shape != shape:
                raise ValueError(f"{type(self).__name__}.params() tensor must have shape {shape!r}")
            return pred
        assert isinstance(params, Tensor)
        return params

    def link(
        self: Union[TSpatialTransform, ParametricTransform], other: TSpatialTransform
    ) -> TSpatialTransform:
        r"""Make shallow copy of this transformation which is linked to another instance."""
        return shallow_copy(self).link_(other)

    def link_(
        self: Union[TSpatialTransform, ParametricTransform],
        other: Union[TSpatialTransform, ParametricTransform]
    ) -> TSpatialTransform:
        r"""Link this transformation to another of the same type.

        This transformation is modified to use a reference to the given transformation. After linking,
        the transformation will not have parameters on its own, and its ``update()`` function will not
        recompute possibly previously shared parameters, e.g., parameters obtained by a callable neural
        network. Instead, it directly copies the parameters from the linked transformation.

        Args:
            other: Other transformation of the same type as ``self`` to which this transformation is linked.

        Returns:
            Reference to this transformation.

        """
        if other is self:
            raise ValueError(f"{type(self).__name__}.link() cannot link tranform to itself")
        if type(self) != type(other):
            raise TypeError(
                f"{type(self).__name__}.link() 'other' must be of the same type, got {type(other).__name__}"
            )
        self.params = other
        if not hasattr(self, "p"):
            if other.params is None:
                p = torch.empty(self.data_shape)
            else:
                p = other.data()
            self.register_buffer("p", p, persistent=False)
            if other.params is None:
                self.reset_parameters()
        return self

    def unlink(self: Union[TSpatialTransform, ParametricTransform]) -> TSpatialTransform:
        r"""Make a shallow copy of this transformation with parameters set to ``None``."""
        return shallow_copy(self).unlink_()

    def unlink_(self: Union[TSpatialTransform, ParametricTransform]) -> TSpatialTransform:
        r"""Resets transformation parameters to ``None``."""
        self.params = None
        if hasattr(self, "p"):
            delattr(self, "p")
        return self

    def update(self: Union[TSpatialTransform, ParametricTransform]) -> TSpatialTransform:
        r"""Update buffered data such as predicted parameters, velocities, and/or displacements."""
        if hasattr(self, "p"):
            p = self._data()
            self.register_buffer("p", p, persistent=False)
        super().update()
        return self


class InvertibleParametricTransform(ParametricTransform):
    r"""Mix-in for spatial transformations that support on-demand inversion."""

    def __init__(
        self,
        grid: Grid,
        groups: Optional[int] = None,
        params: Optional[Union[bool, Tensor, Callable[..., Tensor]]] = True,
        invert: bool = False,
    ) -> None:
        r"""Initialize transformation parameters.

        Args:
            grid: Grid domain on which transformation is defined.
            groups: Number of transformations. A given image batch can either be deformed by a
                single transformation, or a separate transformation for each image in the batch, e.g.,
                for group-wise or batched registration. The default is one transformation for all images
                in the batch, or the batch length of the ``params`` tensor if provided.
            params: Initial parameters. If a tensor is given, it is only registered as optimizable module
                parameters when of type ``torch.nn.Parameter``. When a callable is given instead, it will be
                called by ``self.update()`` with ``self.condition()`` arguments. When a boolean argument is
                given, a new zero-initialized tensor is created. If ``True``, this tensor is registered as
                optimizable module parameter.
            invert: Whether ``params`` correspond to the inverse transformation. When this flag is ``True``,
                the ``self.tensor()`` and related methods return the transformation corresponding to the
                inverse of the transformations with the given ``params``. For example in case of a rotation,
                the rotation matrix is first constructed from the rotation parameters (e.g., Euler angles),
                and then transposed if ``self.invert == True``. In general, inversion of linear transformations
                and non-rigid transformations parameterized by velocity fields can be done efficiently on-the-fly.

        """
        super().__init__(grid, groups=groups, params=params)
        self.invert = bool(invert)

    def inverse(
        self: Union[TSpatialTransform, InvertibleParametricTransform],
        link: bool = False,
        update_buffers: bool = False
    ) -> TSpatialTransform:
        r"""Get inverse of this transformation.

        Args:
            link: Whether the inverse transformation keeps a reference to this transformation.
                If ``True``, the ``update()`` function of the inverse function will not recompute
                shared parameters (e.g., parameters obtained by a callable neural network), but
                directly access the parameters from this transformation. Note that when ``False``,
                the inverse transformation will still share parameters, modules, and buffers with
                this transformation, but these shared tensors may be replaced by a call of ``update()``
                (which is implicitly called as pre-forward hook when ``__call__()`` is invoked).
            update_buffers: Whether buffers of inverse transformation should be updated after creating
                the shallow copy. If ``False``, the ``update()`` function of the returned inverse
                transformation has to be called before it is used.

        Returns:
            Shallow copy of this transformation which computes and applies the inverse transformation.
            The inverse transformation will share the parameters with this transformation.

        """
        inv = shallow_copy(self)
        if link:
            inv.link_(self)
        inv.invert = not self.invert
        return inv

    def extra_repr(self: Union[TSpatialTransform, InvertibleParametricTransform]) -> str:
        r"""Print current transformation."""
        return super().extra_repr() + f", invert={self.invert}"
