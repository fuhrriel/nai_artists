"""Read NovelAI metadata from a PNG.

1. tEXt chunks: `Comment` is a JSON string with everything (docs/nai_api.md §8-9).
2. Fallback: stealth pnginfo in the alpha-channel LSBs, magic `stealth_pngcomp`,
   32-bit big-endian bit length, gzip'd JSON (with `Comment` as a nested string).
   Column-major pixel order, per NovelAI/novelai-image-metadata.
"""

from __future__ import annotations

import json
import zlib
from pathlib import Path
from typing import Any

from PIL import Image

STEALTH_MAGIC = b"stealth_pngcomp"
STEALTH_MAX_BYTES = 8 << 20  # decompressed payload cap: a real Comment is a few KiB, a gzip bomb is not
_LSB_TABLE = bytes.maketrans(bytes(range(256)), b"".join(b"1" if i & 1 else b"0" for i in range(256)))


def read_text_chunks(path: Path | str) -> dict[str, str]:
    with Image.open(path) as im:
        return dict(getattr(im, "text", {}) or {})


def _decode_stealth(im: Image.Image) -> dict[str, Any] | None:
    if "A" not in im.getbands():
        return None
    alpha = im.getchannel("A").transpose(Image.Transpose.TRANSPOSE)  # column-major
    bits = alpha.tobytes().translate(_LSB_TABLE)  # b"0101..." one char per pixel

    def take(offset_bits: int, n_bits: int) -> bytes:
        chunk = bits[offset_bits : offset_bits + n_bits]
        if len(chunk) < n_bits:
            raise ValueError("stealth data truncated")
        return int(chunk, 2).to_bytes(n_bits // 8, "big")

    magic_bits = len(STEALTH_MAGIC) * 8
    if len(bits) < magic_bits + 32 or take(0, magic_bits) != STEALTH_MAGIC:
        return None
    length = int.from_bytes(take(magic_bits, 32), "big")
    payload = take(magic_bits + 32, length)
    d = zlib.decompressobj(wbits=31)  # gzip container
    raw = d.decompress(payload, STEALTH_MAX_BYTES)
    if d.unconsumed_tail:
        raise ValueError("stealth payload too large")
    data = json.loads(raw.decode("utf-8"))
    return data if isinstance(data, dict) else None


def read_comment(path: Path | str) -> dict[str, Any] | None:
    """Return the parsed `Comment` dict, or None if the file carries no NAI metadata."""
    with Image.open(path) as im:
        text = dict(getattr(im, "text", {}) or {})
        if "Comment" in text:
            try:
                c = json.loads(text["Comment"])
                if isinstance(c, dict):
                    return c
            except ValueError:
                pass
        try:
            stealth = _decode_stealth(im)
        except (ValueError, OSError, zlib.error):
            stealth = None
    if not stealth:
        return None
    comment = stealth.get("Comment")
    if isinstance(comment, str):
        try:
            c = json.loads(comment)
            return c if isinstance(c, dict) else None
        except ValueError:
            return None
    return comment if isinstance(comment, dict) else None


def base_caption(comment: dict[str, Any]) -> str:
    """Positive prompt: v4_prompt.caption.base_caption if present, else prompt."""
    try:
        return comment["v4_prompt"]["caption"]["base_caption"]
    except (KeyError, TypeError):
        return comment.get("prompt", "")


def char_captions(comment: dict[str, Any]) -> list[dict]:
    try:
        return list(comment["v4_prompt"]["caption"]["char_captions"])
    except (KeyError, TypeError):
        return []
