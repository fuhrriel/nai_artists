"""NovelAI image API client. See docs/nai_api.md.

Only two things ever call generate(): the CLI `gen` command and the server queue,
both on explicit user action. Never call it from tests or on startup.
"""

from __future__ import annotations

import base64
from typing import Any

import httpx

from . import USER_AGENT, config
from .templates import Settings, Template, build_v4_prompts

HOST = "https://image.novelai.net"


class NAIError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message

    @property
    def fatal(self) -> bool:
        """429 (rate limited) and 402 (no Anlas) must stop the queue, never retry."""
        return self.status in (429, 402)


def build_body(settings: Settings, template: Template, artists: list[str], seed: int | None = None) -> dict[str, Any]:
    """Request body reproducing the web UI's V5 defaults (docs/nai_api.md §4, §9, §10)."""
    prompt, v4_prompt, v4_negative = build_v4_prompts(settings, template, artists)
    seed = settings.seed if seed is None else seed
    return {
        "input": prompt,
        "model": settings.model,
        "action": "generate",
        "parameters": {
            "params_version": 4,
            "width": settings.width,
            "height": settings.height,
            "scale": settings.scale,
            "sampler": settings.sampler,
            "steps": settings.steps,
            "seed": seed,
            "n_samples": 1,
            "ucPreset": 3,
            "qualityToggle": False,
            "sm": False,
            "sm_dyn": False,
            "dynamic_thresholding": False,
            "controlnet_strength": 1,
            "legacy": False,
            "add_original_image": False,
            "cfg_rescale": 0,
            "noise_schedule": settings.noise_schedule,
            "legacy_v3_extend": False,
            "uncond_scale": 0.0,
            "prefer_brownian": True,
            "deliberate_euler_ancestral_bug": False,
            "skip_cfg_above_sigma": None,
            "image_format": "png",
            "negative_prompt": settings.negative,
            "prompt": prompt,
            "reference_image_multiple": [],
            "reference_information_extracted_multiple": [],
            "reference_strength_multiple": [],
            "extra_noise_seed": seed,
            "v4_prompt": v4_prompt,
            "v4_negative_prompt": v4_negative,
        },
    }


def _error_message(r: httpx.Response) -> str:
    try:
        j = r.json()
        if isinstance(j, dict):
            return str(j.get("message") or j.get("error") or j)
    except ValueError:
        pass
    return r.text[:300]


class NAIClient:
    def __init__(self, token: str | None = None, timeout: float = 180.0):
        self._client = httpx.Client(
            base_url=HOST,
            headers={
                "Authorization": f"Bearer {token or config.api_key()}",
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _check(self, r: httpx.Response) -> None:
        if r.status_code >= 400:
            raise NAIError(r.status_code, _error_message(r))

    def generate(self, body: dict[str, Any]) -> tuple[bytes, int]:
        """POST /ai/generate-image. Returns (png_bytes, seed_used)."""
        r = self._client.post("/ai/generate-image", json=body)
        self._check(r)
        data = r.json()
        try:
            img = data["images"][0]
            return base64.b64decode(img["image"]), int(img["seed"])
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise NAIError(r.status_code, f"unexpected response shape: {e}") from None

    def subscription(self) -> dict[str, Any]:
        r = self._client.get("/user/subscription")
        self._check(r)
        return r.json()

    def suggest_tags(self, prompt: str, model: str = "nai-diffusion-5-full", lang: str = "en") -> list[dict]:
        r = self._client.get("/ai/generate-image/suggest-tags", params={"model": model, "prompt": prompt, "lang": lang})
        self._check(r)
        return r.json().get("tags", [])


def battery_summary(sub: dict[str, Any]) -> dict[str, Any]:
    """Flatten /user/subscription into what the UI and CLI show."""
    usage = sub.get("usage") or {}
    steps = sub.get("trainingStepsLeft") or {}
    anlas = int(steps.get("fixedTrainingStepsLeft", 0)) + int(steps.get("purchasedTrainingSteps", 0))
    return {
        "tier": sub.get("tier"),
        "active": sub.get("active"),
        "battery_percent": usage.get("percent"),
        "is_negative": bool(usage.get("isNegative", False)),
        "time_until_next_percent": usage.get("timeUntilNextPercent"),
        "anlas": anlas,
    }


def battery_low(summary: dict[str, Any], min_percent: int) -> bool:
    """True when generating should stop: negative, or at/below the configured threshold."""
    pct = summary.get("battery_percent")
    return bool(summary.get("is_negative")) or (pct is not None and int(pct) <= min_percent)


def anlas_mode(summary: dict[str, Any]) -> bool:
    """The battery is empty (negative or 0 %): every V5 image is paid in Anlas now."""
    pct = summary.get("battery_percent")
    return bool(summary.get("is_negative")) or (pct is not None and int(pct) <= 0)


def anlas_short(summary: dict[str, Any], per_image: int) -> bool:
    """Hard stop, forced or not: paying in Anlas and the balance can't cover one more image."""
    return anlas_mode(summary) and int(summary.get("anlas") or 0) < per_image


def images_until_check(summary: dict[str, Any], battery) -> int:
    """How many images may run on this subscription reading before it must be re-fetched.

    Normally `check_every`. Once paying in Anlas it is capped by what the balance covers, so one
    window can never spend more than was there at the last check. `battery` is settings.Battery.
    """
    if anlas_mode(summary):
        return max(0, min(battery.check_every, int(summary.get("anlas") or 0) // battery.anlas_per_image))
    return battery.check_every
