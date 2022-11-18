# Copyright (c) MONAI Consortium
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import math
import pathlib

import numpy as np
import openslide
import torch
from monai.apps.deepgrow.transforms import AddGuidanceSignald, AddInitialSeedPointd
from monai.apps.nuclick.transforms import NuclickKeys
from monai.apps.nuclick.transforms import PostFilterLabeld as NuClickPostFilterLabeld
from monai.config import KeysCollection
from monai.networks.layers import GaussianFilter
from monai.transforms import MapTransform, Transform
from PIL import Image
from scipy.ndimage import binary_fill_holes
from skimage.filters.thresholding import threshold_otsu
from skimage.morphology import remove_small_holes, remove_small_objects

logger = logging.getLogger(__name__)


class LoadImagePatchd(MapTransform):
    def __init__(
        self, keys: KeysCollection, meta_key_postfix: str = "meta_dict", mode="RGB", dtype=np.uint8, padding=True
    ):
        super().__init__(keys)
        self.meta_key_postfix = meta_key_postfix
        self.mode = mode
        self.dtype = dtype
        self.padding = padding

    def __call__(self, data):
        d = dict(data)
        for key in self.keys:
            if not isinstance(d[key], str):
                continue  # Support direct image in np (pass only transform)

            name = d[key]
            ext = pathlib.Path(name).suffix
            if ext == ".npy":
                d[key] = np.load(d[key])
                continue

            location = d.get("location", (0, 0))
            level = d.get("level", 0)
            size = d.get("size", None)

            # Model input size
            tile_size = d.get("tile_size", size)

            if not ext or ext in (
                ".bif",
                ".mrxs",
                ".ndpi",
                ".scn",
                ".svs",
                ".svslide",
                ".tif",
                ".tiff",
                ".vms",
                ".vmu",
            ):
                slide = openslide.OpenSlide(name)
                size = size if size else slide.dimensions
                img = slide.read_region(location, level, size)
            else:
                img = Image.open(d[key])

            img = img.convert(self.mode) if self.mode else img
            image_np = np.array(img, dtype=self.dtype)

            meta_dict_key = f"{key}_{self.meta_key_postfix}"
            meta_dict = d.get(meta_dict_key)
            if meta_dict is None:
                d[meta_dict_key] = dict()
                meta_dict = d.get(meta_dict_key)

            meta_dict["spatial_shape"] = np.asarray(image_np.shape[:-1])
            meta_dict["original_channel_dim"] = -1
            logger.debug(f"Image shape: {image_np.shape} vs size: {size} vs tile_size: {tile_size}")

            if self.padding and tile_size and (image_np.shape[0] != tile_size[0] or image_np.shape[1] != tile_size[1]):
                image_np = self.pad_to_shape(image_np, tile_size)
            d[key] = image_np
        return d

    @staticmethod
    def pad_to_shape(img, shape):
        img_shape = img.shape[:-1]
        s_diff = np.array(shape) - np.array(img_shape)
        diff = [(0, s_diff[0]), (0, s_diff[1]), (0, 0)]
        return np.pad(
            img,
            diff,
            mode="constant",
            constant_values=0,
        )


def mask_percent(img_np):
    if (len(img_np.shape) == 3) and (img_np.shape[2] == 3):
        np_sum = img_np[:, :, 0] + img_np[:, :, 1] + img_np[:, :, 2]
        mask_percentage = 100 - np.count_nonzero(np_sum) / np_sum.size * 100
    else:
        mask_percentage = 100 - np.count_nonzero(img_np) / img_np.size * 100
    return mask_percentage


def filter_green_channel(img_np, green_thresh=200, avoid_overmask=True, overmask_thresh=90, output_type="bool"):
    g = img_np[:, :, 1]
    gr_ch_mask = (g < green_thresh) & (g > 0)
    mask_percentage = mask_percent(gr_ch_mask)
    if (mask_percentage >= overmask_thresh) and (green_thresh < 255) and (avoid_overmask is True):
        new_green_thresh = math.ceil((255 - green_thresh) / 2 + green_thresh)
        gr_ch_mask = filter_green_channel(img_np, new_green_thresh, avoid_overmask, overmask_thresh, output_type)
    return gr_ch_mask


def filter_grays(rgb, tolerance=15):
    rg_diff = abs(rgb[:, :, 0] - rgb[:, :, 1]) <= tolerance
    rb_diff = abs(rgb[:, :, 0] - rgb[:, :, 2]) <= tolerance
    gb_diff = abs(rgb[:, :, 1] - rgb[:, :, 2]) <= tolerance
    return ~(rg_diff & rb_diff & gb_diff)


def filter_ostu(img):
    mask = np.dot(img[..., :3], [0.2125, 0.7154, 0.0721]).astype(np.uint8)
    mask = 255 - mask
    return mask > threshold_otsu(mask)


def filter_remove_small_objects(img_np, min_size=3000, avoid_overmask=True, overmask_thresh=95):
    rem_sm = remove_small_objects(img_np.astype(bool), min_size=min_size)
    mask_percentage = mask_percent(rem_sm)
    if (mask_percentage >= overmask_thresh) and (min_size >= 1) and (avoid_overmask is True):
        new_min_size = round(min_size / 2)
        rem_sm = filter_remove_small_objects(img_np, new_min_size, avoid_overmask, overmask_thresh)
    return rem_sm


class FilterImaged(MapTransform):
    def __init__(self, keys: KeysCollection, min_size=500):
        super().__init__(keys)
        self.min_size = min_size

    def filter(self, rgb):
        mask_not_green = filter_green_channel(rgb)
        mask_not_gray = filter_grays(rgb)
        mask_gray_green = mask_not_gray & mask_not_green
        mask = (
            filter_remove_small_objects(mask_gray_green, min_size=self.min_size) if self.min_size else mask_gray_green
        )

        return rgb * np.dstack([mask, mask, mask])

    def __call__(self, data):
        d = dict(data)
        for key in self.keys:
            img = d[key].numpy() if isinstance(d[key], torch.Tensor) else d[key]
            d[key] = self.filter(img)
        return d


class PostFilterLabeld(MapTransform):
    def __init__(self, keys: KeysCollection, min_size=64, min_hole=64):
        super().__init__(keys)
        self.min_size = min_size
        self.min_hole = min_hole

    def __call__(self, data):
        d = dict(data)
        for key in self.keys:
            label = d[key].astype(np.uint8)
            if self.min_hole:
                label = remove_small_holes(label, area_threshold=self.min_hole)
            label = binary_fill_holes(label).astype(np.uint8)
            if self.min_size:
                label = remove_small_objects(label, min_size=self.min_size)

            d[key] = np.where(label > 0, d[key], 0)
        return d


class AddInitialSeedPointExd(AddInitialSeedPointd):
    def _apply(self, label, sid):
        try:
            return super()._apply(label, sid)
        except AssertionError:
            dimensions = 2
            default_guidance = [-1] * (dimensions + 1)
            return np.asarray([[default_guidance], [default_guidance]])


class AddClickGuidanced(Transform):
    def __init__(
        self,
        guidance="guidance",
        foreground="foreground",
        background="background",
    ):
        self.guidance = guidance
        self.foreground = foreground
        self.background = background

    def __call__(self, data):
        d = dict(data)

        location = d.get("location", (0, 0))
        tx, ty = location[0], location[1]

        pos = d.get(self.foreground)
        pos = (np.array(pos) - (tx, ty)).astype(int).tolist() if pos else []

        neg = d.get(self.background)
        neg = (np.array(neg) - (tx, ty)).astype(int).tolist() if neg else []

        d[self.guidance] = [pos, neg]
        return d


class AddClickGuidanceSignald(AddGuidanceSignald):
    def _apply(self, image, guidance):
        if guidance and (guidance[0] or guidance[1]):
            return super()._apply(image, guidance)

        if isinstance(image, torch.Tensor):
            s = torch.zeros_like(image[0])[None]
            return torch.concat([image, s, s])

        ns = np.zeros_like(image[0])[np.newaxis]
        return np.concatenate([image, ns, ns], axis=0)


class SplitLabelExd(MapTransform):
    def __init__(self, keys: KeysCollection, others: str = NuclickKeys.OTHERS):

        super().__init__(keys, allow_missing_keys=False)
        self.others = others

    def __call__(self, data):
        if len(self.keys) > 1:
            logger.error("Only 'label' key is supported, more than 1 key was found")
            return None

        d = dict(data)
        for key in self.keys:
            label = d[key]
            mask_value = int(torch.max(torch.where(torch.logical_and(label > 0, label < 255), label, 0)))
            d[key] = (label == mask_value).type(torch.uint8)
            d[self.others] = (label == 255).type(torch.uint8)
        return d


class ApplyGaussianFilter(MapTransform):
    def __init__(self, keys: KeysCollection, spatial_dims=2, index: int = -1, sigma: int = 8, mask_to_point=True):

        super().__init__(keys, allow_missing_keys=False)
        self.spatial_dims = spatial_dims
        self.index = index
        self.sigma = sigma
        self.mask_to_point = mask_to_point

    def mask_to_random_point(self, mask):
        point_mask = np.zeros_like(mask)
        indices = np.argwhere(mask > 0)
        if len(indices) > 0:
            idx = np.random.randint(0, len(indices))
            point_mask[indices[idx, 0], indices[idx, 1]] = 1

        return point_mask

    def __call__(self, data):
        d = dict(data)
        gaussian = GaussianFilter(spatial_dims=self.spatial_dims, sigma=self.sigma)

        for key in self.keys:
            img = d[key]
            signal_tensor = img if self.index < 0 else img[self.index]
            signal_tensor = self.mask_to_random_point(signal_tensor) if self.mask_to_point else signal_tensor
            signal_tensor = torch.tensor(signal_tensor)
            signal_tensor = gaussian(signal_tensor)
            if self.index < 0:
                img = signal_tensor
            else:
                img[self.index] = signal_tensor
            d[key] = img
        return d


class FixNuclickClassd(Transform):
    def __init__(self, image="image", label="label", offset=-1) -> None:
        self.image = image
        self.label = label
        self.offset = offset

    def __call__(self, data):
        d = dict(data)
        label = d[self.label]
        mask_value = int(torch.max(torch.where(torch.logical_and(label > 0, label < 255), label, 0)))
        signal = (label == mask_value).type(torch.uint8)

        if len(signal.shape) < len(d[self.image].shape):
            signal = signal[None]

        d[self.image] = torch.cat([d[self.image], signal], dim=len(signal.shape) - 3)
        d[self.label] = mask_value + self.offset
        return d


class NuClickPostFilterLabelExd(NuClickPostFilterLabeld):
    def __call__(self, data):
        d = dict(data)

        nuc_points = d[self.nuc_points]
        bounding_boxes = d[self.bounding_boxes]
        img_height = d[self.img_height]
        img_width = d[self.img_width]

        for key in self.keys:
            label = d[key].astype(np.uint8)
            masks = self.post_processing(
                label,
                thresh=self.thresh,
                min_size=self.min_size,
                min_hole=self.min_hole,
                do_reconstruction=self.do_reconstruction,
                nuc_points=nuc_points,
            )

            pred_classes = d.get("pred_classes")
            d[key] = self.gen_instance_map(
                masks, bounding_boxes, img_height, img_width, pred_classes=pred_classes
            ).astype(np.uint8)
        return d

    def gen_instance_map(self, masks, bounding_boxes, m, n, flatten=True, pred_classes=None):
        instance_map = np.zeros((m, n), dtype=np.uint16)
        for i, item in enumerate(masks):
            this_bb = bounding_boxes[i]
            this_mask_pos = np.argwhere(item > 0)
            this_mask_pos[:, 0] = this_mask_pos[:, 0] + this_bb[1]
            this_mask_pos[:, 1] = this_mask_pos[:, 1] + this_bb[0]

            c = pred_classes[i] if pred_classes and i < len(pred_classes) else 1
            instance_map[this_mask_pos[:, 0], this_mask_pos[:, 1]] = c if flatten else i + 1
        return instance_map
