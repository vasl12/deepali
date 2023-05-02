r"""Modules operating on flow vector fields."""

from __future__ import annotations

from copy import copy as shallow_copy
from typing import Optional

from torch import Tensor
from torch.nn import Module

from ..core import functional as U
from ..core import ALIGN_CORNERS


class ExpFlow(Module):
    r"""Layer that computes exponential map of flow field."""

    def __init__(
        self,
        scale: Optional[float] = None,
        steps: Optional[int] = None,
        align_corners: bool = ALIGN_CORNERS,
    ):
        r"""Initialize parameters.

        Args:
            scale: Constant scaling factor of input velocities (e.g., -1 for inverse). Default is 1.
            steps: Number of squaring steps.
            align_corners: Whether input vectors are with respect to ``Axes.CUBE`` (False)
                or ``Axes.CUBE_CORNERS`` (True). This flag is passed on to ``grid_sample()``.

        """
        super().__init__()
        self.scale = float(1 if scale is None else scale)
        self.steps = int(5 if steps is None else steps)
        self.align_corners = bool(align_corners)

    def forward(self, x: Tensor, inverse: bool = False) -> Tensor:
        r"""Compute exponential map of vector field."""
        scale = self.scale
        if inverse:
            scale *= -1
        return U.expv(x, scale=scale, steps=self.steps, align_corners=self.align_corners)

    @property
    def inv(self) -> ExpFlow:
        r"""Get inverse exponential map.

        .. code::
            u = exp(v)
            w = exp.inv(v)

        """
        return self.inverse()

    def inverse(self) -> ExpFlow:
        r"""Get inverse exponential map."""
        copy = shallow_copy(self)
        copy.scale *= -1
        return copy

    def extra_repr(self) -> str:
        return f"scale={repr(self.scale)}, steps={repr(self.steps)}"
