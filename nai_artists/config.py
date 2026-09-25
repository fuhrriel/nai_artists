"""Paths and secrets. Generation settings live in settings.toml, not here."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)

SETTINGS_FILE = ROOT / "settings.toml"
TEMPLATES_DIR = ROOT / "templates"
DATA_DIR = Path(os.environ.get("NAI_DATA_DIR", ROOT / "data")).resolve()
FULL_DIR = DATA_DIR / "full"
THUMBS_DIR = DATA_DIR / "thumbs"
INBOX_DIR = DATA_DIR / "inbox"
REFS_DIR = DATA_DIR / "refs"  # full/<slug>/<post_id>.<ext>, thumbs/<slug>/<post_id>.webp
DB_FILE = DATA_DIR / "nai.db"


def api_key() -> str:
    key = os.environ.get("NAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("NAI_API_KEY is not set (put it in .env)")
    return key


def _env(name: str) -> str:
    """Process env first, then .env re-read now, so a key added to .env works without a restart."""
    v = os.environ.get(name, "").strip()
    if not v and (ROOT / ".env").is_file():
        v = (dotenv_values(ROOT / ".env").get(name) or "").strip()
    return v


def gelbooru_credentials() -> tuple[str, str] | None:
    """(api_key, user_id) from GELBOORU_API_KEY / GELBOORU_USER_ID, or None when not configured.

    GELBOORU_API_KEY may also hold the whole '&api_key=...&user_id=...' string from the Gelbooru
    account options page; both values are parsed out of it.
    """
    key, uid = _env("GELBOORU_API_KEY"), _env("GELBOORU_USER_ID")
    if "api_key=" in key:
        parts = dict(p.split("=", 1) for p in key.lstrip("&").split("&") if "=" in p)
        key, uid = parts.get("api_key", "").strip(), uid or parts.get("user_id", "").strip()
    return (key, uid) if key and uid else None


BASE_SLUG = "_base"  # the artist-less baseline row; reserved
# Windows won't create a file or directory with these names, on any drive.
WINDOWS_DEVICE_NAMES = {"con", "prn", "aux", "nul", *(f"{p}{i}" for p in ("com", "lpt") for i in range(1, 10))}


def artist_slug(tag: str) -> str:
    """Tag lowercased, non-alnum -> '_'. Combos are joined with '+' by the caller."""
    slug = "".join(c if c.isalnum() else "_" for c in tag.strip().lower())
    if not slug or slug == BASE_SLUG:
        raise ValueError(f"invalid artist tag {tag!r}: empty or reserved")
    return slug + "_" if slug in WINDOWS_DEVICE_NAMES else slug


def combo_slug(tags: list[str]) -> str:
    """Slug for a list of artists; the empty list is the baseline."""
    return "+".join(artist_slug(t) for t in tags) if tags else BASE_SLUG
