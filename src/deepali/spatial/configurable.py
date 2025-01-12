r"""Configurable spatial transformation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Union

import torch
from torch import Tensor
from torch.nn import ModuleDict

from ..core.affine import euler_rotation_angles
from ..core.affine import euler_rotation_matrix
from ..core.config import DataclassConfig
from ..core.grid import Grid
from ..core.linalg import quaternion_to_rotation_matrix
from ..core.linalg import rotation_matrix_to_quaternion
from ..core.types import ScalarOrTuple

from .bspline import FreeFormDeformation, StationaryVelocityFreeFormDeformation
from .composite import SequentialTransform
from .linear import AnisotropicScaling, EulerRotation, QuaternionRotation
from .linear import HomogeneousTransform, Shearing, Translation
from .nonrigid import DisplacementFieldTransform, StationaryVelocityFieldTransform


ParamsDict = Mapping[str, Tensor]


# Names of elementary affine transformation child modules
# - key: Letter used in affine model definition (cf. TransformConfig)
AFFINE_NAMES = {
    "A": "affine",
    "K": "shearing",
    "T": "translation",
    "R": "rotation",
    "S": "scaling",
    "Q": "quaternion",
}

# Types of elementary affine transformations
# - key: Letter used in affine model definition (cf. TransformConfig)
AFFINE_TRANSFORMS = {
    "A": HomogeneousTransform,
    "K": Shearing,
    "T": Translation,
    "R": EulerRotation,
    "S": AnisotropicScaling,
    "Q": QuaternionRotation,
}

NONRIGID_TRANSFORMS = {
    "DDF": DisplacementFieldTransform,
    "FFD": FreeFormDeformation,
    "SVF": StationaryVelocityFieldTransform,
    "SVFFD": StationaryVelocityFreeFormDeformation,
}

VALID_COMPONENTS = ("Affine",) + tuple(NONRIGID_TRANSFORMS.keys())


def transform_components(model: str) -> List[str]:
    r"""Non-rigid component of transformation or ``None`` if it is a linear transformation."""
    return model.split(" o ")


def valid_transform_model(
    model: str, max_affine: Optional[int] = None, max_nonrigid: Optional[int] = None
) -> bool:
    r"""Whether given string denotes a valid transformation model."""
    components = transform_components(model)
    num_affine = 0
    num_nonrigid = 0
    for component in components:
        if component not in VALID_COMPONENTS:
            return False
        if component == "Affine":
            num_affine += 1
        else:
            num_nonrigid += 1
    if len(components) < 1:
        return False
    if max_affine is not None and num_affine > max_affine:
        return False
    if max_nonrigid is not None and num_nonrigid > max_nonrigid:
        return False
    return True


def has_affine_component(model: str) -> bool:
    r"""Whether transformation model includes an affine component."""
    return "Affine" in transform_components(model)


def has_nonrigid_component(model: str) -> bool:
    r"""Whether transformation model includes a non-rigid component."""
    return nonrigid_components(model)


def nonrigid_components(model: str) -> List[str]:
    r"""Non-rigid components of transformation model."""
    return [comp for comp in transform_components(model) if comp in NONRIGID_TRANSFORMS]


def affine_first(model: str) -> bool:
    r"""Whether transformation applies affine component first."""
    components = transform_components(model)
    assert components, "must contain at least one transformation component"
    return components[-1] == "Affine"


@dataclass
class TransformConfig(DataclassConfig):
    r"""Configuration of spatial transformation model."""

    # Spatial transformation model to use
    transform: str = "Affine o SVF"
    # Composition of affine transformation.
    #
    # The string value of this configuration entry can be in one of two forms:
    # - Matrix notation: Each letter is a factor in the sequence of matrix-matrix products.
    # - Function composition: Use deliminator " o " between transformations to denote composition.
    affine_model: str = "TRS"  # same as "T o R o S"
    # Order of elementary Euler rotations. This configuration value is only used
    # when ``affine_model`` contains an ``EulerRotation`` denoted by letter "R".
    # Valid values are "ZXZ", "XZX", ... (cf. euler_rotation() function).
    rotation_model: str = "ZXZ"
    # Control point spacing of non-rigid transformations in voxel units of
    # the grid domain with respect to which the transformations are defined.
    control_point_spacing: ScalarOrTuple[int] = 1
    # Number of scaling and squaring steps
    scaling_and_squaring_steps: int = 6
    # Whether predicted transformation parameters are with respect to a grid
    # with point coordinates in the order (..., x) instead of (x, ...).
    flip_grid_coords: bool = False

    def _finalize(self, parent: Path) -> None:
        r"""Finalize parameters after loading these from input file."""
        super()._finalize(parent)


class ConfigurableTransform(SequentialTransform):
    r"""Configurable spatial transformation."""

    def __init__(
        self,
        grid: Grid,
        params: Optional[Union[bool, Callable[..., ParamsDict], ParamsDict]] = True,
        config: Optional[TransformConfig] = None,
    ) -> None:
        r"""Initialize spatial transformation."""
        if (
            params not in (None, False, True)
            and not callable(params)
            and not isinstance(params, Mapping)
        ):
            raise TypeError(
                f"{type(self).__name__}() 'params' must be bool, callable, dict, or None"
            )
        if config is None:
            config = getattr(params, "config", None)
            if config is None:
                raise AssertionError(
                    f"{type(self).__name__}() 'config' or 'params.config' required"
                )
            if not isinstance(config, TransformConfig):
                raise TypeError(f"{type(self).__name__}() 'params.config' must be TransformConfig")
        elif not isinstance(config, TransformConfig):
            raise TypeError(f"{type(self).__name__}() 'config' must be TransformConfig")
        if not valid_transform_model(config.transform, max_affine=1, max_nonrigid=1):
            raise ValueError(
                f"{type(self).__name__}() 'config.transform' invalid or not supported: {config.transform}"
            )
        modules = ModuleDict()
        # Initialize affine components
        if has_affine_component(config.transform):
            for key in reversed(config.affine_model.replace(" o ", "")):
                key = key.upper()
                if key not in AFFINE_TRANSFORMS:
                    raise ValueError(
                        f"{type(self).__name__}() invalid character '{key}' in 'config.affine_model'"
                    )
                name = AFFINE_NAMES[key]
                if name in modules:
                    raise NotImplementedError(
                        f"{type(self).__name__}() 'config.affine_model' must contain each elementary"
                        f" transform at most once, but encountered key '{key}' more than once."
                    )
                kwargs = dict(grid=grid, params=params if isinstance(params, bool) else None)
                if key == "R":
                    kwargs["order"] = config.rotation_model
                modules[name] = AFFINE_TRANSFORMS[key](**kwargs)
        # Initialize non-rigid component
        nonrigid_models = nonrigid_components(config.transform)
        if len(nonrigid_models) > 1:
            raise ValueError(
                f"{type(self).__name__}() 'config.transform' must contain at most one non-rigid component"
            )
        if nonrigid_models:
            nonrigid_model = nonrigid_models[0]
            nonrigid_params = params if isinstance(params, bool) else None
            nonrigid_kwargs = dict(grid=grid, params=nonrigid_params)
            NonRigidTransform = NONRIGID_TRANSFORMS[nonrigid_model]
            if nonrigid_model in ("DDF", "SVF") and config.control_point_spacing > 1:
                size = grid.size_tensor()
                stride = torch.tensor(config.control_point_spacing).to(size)
                size = size.div(stride).ceil().long()
                nonrigid_kwargs["grid"] = grid.resize(size)
            if nonrigid_model == "SVF":
                nonrigid_kwargs["steps"] = config.scaling_and_squaring_steps
            if nonrigid_model in ("FFD", "SVFFD"):
                nonrigid_kwargs["stride"] = config.control_point_spacing
            _modules = ModuleDict({"nonrigid": NonRigidTransform(**nonrigid_kwargs)})
            if affine_first(config.transform):
                modules.update(_modules)
            else:
                _modules.update(modules)
                modules = _modules
        # Set parameters of transformation if given as dictionary
        if isinstance(params, Mapping):
            for name, transform in self.named_transforms():
                transform.data_(params[name])
        # Insert transformations in order of composition
        super().__init__(grid, modules)
        self.config = config
        self.params = params if callable(params) else None

    def _data(self) -> Dict[str, Tensor]:
        r"""Get most recent transformation parameters."""
        if not self._transforms:
            return {}
        params = self.params
        if params is None:
            params = {}
            for name, transform in self.named_transforms():
                params[name] = transform.data()
            return params
        if isinstance(params, ConfigurableTransform):
            return {}  # transforms are individually linked to their counterparts
        if callable(params):
            args, kwargs = self.condition()
            pred = params(*args, **kwargs)
            if not isinstance(pred, Mapping):
                raise TypeError(
                    f"{type(self).__name__} 'params' callable return value must be a Mapping"
                )
        elif isinstance(params, Mapping):
            pred = params
        else:
            raise TypeError(
                f"{type(self).__name__} 'params' attribute must be a"
                " callable, Mapping, linked ConfigurableTransform, or None"
            )
        data = {}
        flip_grid_coords = self.config.flip_grid_coords
        if "affine" in self._transforms:
            matrix = pred["affine"]
            assert isinstance(matrix, Tensor)
            assert matrix.ndim >= 2
            D = matrix.shape[-2]
            assert matrix.shape[-1] == D + 1
            if flip_grid_coords:
                matrix[..., :D, :D] = matrix[..., :D, :D].flip((1, 2))
                matrix[..., :D, -1] = matrix[..., :D, -1].flip(-1)
            data["affine"] = matrix
        if "translation" in self._transforms:
            if "translation" in pred:
                offset = pred["translation"]
            else:
                offset = pred["offset"]
            assert isinstance(offset, Tensor)
            if flip_grid_coords:
                offset = offset.flip(-1)
            data["translation"] = offset
        if "rotation" in self._transforms:
            if "rotation" in pred:
                angles = pred["rotation"]
            else:
                angles = pred["angles"]
            assert isinstance(angles, Tensor)
            if flip_grid_coords:
                rotmodel = self.config.rotation_model
                rotation = euler_rotation_matrix(angles, order=rotmodel).flip((1, 2))
                angles = euler_rotation_angles(rotation, order=rotmodel)
            data["rotation"] = angles
        if "scaling" in self._transforms:
            if "scaling" in pred:
                scales = pred["scaling"]
            else:
                scales = pred["scales"]
            assert isinstance(scales, Tensor)
            if flip_grid_coords:
                scales = scales.flip(-1)
            data["scaling"] = scales
        if "quaternion" in self._transforms:
            q = pred["quaternion"]
            assert isinstance(q, Tensor)
            if flip_grid_coords:
                m = quaternion_to_rotation_matrix(q)
                m = m.flip((1, 2))
                q = rotation_matrix_to_quaternion(m)
            data["quaternion"] = q
        if "nonrigid" in self._transforms:
            if "nonrigid" in pred:
                vfield = pred["nonrigid"]
            else:
                vfield = pred["vfield"]
            assert isinstance(vfield, Tensor)
            if flip_grid_coords:
                vfield = vfield.flip(1)
            data["nonrigid"] = vfield
        return data

    def inverse(self, link: bool = False, update_buffers: bool = False) -> ConfigurableTransform:
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
            Shallow copy of this transformation which computes and applied the inverse transformation.
            The inverse transformation will share the parameters with this transformation. Not all
            transformations may implement this functionality.

        Raises:
            NotImplementedError: When a transformation does not support sharing parameters with its inverse.

        """
        inv = super().inverse(link=link, update_buffers=update_buffers)
        if link:
            inv.params = self
        return inv

    def update(self) -> ConfigurableTransform:
        r"""Update transformation parameters."""
        if self.params is not None:
            params = self._data()
            for k, p in params.items():
                transform = self._transforms[k]
                transform.data_(p)
        super().update()
        return self
