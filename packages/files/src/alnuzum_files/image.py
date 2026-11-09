"""A photo or a picture of a page: re-encoded as a JPEG of at most 1568 px a side, the size the model reads
without scaling it down itself."""

import io

from alnuzum_files import FileError

MAX_PIXELS = 40_000_000
MAX_SIDE = 1568


def normalize(data: bytes) -> bytes:
    from PIL import Image, ImageOps, UnidentifiedImageError

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.width * img.height > MAX_PIXELS:
                raise FileError("e_image_size")
            # shrink first, in place: a 40 Mp picture is never copied whole (the container has 512 MB); a JPEG
            # is even decoded at a reduced scale
            img.draft("RGB", (MAX_SIDE, MAX_SIDE))
            img.thumbnail((MAX_SIDE, MAX_SIDE))
            turned = ImageOps.exif_transpose(img).convert("RGB")
            out = io.BytesIO()
            turned.save(out, "JPEG", quality=88)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError) as exc:
        if isinstance(exc, FileError):
            raise
        raise FileError("e_damaged") from None
    return out.getvalue()
