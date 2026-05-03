from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageFile

from regions import CropRegion


Image.MAX_IMAGE_PIXELS = None
ImageFile.LOAD_TRUNCATED_IMAGES = True


@dataclass(frozen=True, slots=True)
class CropResult:
    output_path: Path
    width: int
    height: int


def crop_image(
    *,
    source_path: Path,
    output_path: Path,
    region: CropRegion,
    jpeg_quality: int = 92,
) -> CropResult:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(source_path) as image:
        image_width, image_height = image.size
        safe_region = region.clamp(image_width=image_width, image_height=image_height)

        if safe_region.width <= 0 or safe_region.height <= 0:
            raise ValueError(
                f"Região inválida após clamp: {safe_region}. "
                f"Tamanho da imagem: {image_width}x{image_height}"
            )

        cropped = image.crop(safe_region.box)

        if cropped.mode != "RGB":
            cropped = cropped.convert("RGB")

        cropped.save(
            output_path,
            format="JPEG",
            quality=jpeg_quality,
            optimize=False,
        )

        return CropResult(
            output_path=output_path,
            width=cropped.width,
            height=cropped.height,
        )