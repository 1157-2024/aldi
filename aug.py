import numpy as np
import random
import math
from scipy.ndimage import gaussian_filter
from torch.nn import functional as F

from detectron2.data.transforms.augmentation import _get_aug_input_args
from detectron2.data.transforms.augmentation_impl import RandomApply
from detectron2.data import transforms as T
from detectron2.data import detection_utils as utils
from fvcore.transforms.transform import Transform, NoOpTransform

# key for weakly augmented image in aug_input object
WEAK_IMG = "img_weak"

def get_augs(cfg, labeled):
    """
    Get augmentations list for a dataset (labeled or unlabeled) according to settings in cfg.
    """
    # default weak augmentations: see DatasetMapper.from_config
    augs = utils.build_augmentation(cfg, is_train=True)
    if cfg.INPUT.CROP.ENABLED:
        augs.insert(0, T.RandomCrop(cfg.INPUT.CROP.TYPE, cfg.INPUT.CROP.SIZE))
    
    # add a hook to save the image after weak augmentation occurs
    augs.append(SaveImgAug(WEAK_IMG))

    # add strong augmentation
    if (labeled and cfg.DATASETS.LABELED_STRONG_AUG) or (not labeled and cfg.DATASETS.UNLABELED_STRONG_AUG):
        augs += build_strong_augmentation_d2()

    # add MIC
    if (labeled and cfg.DATASETS.LABELED_MIC_AUG) or (not labeled and cfg.DATASETS.UNLABELED_MIC_AUG):
        augs.append(T.RandomApply(MICTransform(0.5, 32), prob=1.0))

    return augs

def build_strong_augmentation_d2(include_erasing=True):
    """
    Modified from Adaptive Teacher / Unbiased Teacher codebase
        - Replace random "hue" of ColorJitter with RandomLighting (this has the advantage of being much faster)
        - Use scipy implementation of gaussian blur
    """
    augs = [
        T.RandomApply(T.AugmentationList([
            T.RandomContrast(0.6, 1.4),
            T.RandomBrightness(0.6, 1.4),
            T.RandomSaturation(0.6, 1.4),
            T.RandomLighting(0.1),
        ]), prob=0.8),
        T.RandomApply(GrayscaleTransform(), prob=0.2),
        T.RandomApply(RandomBlurTransform((0.1, 2.0)), prob=0.5),
    ]
    if include_erasing:
        augs += [
            T.RandomApply(RandomEraseTransform(sl=0.05, sh=0.2, r1=0.3, r2=3.3, value="random"), prob=0.7),
            T.RandomApply(RandomEraseTransform(sl=0.02, sh=0.2, r1=0.1, r2=6, value="random"), prob=0.5),
            T.RandomApply(RandomEraseTransform(sl=0.02, sh=0.2, r1=0.05, r2=8, value="random"), prob=0.3),
        ]
    return augs

class SaveImgAug(T.Augmentation):
    """
    A Detectron2 'augmentation' that saves a copy of the image to the input object.
    This is used to get a copy of the image before additional augmentations are applied,
    so that, e.g., we can obtain a weakly and strongly augmented version of the same image for 
    self-training.
    """
    def __init__(self, savename):
        super().__init__()
        self._init(locals())

    def get_transform(self, *args) -> Transform:
        return NoOpTransform()
    
    def __call__(self, aug_input) -> Transform:
        image = _get_aug_input_args(self, aug_input)[0].copy()
        setattr(aug_input, self.savename, image)
        return super().__call__(aug_input)

class GrayscaleTransform(Transform):
    """See Detectron2.data.transforms.augmentation_impl.RandomSaturation"""
    def __init__(self):
        super().__init__()

    def apply_image(self, img: np.ndarray) -> np.ndarray:
        if img.dtype == np.uint8:
            img = img.astype(np.float32)
            img = img.dot([0.299, 0.587, 0.114])[:, :, np.newaxis]
            return np.clip(img, 0, 255).astype(np.uint8)
        else:
            return img.dot([0.299, 0.587, 0.114])[:, :, np.newaxis]

    def apply_coords(self, coords: np.ndarray) -> np.ndarray:
        return coords

    def apply_segmentation(self, segmentation: np.ndarray) -> np.ndarray:
        return segmentation

    def inverse(self) -> Transform:
        return NoOpTransform()
    
class RandomBlurTransform(Transform):
    def __init__(self, sigma):
        super().__init__()
        self.sigma = sigma

    def apply_image(self, img: np.ndarray) -> np.ndarray:
        sigma = random.uniform(self.sigma[0], self.sigma[1])
        if img.dtype == np.uint8:
            img = img.astype(np.float32)
            img = gaussian_filter(img, sigma=sigma)
            return np.clip(img, 0, 255).astype(np.uint8)
        else:
            return gaussian_filter(img, sigma=sigma)

    def apply_coords(self, coords: np.ndarray) -> np.ndarray:
        return coords

    def apply_segmentation(self, segmentation: np.ndarray) -> np.ndarray:
        return segmentation

    def inverse(self) -> Transform:
        return NoOpTransform()

class RandomEraseTransform(Transform):
    """
    Modified from this implementation: https://github.com/zhunzhong07/Random-Erasing/blob/master/transforms.py
    to better match Torchvision params:
        scale=(sl, sh): range of proportion of erased area against input image.
        ratio=(r1. r2): range of aspect ratio of erased area.
        value="random" or specified value
    """
    def __init__(self, sl = 0.02, sh = 0.4, r1 = 0.3, r2=3.3, value="random"): #[0.4914, 0.4822, 0.4465]):
        super().__init__()
        self._set_attributes(locals())
    
    def apply_image(self, img: np.ndarray) -> np.ndarray:
        was_int = False
        if img.dtype == np.uint8:
            was_int = True
            img = img.astype(np.float32)

        for attempt in range(100):
            _, imgw, imgh = img.shape
            area = imgw * imgh
            target_area = random.uniform(self.sl, self.sh) * area
            aspect_ratio = random.uniform(self.r1, self.r2)
            h = int(round(math.sqrt(target_area * aspect_ratio)))
            w = int(round(math.sqrt(target_area / aspect_ratio)))
            if w > 0 and h > 0 and w < imgw and h < imgh:
                x1 = random.randint(0, imgh - h)
                y1 = random.randint(0, imgw - w)
                if self.value == "random":
                    img[:, x1:x1+h, y1:y1+w] = np.random.rand(img.shape[0], h, w)
                else:
                    img[:, x1:x1+h, y1:y1+w] = self.value
                break
        if was_int:
            return np.clip(img, 0, 255).astype(np.uint8)
        else:
            return img

    def apply_coords(self, coords: np.ndarray) -> np.ndarray:
        return coords

    def apply_segmentation(self, segmentation: np.ndarray) -> np.ndarray:
        return segmentation

    def inverse(self) -> Transform:
        return NoOpTransform() # ?

class MICTransform(Transform):
    def __init__(self, ratio, block_size):
        super().__init__()
        self._set_attributes(locals())

    def apply_image(self, img: np.ndarray) -> np.ndarray:
        _, H, W = img.shape

        was_int = False
        if img.dtype == np.uint8:
            was_int = True
            img = img.astype(np.float32)

        mh, mw = round(H / self.block_size), round(W / self.block_size)
        input_mask = np.random.rand(mh, mw)
        input_mask = input_mask > self.ratio
        input_mask = np.resize(input_mask, (H, W))
        masked_img = img * input_mask

        if was_int:
            return np.clip(masked_img, 0, 255).astype(np.uint8)
        else:
            return masked_img  

    def apply_coords(self, coords: np.ndarray) -> np.ndarray:
        return coords

    def apply_segmentation(self, segmentation: np.ndarray) -> np.ndarray:
        return segmentation

    def inverse(self) -> Transform:
        return NoOpTransform()