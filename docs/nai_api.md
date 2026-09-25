# NovelAI Image API — working notes (V5 focus)

Researched 2026-09-20 against the official spec: <https://image.novelai.net/docs/index.html>
(raw Swagger JSON at `https://image.novelai.net/docs/doc.json`, title "Omegalaser API").

Confidence legend: **[spec]** from the OpenAPI doc, **[official]** from docs.novelai.net / NAI journal,
**[3p]** from third-party clients or writeups that hit the live API, **[unverified]** needs a real request or file to confirm.

---

## 1. Hosts and auth

| Purpose | Host |
|---|---|
| Image generation, tag suggest, user/subscription | `https://image.novelai.net` |
| Account / login (legacy primary API) | `https://api.novelai.net` |
| Text generation | `https://text.novelai.net` |

- Auth header: `Authorization: Bearer <persistent token>` **[spec]**. Token comes from the NAI web UI
  (Account → Get Persistent API Token) or `POST /user/create-persistent-token`.
- Old host `api.novelai.net/ai/generate-image` returns 404 now. Use `image.novelai.net` **[3p]**.
- ToS, verbatim from the spec (on every generation endpoint): "According to our Terms of Service, all generation
  requests must be initiated by a human action. Automating text or image generation to create excessive load on
  our systems is not allowed." Our rule: **one human action → at most one generation request**, enforced
  server-side. A queue may serialize clicks (one generation at a time per account); it never turns one click into
  many requests. No batch fill, no scheduler, no retries.

## 2. Endpoints we care about **[spec]**

| Method | Path | Notes |
|---|---|---|
| POST | `/ai/generate-image` | Main one. Returns `application/zip` by default, `application/json` (base64) if `Accept: application/json`. |
| POST | `/ai/generate-image-stream` | Same body, SSE stream (`parameters.stream: "sse"` or `"msgpack"`). Events: Intermediate, Final, Error. Not needed for MVP. |
| GET | `/ai/generate-image/suggest-tags?model=&prompt=&lang=en` | Tag autocomplete. Response `{tags: [{tag, count, confidence}]}`. Useful for artist name autocomplete in the paste box. |
| GET | `/user/subscription` | `tier`, `active`, `perks`, `trainingStepsLeft` (= Anlas), `usage` (= V5 Opus battery, see §6). |
| POST | `/ai/augment-image`, `/ai/upscale`, `/ai/encode-vibe` | Not needed. |

Optional header `x-correlation-id` (6 alphanumerics) is echoed on 500s for support tickets.

## 3. Models

| Model ID | Status |
|---|---|
| `nai-diffusion-5-full` | V5 Full, released 2026-08-21. Live-confirmed by a dev.to writeup and used by NAIWeaver and two ComfyUI nodes **[3p]**. |
| `nai-diffusion-5-curated` | V5 Curated **[3p]** |
| `nai-diffusion-5-full-inpainting` | V5 Full inpaint. Curated inpaint still routes to V4.5 **[3p]** |
| `nai-diffusion-4-5-full`, `nai-diffusion-4-5-curated` | Previous gen, still free on Opus under normal limits. |

V5 facts **[official]**: 32-channel VAE, ~2x V4.5 size, prompt limit ~1471 effective tokens (Full) / ~703 (Curated),
natural language and Japanese prompting, free-drag character positioning (22+ chars), native transparency
(`transparent background` / `has alpha`, strengthen with `2.1::transparent background::`), multi-panel comics.
Special dataset tags: `fur dataset,` and `background dataset,`.

Not available on V5 at launch **[3p]**: Vibe Transfer, Character Reference, SMEA / SMEA DYN, Variety+ (skip_cfg_above_sigma).

## 4. Request body

Top-level **[spec]**: `{ "input": str, "model": str, "action": "generate", "parameters": {...} }`.
`input` is the prompt again (legacy); the model actually reads `parameters.v4_prompt`.

Working V5 body, verified live by the dev.to author **[3p]**:

```json
{
  "input": "PROMPT",
  "model": "nai-diffusion-5-full",
  "action": "generate",
  "parameters": {
    "params_version": 4,
    "width": 832,
    "height": 1216,
    "scale": 5,
    "sampler": "k_euler_ancestral",
    "steps": 23,
    "seed": 1234567890,
    "n_samples": 1,
    "ucPreset": 0,
    "qualityToggle": false,
    "sm": false,
    "sm_dyn": false,
    "dynamic_thresholding": false,
    "controlnet_strength": 1,
    "legacy": false,
    "add_original_image": false,
    "cfg_rescale": 0,
    "noise_schedule": "karras",
    "legacy_v3_extend": false,
    "uncond_scale": 1,
    "negative_prompt": "NEGATIVE",
    "prompt": "PROMPT",
    "reference_image_multiple": [],
    "reference_information_extracted_multiple": [],
    "reference_strength_multiple": [],
    "extra_noise_seed": 1234567890,
    "v4_prompt": {
      "use_coords": false,
      "use_order": false,
      "caption": { "base_caption": "PROMPT", "char_captions": [] }
    },
    "v4_negative_prompt": {
      "use_coords": false,
      "use_order": false,
      "caption": { "base_caption": "NEGATIVE", "char_captions": [] }
    }
  }
}
```

Field notes:

- `v4_prompt` and `v4_negative_prompt` are **mandatory**; omitting either gives a bare 500 **[3p]**.
- `params_version`: official client sends 4 for V5, 3 for V4.x. Live test showed 4/3/1/0 produce identical pixels with this body, so it's a versioning hint, not a behaviour switch **[3p]**. Send 4.
- `noise_schedule`: V5 is Karras-only; clients force `"karras"` **[3p]**. Other schedules on V4.5: `native`, `karras`, `exponential`, `polyexponential`.
- Samplers seen: `k_euler`, `k_euler_ancestral`, `k_dpmpp_2s_ancestral`, `k_dpmpp_2m`, `k_dpmpp_2m_sde`, `k_dpmpp_sde`, `ddim_v3`. V5 UI recommends Euler Ancestral **[official/3p]**.
- `sm` / `sm_dyn`: keep `false` on V5. Sending stale SMEA flags on V4.5 has caused 500s **[3p]**.
- `qualityToggle: true` makes the server append quality tags (§5). We'll set it `false` and put tags in the template explicitly so the stored prompt equals the sent prompt.
- `ucPreset`: which built-in negative preset the server appends. Index mapping inferred from clients: 0 Heavy, 1 Light, 2 Human Focus, 3 None **[unverified]**. Same logic as above: send 3 (None) and write the negative ourselves.
- `image_format`: `"png"` or `"webp"` **[spec]**. PNG for archival, metadata lives there.
- `char_captions[].centers[]` are `{x, y}` floats; V5 uses free coordinates (3-decimal precision in NAIWeaver) rather than V4's 5x5 grid **[3p]**. `use_coords: true` when you supply them.
- Resolution: multiples of 64. V5 hard cap 3,145,728 px **[3p]**.

Presets (V3+ convention, still what the UI offers) **[3p]**:

| Preset | Portrait | Landscape | Square |
|---|---|---|---|
| Small | 512x768 | 768x512 | 640x640 |
| Normal | 832x1216 | 1216x832 | 1024x1024 |
| Large | 1024x1536 | 1536x1024 | 1472x1472 |

Defaults the UI ships for V5: 23 steps, guidance 7.0 per NAIWeaver's changelog; the V5 review articles recommend guidance ~5 and warn V5 Full gets grainy above that, WeavAI says ~4 **[3p, conflicting]**. **Action: generate one image in the web UI with defaults and read its `Comment` chunk (§8) to settle this before locking templates.**

## 5. Prompt conventions

Quality tags the toggle appends **[official]**:

- V5 Full / Curated, standard: `, very aesthetic, masterpiece, no text`
- V5 Full / Curated, light: `, very aesthetic, amazing quality, no text`
- V4.5 Full: `, location, very aesthetic, masterpiece, no text`

Undesired Content presets, V5 Full and Curated (identical) **[official]**:

- Heavy: `lowres, artistic error, film grain, scan artifacts, worst quality, bad quality, jpeg artifacts, very displeasing, chromatic aberration, dithering, halftone, screentone, multiple views, logo, too many watermarks, negative space, blank page`
- Light: `lowres, bad hands, bad anatomy, artistic error, sepia, white haze, worst quality, very displeasing, jpeg artifacts, 0::ai-generated::`
- Human Focus: Heavy + `, @_@, mismatched pupils, glowing eyes, bad anatomy`

Emphasis **[official]**: `{tag}` x1.05 per brace, `[tag]` /1.05 per bracket, numeric `1.3::tag one, tag two::`. Weights below 1 weaken, negative weights (V4.5+) invert. Works in UC too, reversed.

Artist tags: V4+ convention is `artist:name` (Danbooru-style, spaces as spaces or underscores both accepted). Nothing in the V5 docs changes the syntax; whether V5 responds to the same names as V4.5 is literally the experiment this project exists for **[unverified]**.

New V5 style tags worth a template slot **[official]**: `depthness`, `low/medium/high/ultra complexity`, `meta:novel era`, `meta:golden era`, `visual novel art|bg|cg|chibi|sprite`, `attractive male`.

## 6. Cost, limits, concurrency

- **Opus free generation (V4.5 and older)** **[official]**: 0 Anlas when ≤ "normal" size (≤ 1024x1024 = 1,048,576 px), ≤ 28 steps, `n_samples: 1`.
- **V5 on Opus** **[official/3p]**: not unlimited. A "battery" (`usage` object on `/user/subscription`) drains per image and refills continuously, roughly 0.5 %/h, empty to full ≈ 1 week, sized for ~1,800 normal generations per week. When `usage.isNegative` is true, V5 generations cost Anlas from `trainingStepsLeft`.

  ```json
  "usage": { "percent": 0-100, "isNegative": false, "timeUntilNextPercent": 3600 }
  "trainingStepsLeft": { "fixedTrainingStepsLeft": N, "purchasedTrainingSteps": N }
  ```
  Anlas balance = `fixedTrainingStepsLeft + purchasedTrainingSteps`.
- **V5 Anlas cost when battery is empty** (observed, defaults) **[3p]**: small 11, normal 26, large 39. Scales with steps.
- **Concurrency**: one generation at a time per account. Queue must be serial.
- **429**: spec literally says "Rate limited. API clients should not attempt to retry." Treat as fatal for the job, surface it, pause the queue. Third-party tools space requests by a few seconds; 6 s between requests at concurrency 1 never triggered it **[3p]**.
- **402**: "Not enough Anlas" **[spec]**.
- **400**: bad model enum, malformed body. **401**: token. **500**: often a body-shape problem (missing `v4_prompt`), not a server outage.
- There is no price-preview endpoint; `/ai/generate-image/request-price` is 404 **[3p]**. Estimate locally and diff `trainingStepsLeft` before and after if you want ground truth.

Plan for the app: check `/user/subscription` before a batch, show battery % and Anlas, refuse to start a V5 batch that would go negative unless the user opts in.

## 7. Response handling

Default: HTTP 200, body is a zip, first entry `image_0.png` **[3p]**. With `Accept: application/json`: HTTP 201, `{"images":[{"image": "<base64>", "index": 0, "seed": N}]}` **[spec]**. Use the JSON form; it hands back the actual seed, which matters when seed is 0/random.

Python sketch:

```python
r = httpx.post(f"{HOST}/ai/generate-image", json=body,
               headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"}, timeout=180)
r.raise_for_status()
img = r.json()["images"][0]
png = base64.b64decode(img["image"]); seed = img["seed"]
```

## 8. PNG metadata (for import)

Two copies of the same data **[official repo NovelAI/novelai-image-metadata]**:

1. **tEXt chunks**. Keys used by NAI outputs (from V3/V4 files; V5 expected identical, confirm on first real file **[unverified]**):
   `Title` = `AI generated image`, `Description` = positive prompt, `Software` = `NovelAI`,
   `Source` = `NovelAI Diffusion V5 <hash>` (model name + short hash), `Generation time`, `Comment` = JSON string.
   `Comment` JSON carries `prompt`, `uc`, `seed`, `steps`, `width`, `height`, `scale`, `sampler`, `noise_schedule`,
   `cfg_rescale`, `v4_prompt`, `v4_negative_prompt`, `request_type`, `signed_hash`, plus a pile of internal flags.
   The live test found **no `params_version`** in it.
2. **Stealth pnginfo in the alpha channel**: LSB of alpha, magic `stealth_pngcomp`, then 32-bit big-endian length in bits,
   then gzip'd JSON of the same dict (with `Comment` as a nested JSON string). Survives PNG re-encoding but not resizing. Also carries FEC data so a few flipped pixels still verify.

Import strategy: read `Image.open(p).text["Comment"]` first; fall back to the alpha channel decoder only if the chunks are gone (Discord, Twitter, some galleries strip them). Match on `v4_prompt.caption.base_caption` if present, else `prompt`.

Quick inspection one-liner:

```bash
python3 -c 'import sys,json;from PIL import Image;t=Image.open(sys.argv[1]).text;print({k:v for k,v in t.items() if k!="Comment"});print(json.dumps(json.loads(t["Comment"]),indent=1))' some.png
```

## 9. Confirmed from two real V5 PNGs (2026-09-20, generated in the web UI)

Reference files: the two `artist_wlop,*.png` in the project root. Keep them, they double as import test fixtures.

- `Source` = `NovelAI Diffusion V5 0ADF9AB7`; `Title` = `NovelAI generated image`; also `Description`, `Software`, `Generation time`, `Comment`.
- `Comment` has `model_name`, `model_hash`, `v4_prompt`, `v4_negative_prompt`, `uc`, `seed`, everything we need. No `params_version`.
- The UI's V5 defaults, as actually used: **28 steps, scale 5.0, `k_euler_ancestral`, `karras`, `cfg_rescale 0`, `uncond_scale 0.0`, `sm`/`sm_dyn` false, `dynamic_thresholding` false, `prefer_brownian true`, `deliberate_euler_ancestral_bug false`, `skip_cfg_above_sigma null`, `quality_boost false`, `tag_hint_qt 0`.** The 7.0 figure from NAIWeaver is wrong for this account's usage; we go with 5.0.
- The UI fills `char_captions` even for a single character (`"girl, "` at 0.5/0.5) with `use_order: true`, `use_coords: false`. Negative side mirrors the same centers with empty captions. Templates must reproduce this exactly or imports won't match.
- Prompt layout the user actually writes: blocks separated by blank lines, artist first, subject block wrapped `1::...::`, quality block wrapped `1.1::...::`, scene last. Import matching (`match.py`) keys off this.
- The UC contains none of the built-in preset strings, so we send `ucPreset: 3` and `qualityToggle: false` and carry the full negative ourselves. (`tag_hint_uc_preset: 0` in the file is a UI hint only.)

Still open: nothing blocking. `Accept: application/json` seed echo gets checked on the first live call.

## Sources

- OpenAPI spec: https://image.novelai.net/docs/doc.json
- V5 release post: https://journal.novelai.net/image-generation-novelai-diffusion-v5-is-here-c2df7c6b8d2d/
- Models: https://docs.novelai.net/en/image/models/
- UC presets: https://docs.novelai.net/en/image/undesiredcontent/
- Quality tags: https://docs.novelai.net/en/image/qualitytags/
- Emphasis: https://docs.novelai.net/en/image/strengthening-weakening/
- Live V5 request writeup: https://dev.to/ilan_kim/calling-the-novelai-v5-api-directly-nai-diffusion-5-full-request-body-paramsversion-4310-133a
- NAIWeaver changelog (V5 params, battery): https://github.com/ststoryweaver/NAIWeaver/blob/master/CHANGELOG.md
- ComfyUI_NAIDGenerator (body shape, model list): https://github.com/bedovyy/ComfyUI_NAIDGenerator
- ComfyUI_RS_NAI_API_Request (params_version 3 vs 4, allowance check): https://github.com/raspie10032/ComfyUI_RS_NAI_API_Request
- Metadata format: https://github.com/NovelAI/novelai-image-metadata
- V5 cost/battery reporting: https://note.com/tank_ai/n/nbde8e7623b28 , https://weavai.app/blog/en/2026/08/29/novelai-diffusion-v5-22-characters-transparent-bg/

## 10. Live smoke test, 2026-09-20 (recreated the solo reference image → `output.png`)

- **Result: byte-for-byte identical pixels** to the UI-generated original (mean diff 0, 100 % identical px). Same seed, same body, deterministic. Template matrices are therefore fully reproducible.
- **Set a real `User-Agent`.** Python's default `Python-urllib/3.x` gets a Cloudflare 403 on every endpoint; curl's UA and a custom one (`nai_artists/0.1`) both pass. httpx's default UA is untested, set ours explicitly regardless.
- `Accept: application/json` works: HTTP 200 (not 201 as the spec says), `{"images":[{"image": b64, "index": 0, "seed": 507589385}]}`. Seed is echoed. ~1.77 MB body for a 1088x960 image, 7.5 s round trip incl. generation.
- PNG returned via API differs from a UI download only in chunk trivia: `Title` = `AI generated image` (UI: `NovelAI generated image`), key `Generation_time` (UI: `Generation time`), and `Comment.stream` = `none`, `tag_hint_*` = null. Everything an importer keys on (`Source`, `Comment.v4_prompt`, `seed`, params) is identical.
- `api.novelai.net/user/*` now answers 400 "update to the image URL". Everything lives on `image.novelai.net`, including `/user/subscription` and `/user/information`.
- One normal-size image did not move `usage.percent` (17 → 17) nor `timeUntilNextPercent`; the battery gauge is coarse, don't try per-image accounting from it. Anlas untouched while battery is positive, as expected.
- The `settings.toml` values plus the exact `v4_prompt`/`v4_negative_prompt` structure from the reference PNG are what reproduced it.
