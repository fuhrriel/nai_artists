"""Prompt <-> template matching for imports.

Two jobs:

1. `strip_artists(prompt)`: pull every `artist:` token out of a prompt, byte-exact otherwise.
   Handles the UI layout (`artist:wlop,` in its own block), ours (`::artist:wlop,::` line),
   weighted lines (`1.2::artist:a, artist:b::`), inline tokens (`solo, artist:a, 1girl`)
   and brace/bracket emphasis (`{artist:a}`). A line that was only artists is removed;
   a leading block left empty is dropped together with its blank-line separator.
   This is what template-from-image writes, so it must not normalize anything.

2. `match(prompt, chars, templates)`: compare the stripped prompt to every template body after
   normalization (blank-line blocks; whitespace collapsed; trailing commas/spaces per block
   dropped), plus the character captions. Exact or nothing; on no exact match the closest
   template is returned with a diff for the UI to show.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import config

if TYPE_CHECKING:  # match.py must not import templates.py at runtime (templates imports us)
    from .templates import CharCaption, Template

_NAME = r"[^,\n:{}\[\]]+"
_TOK = rf"[{{\[]*(?<!\w)artist:{_NAME}[}}\]]*"
# whole line = optional weight opener, one or more artist tokens, optional closer / trailing comma
_ARTIST_LINE = re.compile(rf"^[ \t]*(?:-?[\d.]*::)?[ \t]*(?:{_TOK}[ \t]*,?[ \t]*)+(?:::)?[ \t]*,?[ \t]*$")
_TOKEN = re.compile(rf"[{{\[]*(?<!\w)artist:({_NAME})")
_INLINE_WITH_COMMA = re.compile(rf"{_TOK}[ \t]*,[ \t]*")
_INLINE_LAST = re.compile(rf"(?:,[ \t]*)?{_TOK}")
_LEADING_BLANK = re.compile(r"\A(?:[ \t]*\n)+")
_WEIGHT_WRAP = re.compile(r"^\s*-?[\d.]*::(.*?)::\s*,?\s*$", re.S)


def strip_artists(prompt: str) -> tuple[list[str], str]:
    """Return (artist tags in order of appearance, deduped by slug; prompt with them removed)."""
    seen: dict[str, str] = {}
    for m in _TOKEN.finditer(prompt):
        tag = m.group(1).strip()
        if not tag:
            continue
        try:
            slug = config.artist_slug(tag)
        except ValueError:
            continue
        seen.setdefault(slug, tag)
    kept = []
    for line in prompt.split("\n"):
        if _ARTIST_LINE.fullmatch(line):
            continue
        line = _INLINE_WITH_COMMA.sub("", line)
        line = _INLINE_LAST.sub("", line)
        kept.append(line)
    body = _LEADING_BLANK.sub("", "\n".join(kept))
    return list(seen.values()), body


# --- normalization --------------------------------------------------------------

_BLANK_LINE = re.compile(r"\n[ \t]*\n")
_WS = re.compile(r"\s+")


def norm_block(block: str) -> str:
    return _WS.sub(" ", block).strip().rstrip(", ").strip()


def normalize(text: str) -> str:
    """Blocks split on blank lines, whitespace collapsed, trailing ', ' dropped, empties removed."""
    blocks = [norm_block(b) for b in _BLANK_LINE.split(text)]
    return "\n\n".join(b for b in blocks if b)


def norm_chars(chars) -> tuple[tuple[str, float, float], ...]:
    """Accepts NAI `char_captions` dicts or CharCaption objects; first center only, 3 decimals."""
    out = []
    for c in chars or ():
        if isinstance(c, dict):
            caption = c.get("char_caption", "")
            centers = c.get("centers") or [{}]
            x, y = centers[0].get("x", 0.5), centers[0].get("y", 0.5)
        else:
            caption, x, y = c.caption, c.x, c.y
        out.append((norm_block(str(caption)), round(float(x), 3), round(float(y), 3)))
    return tuple(out)


def default_name(body: str) -> str:
    """Name suggestion for a new template: the first block minus its weight wrapper."""
    first = normalize(body).split("\n\n", 1)[0]
    m = _WEIGHT_WRAP.match(first)
    return norm_block(m.group(1) if m else first) or "untitled"


# --- matching -------------------------------------------------------------------

@dataclass
class MatchResult:
    template: "Template | None"
    artists: list[str]
    exact: bool
    diffs: list[str] = field(default_factory=list)
    body: str = ""  # prompt with artist tokens stripped, not normalized

    @property
    def template_id(self) -> str | None:
        return self.template.id if self.template else None


def _diff(template_body: str, image_body: str, t_chars, i_chars) -> list[str]:
    lines = list(difflib.unified_diff(
        normalize(template_body).split("\n"), normalize(image_body).split("\n"),
        fromfile="template", tofile="image", lineterm="", n=1,
    ))
    if t_chars != i_chars:
        lines.append(f"chars: template {list(t_chars)} != image {list(i_chars)}")
    return lines


def match(prompt: str, chars, templates: "list[Template]") -> MatchResult:
    """`prompt` is the image's base_caption (artists still in it), `chars` its char_captions."""
    artists, body = strip_artists(prompt)
    nb, nc = normalize(body), norm_chars(chars)
    best, best_score = None, -1.0
    for t in templates:
        tb, tc = normalize(t.body), norm_chars(t.chars)
        if tb == nb and tc == nc:
            return MatchResult(t, artists, True, [], body)
        score = difflib.SequenceMatcher(None, tb, nb).ratio() + (0.1 if tc == nc else 0.0)
        if score > best_score:
            best, best_score = t, score
    if best is None:
        return MatchResult(None, artists, False, ["no templates defined"], body)
    return MatchResult(best, artists, False, _diff(best.body, body, norm_chars(best.chars), nc), body)
