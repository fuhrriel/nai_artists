"""Thumbnails: 384px longest side, WebP q80, alpha dropped."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

SIZE = 384
QUALITY = 80


def make_thumb(src: Path, dest: Path, size: int = SIZE, quality: int = QUALITY) -> Path:
    with Image.open(src) as im:
        im = im.convert("RGB")
        im.thumbnail((size, size), Image.Resampling.LANCZOS)
        dest.parent.mkdir(parents=True, exist_ok=True)
        im.save(dest, "WEBP", quality=quality, method=4)
    return dest
