"""settings.toml + templates/*.txt loading, hashing, and prompt assembly.

Both files are re-read on every call. They are small; no caching, no watcher.

Template file layout:

    ---
    name: Solo girl          # YAML front matter
    primary: true
    enabled: true
    sort: 10                 # optional
    chars:                   # optional, mirrors v4_prompt.caption.char_captions
      - caption: "girl, "
        x: 0.5
        y: 0.5
    ---
    <prompt body, byte-exact>

The body is everything after the closing '---' line. One trailing newline is
tolerated (editors add it) and stripped; everything else, including trailing
', ', is preserved because that is what the NAI UI writes.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import config
from .match import default_name, strip_artists

FRONT_MATTER_DELIM = "---"
TEMPLATE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class TemplateError(ValueError):
    pass


@dataclass(frozen=True)
class Pacing:
    gap_ms: int = 2000  # fixed wait after every generation request


@dataclass(frozen=True)
class Battery:
    check_every: int = 10
    min_percent: int = 5
    anlas_per_image: int = 30  # hard floor once the battery is empty: never start an image the balance can't cover


@dataclass(frozen=True)
class Refs:
    count: int = 3
    extra_tags: str = "-video -animated -comic"
    use_sample: bool = False
    per_second: float = 8.0  # every Gelbooru request (search + downloads), evenly spaced; their limit is 10/s


NON_HASHED_TABLES = ("pacing", "battery", "refs")


@dataclass(frozen=True)
class Settings:
    model: str
    width: int
    height: int
    steps: int
    scale: float
    sampler: str
    noise_schedule: str
    seed: int
    artist_line: str
    negative: str
    pacing: Pacing
    battery: Battery
    raw: dict = field(repr=False, compare=False)
    refs: Refs = Refs()

    def hash_material(self) -> dict:
        """Everything in settings.toml except [pacing], [battery] and [refs]; feeds Template.hash."""
        return {k: v for k, v in self.raw.items() if k not in NON_HASHED_TABLES}


@dataclass(frozen=True)
class CharCaption:
    caption: str
    x: float
    y: float

    def as_positive(self) -> dict:
        return {"char_caption": self.caption, "centers": [{"x": self.x, "y": self.y}]}

    def as_negative(self) -> dict:
        return {"char_caption": "", "centers": [{"x": self.x, "y": self.y}]}


@dataclass(frozen=True)
class Template:
    id: str
    name: str
    primary: bool
    enabled: bool
    sort: int | None
    chars: tuple[CharCaption, ...]
    body: str
    hash: str
    path: Path


def load_settings(path: Path | None = None) -> Settings:
    path = path or config.SETTINGS_FILE
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    gap = raw.get("pacing", {}).get("gap_ms", Pacing.gap_ms)
    if isinstance(gap, bool) or not isinstance(gap, (int, float)) or gap < 0:
        raise TemplateError("settings.toml [pacing].gap_ms must be one number of milliseconds >= 0")
    pacing = Pacing(gap_ms=int(gap))
    b = raw.get("battery", {})
    battery = Battery(check_every=int(b.get("check_every", 10)), min_percent=int(b.get("min_percent", 5)),
                      anlas_per_image=int(b.get("anlas_per_image", 30)))
    if battery.check_every < 1 or not (0 <= battery.min_percent <= 100) or battery.anlas_per_image < 1:
        raise TemplateError("settings.toml [battery]: check_every >= 1, 0 <= min_percent <= 100, anlas_per_image >= 1")
    r = raw.get("refs", {})
    refs = Refs(count=int(r.get("count", 3)), extra_tags=str(r.get("extra_tags", Refs.extra_tags)),
                use_sample=bool(r.get("use_sample", False)), per_second=float(r.get("per_second", 8)))
    if not 1 <= refs.count <= 20 or not 0 < refs.per_second <= 10:
        raise TemplateError("settings.toml [refs]: 1 <= count <= 20, 0 < per_second <= 10 (Gelbooru allows 10/s)")
    try:
        return Settings(
            model=str(raw["model"]),
            width=int(raw["width"]),
            height=int(raw["height"]),
            steps=int(raw["steps"]),
            scale=float(raw["scale"]),
            sampler=str(raw["sampler"]),
            noise_schedule=str(raw["noise_schedule"]),
            seed=int(raw["seed"]),
            artist_line=str(raw["artist_line"]),
            negative=str(raw["negative"]),
            pacing=pacing,
            battery=battery,
            raw=raw,
            refs=refs,
        )
    except KeyError as e:
        raise TemplateError(f"settings.toml missing key {e}") from None


def split_front_matter(text: str) -> tuple[dict, str]:
    """Return (front_matter_dict, body). Body is byte-exact minus one trailing newline."""
    lines = text.split("\n")
    if not lines or lines[0].rstrip("\r") != FRONT_MATTER_DELIM:
        raise TemplateError("template must start with a '---' line")
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r") == FRONT_MATTER_DELIM:
            fm_text = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1 :])
            if body.endswith("\n"):
                body = body[:-1]
            fm = yaml.safe_load(fm_text) or {}
            if not isinstance(fm, dict):
                raise TemplateError("front matter must be a mapping")
            return fm, body
    raise TemplateError("front matter never closed with '---'")


def _parse_chars(raw) -> tuple[CharCaption, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise TemplateError("chars must be a list")
    out = []
    for c in raw:
        if not isinstance(c, dict) or "caption" not in c:
            raise TemplateError("each chars entry needs caption, x, y")
        out.append(CharCaption(str(c["caption"]), float(c.get("x", 0.5)), float(c.get("y", 0.5))))
    return tuple(out)


def template_hash(body: str, chars: tuple[CharCaption, ...], settings: Settings) -> str:
    material = {
        "body": body,
        "chars": [[c.caption, c.x, c.y] for c in chars],
        "settings": settings.hash_material(),
    }
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def load_template(path: Path, settings: Settings) -> Template:
    fm, body = split_front_matter(path.read_text(encoding="utf-8"))
    chars = _parse_chars(fm.get("chars"))
    tid = path.stem
    sort = fm.get("sort")
    return Template(
        id=tid,
        name=str(fm.get("name", tid)),
        primary=bool(fm.get("primary", False)),
        enabled=bool(fm.get("enabled", True)),
        sort=int(sort) if sort is not None else None,
        chars=chars,
        body=body,
        hash=template_hash(body, chars, settings),
        path=path,
    )


def load_templates(settings: Settings | None = None, directory: Path | None = None) -> list[Template]:
    settings = settings or load_settings()
    directory = directory or config.TEMPLATES_DIR
    items = [load_template(p, settings) for p in sorted(directory.glob("*.txt"))]
    # sort key if present, else filename; sorted ones come first
    items.sort(key=lambda t: (t.sort is None, t.sort if t.sort is not None else 0, t.id))
    return items


def get_template(tid: str, settings: Settings | None = None) -> Template:
    settings = settings or load_settings()
    path = config.TEMPLATES_DIR / f"{tid}.txt"
    if not path.is_file():
        raise TemplateError(f"no such template: {tid} ({path})")
    return load_template(path, settings)


def render_front_matter(name: str, primary: bool, enabled: bool, chars: list[CharCaption] | tuple[CharCaption, ...], sort: int | None = None) -> str:
    fm: dict = {"name": name, "primary": primary, "enabled": enabled}
    if sort is not None:
        fm["sort"] = sort
    if chars:
        fm["chars"] = [{"caption": c.caption, "x": c.x, "y": c.y} for c in chars]
    return yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, default_flow_style=False)


def template_text(name: str, body: str, chars=(), primary=False, enabled=True, sort=None) -> str:
    return f"{FRONT_MATTER_DELIM}\n{render_front_matter(name, primary, enabled, chars, sort)}{FRONT_MATTER_DELIM}\n{body}\n"


def write_template(path: Path, name: str, body: str, chars=(), primary=False, enabled=True, sort=None) -> None:
    path.write_text(template_text(name, body, chars, primary, enabled, sort), encoding="utf-8", newline="\n")


# --- prompt assembly -------------------------------------------------------

def artist_prefix(settings: Settings, artists: list[str]) -> str:
    """One artist_line per artist, {artist} replaced by the bare tag (no 'artist:' prefix)."""
    return "".join(settings.artist_line.replace("{artist}", a) for a in artists)


def build_prompt(settings: Settings, template: Template, artists: list[str]) -> str:
    return artist_prefix(settings, artists) + template.body


def build_v4_prompts(settings: Settings, template: Template, artists: list[str]) -> tuple[str, dict, dict]:
    """Return (prompt, v4_prompt, v4_negative_prompt) exactly as the NAI UI shapes them."""
    prompt = build_prompt(settings, template, artists)
    v4_prompt = {
        "caption": {
            "base_caption": prompt,
            "char_captions": [c.as_positive() for c in template.chars],
        },
        "use_coords": False,
        "use_order": bool(template.chars),
        "legacy_uc": False,
    }
    v4_negative = {
        "caption": {
            "base_caption": settings.negative,
            "char_captions": [c.as_negative() for c in template.chars],
        },
        "use_coords": False,
        "use_order": False,
        "legacy_uc": False,
    }
    return prompt, v4_prompt, v4_negative


# --- creating a template from an image ---------------------------------------

def chars_from_captions(char_captions: list[dict]) -> tuple[CharCaption, ...]:
    """NAI `char_captions` dicts -> CharCaption (first center only, which is all the UI writes)."""
    out = []
    for c in char_captions or []:
        centers = c.get("centers") or [{}]
        out.append(CharCaption(str(c.get("char_caption", "")), float(centers[0].get("x", 0.5)), float(centers[0].get("y", 0.5))))
    return tuple(out)


def create_from_image(
    comment: dict,
    tid: str,
    name: str | None = None,
    *,
    primary: bool = False,
    enabled: bool = True,
    sort: int | None = None,
    force: bool = False,
    settings: Settings | None = None,
) -> tuple[Template, list[str]]:
    """Write templates/<tid>.txt from a PNG's Comment: body = base_caption minus artist tokens, chars copied.

    Size, steps, scale, seed, sampler and negative of the image are ignored (settings are global).
    Returns (template, artists found in the image) so the caller can register the image as a cell.
    """
    from .meta import base_caption, char_captions  # local: meta is standalone, keep it that way

    if not TEMPLATE_ID.match(tid):
        raise TemplateError(f"bad template id {tid!r}: use [a-z0-9_-], starting with a letter or digit")
    path = config.TEMPLATES_DIR / f"{tid}.txt"
    if path.exists() and not force:
        raise TemplateError(f"template {tid} already exists at {path} (force to overwrite)")
    artists, body = strip_artists(base_caption(comment))
    if not body.strip():
        raise TemplateError("prompt is empty once artist tokens are removed")
    chars = chars_from_captions(char_captions(comment))
    config.TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    write_template(path, name or default_name(body), body, chars=chars, primary=primary, enabled=enabled, sort=sort)
    return load_template(path, settings or load_settings()), artists


def update_front_matter(path: Path, changes: dict) -> None:
    """Flip enabled / primary / sort (None removes sort) / name in place. The body stays byte-exact."""
    fm, body = split_front_matter(path.read_text(encoding="utf-8"))
    allowed = {"enabled", "primary", "sort", "name"}
    bad = set(changes) - allowed
    if bad:
        raise TemplateError(f"cannot change {sorted(bad)} via the API; edit the file")
    fm.update(changes)
    sort = fm.get("sort")
    write_template(
        path, str(fm.get("name", path.stem)), body,
        chars=_parse_chars(fm.get("chars")), primary=bool(fm.get("primary", False)),
        enabled=bool(fm.get("enabled", True)), sort=int(sort) if sort is not None else None,
    )
