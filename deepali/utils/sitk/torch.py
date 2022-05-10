r"""Auxiliary functions for conversion between SimpleITK and PyTorch."""

from typing import Optional, Sequence

import SimpleITK as sitk
import torch


def image_from_tensor(
    data: torch.Tensor,
    origin: Optional[Sequence[float]] = None,
    spacing: Optional[Sequence[float]] = None,
    direction: Optional[Sequence[float]] = None,
) -> sitk.Image:
    r"""Create ``SimpleITK.Image`` from image data tensor.

    Args:
        data: Image tensor of shape ``(C, ..., X)``.
        origin: World coordinates of center of voxel with zero indices.
        spacing: Voxel size in world units in each dimension.
        direction: Flattened image orientation cosine matrix.

    Returns:
        SimpleITK image.

    """
    data = data.detach().cpu().rename(None)
    nchannels = data.shape[0]
    if nchannels == 1:
        data = data[0]
    else:
        data = data.unsqueeze(-1).transpose(0, -1).squeeze(0)
    image = sitk.GetImageFromArray(data.numpy(), isVector=nchannels > 1)
    if origin:
        image.SetOrigin(origin)
    if spacing:
        image.SetSpacing(spacing)
    if direction:
        image.SetDirection(direction)
    return image


def tensor_from_image(
    image: sitk.Image,
    dtype: Optional[torch.dtype] = None,
    device: Optional[torch.device] = None,
) -> torch.Tensor:
    r"""Create image data tensor from ``SimpleITK.Image``."""
    if image.GetPixelID() == sitk.sitkUInt16:
        image = sitk.Cast(image, sitk.sitkInt32)
    elif image.GetPixelID() == sitk.sitkUInt16:
        image = sitk.Cast(image, sitk.sitkInt64)
    data = torch.from_numpy(sitk.GetArrayFromImage(image))
    data = data.unsqueeze(0)
    if image.GetNumberOfComponentsPerPixel() > 1:
        data = data.transpose(0, -1).squeeze(-1)
    return data.to(dtype=dtype, device=device)
