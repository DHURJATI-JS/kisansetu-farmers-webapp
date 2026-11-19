"""Image safety & quality: strict decoding, EXIF/GPS stripping re-encode, blur/brightness checks, perceptual hash."""
from __future__ import annotations

import io

from PIL import Image, ImageFilter, ImageOps, ImageStat

Image.MAX_IMAGE_PIXELS = 40_000_000  # decompression-bomb guard
FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


class BadImage(ValueError):
    pass


def load_image(data: bytes) -> tuple[Image.Image, str]:
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        im = Image.open(io.BytesIO(data))
        fmt = im.format or ""
        if fmt not in FORMATS:
            raise BadImage("unsupported_format")
        im = ImageOps.exif_transpose(im)
        if im.mode not in ("RGB", "L"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                bg.paste(im, mask=im.split()[-1])
                im = bg
            else:
                im = im.convert("RGB")
        elif im.mode == "L":
            im = im.convert("RGB")
        return im, fmt
    except BadImage:
        raise
    except Exception as exc:
        raise BadImage("invalid_image") from exc


def reencode(im: Image.Image, fmt: str, max_side: int = 2400) -> tuple[bytes, str, str]:
    """Fresh encode → strips EXIF/GPS/ICC metadata and any polyglot payloads."""
    if max(im.size) > max_side:
        im = im.copy()
        im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    if fmt == "PNG":
        im.save(out, format="PNG", optimize=True)
        return out.getvalue(), "image/png", "png"
    im.save(out, format="JPEG", quality=88, optimize=True, progressive=True)
    return out.getvalue(), "image/jpeg", "jpg"


def quality(im: Image.Image) -> dict:
    w, h = im.size
    small = im.copy()
    small.thumbnail((1000, 1000))
    gray = small.convert("L")
    stat = ImageStat.Stat(gray)
    brightness = stat.mean[0]
    contrast = stat.stddev[0]
    try:
        import cv2
        import numpy as np

        blur = float(cv2.Laplacian(np.asarray(gray), cv2.CV_64F).var())
    except Exception:
        edges = gray.filter(ImageFilter.FIND_EDGES)
        blur = float(ImageStat.Stat(edges).var[0])
    return {"width": w, "height": h, "blur": round(blur, 1), "brightness": round(brightness, 1), "contrast": round(contrast, 1)}


def is_clear(q: dict, *, min_side: int = 320, min_blur: float = 40.0) -> tuple[bool, str | None]:
    """White-background payment apps are fine; reject only tiny, blurry, near-black or washed-out (no contrast) images."""
    if min(q["width"], q["height"]) < min_side:
        return False, "too_small"
    if q["blur"] < min_blur:
        return False, "blurry"
    if q["brightness"] < 25:
        return False, "too_dark"
    if q.get("contrast", 99) < 12:
        return False, "too_bright" if q["brightness"] > 128 else "too_dark"
    return True, None


def dhash(im: Image.Image, size: int = 8) -> str:
    g = im.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
    px = list(g.getdata())
    bits = 0
    for row in range(size):
        for col in range(size):
            left = px[row * (size + 1) + col]
            right = px[row * (size + 1) + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return f"{bits:016x}"


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def to_jpeg_bytes(im: Image.Image, max_side: int = 1280) -> bytes:
    """Compact copy for sending to vision models."""
    c = im.copy()
    c.thumbnail((max_side, max_side))
    out = io.BytesIO()
    c.save(out, format="JPEG", quality=85)
    return out.getvalue()
