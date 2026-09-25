import json
from pathlib import Path

import pytest
from PIL import Image

from nai_artists import config

ROOT = Path(__file__).resolve().parent.parent
# Real V5 PNGs (UI prompt layout, `artist:fuhrriel`, 1088x960, seed 507589385) and the template solo.png reproduces.
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SOLO = FIXTURES / "solo.png"
DUO = FIXTURES / "duo.png"
TEMPLATES = FIXTURES / "templates"


@pytest.fixture(autouse=True)
def fixture_templates(monkeypatch):
    """templates/ is the user's own (git-ignored); tests read tests/fixtures/templates/ instead."""
    monkeypatch.setattr(config, "TEMPLATES_DIR", TEMPLATES)


@pytest.fixture
def solo_comment():
    with Image.open(SOLO) as im:
        return json.loads(im.text["Comment"])


@pytest.fixture
def duo_comment():
    with Image.open(DUO) as im:
        return json.loads(im.text["Comment"])
