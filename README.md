# nai_artists

Which `artist:` tags actually do anything on NovelAI Diffusion V5, and how does each behave across subjects?
This tool generates a fixed matrix (artists × subject templates) with one seed and identical settings,
shows it as a local web gallery, and lets you rate, compare against an artist-less baseline, and regenerate.

Single user, localhost only. Python 3.12+, FastAPI, SQLite, vanilla JS. No build step. Linux, macOS, Windows.

## Before you start

- **This spends your NovelAI battery and Anlas.** Every cell is a real generation, started by you clicking it.
  The queue stops at `[battery].min_percent` (5 % by default) unless you force it.
- A NovelAI subscription that can generate V5 images, and its persistent API token:
  novelai.net → User Settings → Account → Get Persistent API Token (starts with `pst-`).
- [uv](https://docs.astral.sh/uv/getting-started/installation/). It downloads a suitable Python by itself.
- git.

## One click, one image

NovelAI's API documentation says: "According to our Terms of Service, all generation requests must be initiated
by a human action. Automating text or image generation to create excessive load on our systems is not allowed."

So this tool never turns one action into many requests. There is no "generate everything", no fill, no batch
regenerate: each image is one click on one matrix cell (or one `nai gen` run). The server enforces this, not
just the UI. Clicked cells wait in a queue because NovelAI runs one generation at a time per account; the queue
sends them one by one, `[pacing].gap_ms` apart, to keep load on NovelAI's servers low. Nothing is retried, and
nothing generates on startup: cells still queued when the server stops are dropped.

## Setup

bash / zsh:

```bash
git clone <repo url> nai_artists && cd nai_artists
uv sync
cp .env.example .env                  # then paste your token into it
cp -r examples/templates templates    # starter template
```

PowerShell:

```powershell
git clone <repo url> nai_artists; cd nai_artists
uv sync
Copy-Item .env.example .env
Copy-Item -Recurse examples\templates templates
```

`.env` holds your token and is git-ignored. On Windows, make sure Notepad didn't save it as `.env.txt`
(`dir .env*` shows the real name). What goes in it:

```
NAI_API_KEY=pst-...
```

Optional: `NAI_DATA_DIR=/somewhere/else` (default `./data`, git-ignored).

Optional, for reference images: your Gelbooru API credentials (Gelbooru → My Account → Options →
"API Access Credentials"). Either both values, or paste the whole `&api_key=...&user_id=...` string
into `GELBOORU_API_KEY`. Picked up without a restart.

```
GELBOORU_API_KEY=...
GELBOORU_USER_ID=...
```

Generation settings live in `settings.toml`. Templates are files in `templates/`, which is git-ignored:
they are yours. Both are read on every request, so edit and refresh. No restart needed.

Run everything from the checkout with `uv run`. `settings.toml`, `templates/` and `.env` are looked up
relative to the repo, so a `uv tool install` / `pip install` from git gives you a `nai` that finds none of them.

## Run the web app

```bash
uv run nai serve            # http://127.0.0.1:8000
uv run nai serve --reload   # dev: restart on code changes
```

There is no authentication. Keep the default `--host 127.0.0.1`: with `--host 0.0.0.0` anyone who can reach
the port can queue generations on your account.

Tabs:

- **Matrix**: rows are artists, columns are templates, the first row is always the artist-less baseline.
  Click a thumbnail for the lightbox. Filter, sort, rate from here. ⟳ on a cell generates that one image: a
  missing or stale cell right away, a fresh one after a confirm (`g` in the lightbox does the same). Press `?`
  for every shortcut.
  Drag column headers to reorder them, click to collapse. `#` on a row edits its labels (free text:
  painterly, toony, dark…); click a label to filter on it, shift-click to exclude it, or use `labels ▾`.
  The **refs** column shows the artist's top-scored Gelbooru posts next to the generations; in the lightbox
  `r` puts them side by side with the generated image. Questionable / explicit refs are blurred until you
  tick "nsfw refs".
- **Add artists**: paste artist tags one per line (`artist:` optional, `a + b` for a combo). This only adds matrix
  rows; their cells start out missing until you click them. Optionally fetches Gelbooru refs for new single
  artists. Also shows the battery gauge.
- **Import**: drop PNGs generated in the NovelAI UI. The prompt is matched against templates, artists are read
  from the `artist:` tokens. Unmatched files land in `data/inbox/` untouched.
- **Templates**: enable / primary toggles, stale counts, and the settings dump. Prompt bodies are edited in
  your editor, not here.

The queue drawer (`q`) shows what is generating. The queue suspends itself when the V5 battery drops to
`[battery].min_percent`, and a click below it asks first. Forcing (confirming that prompt) keeps the queue
going until it runs dry, into Anlas if the battery empties, but it never starts an image once the battery is
empty and fewer than `[battery].anlas_per_image` Anlas are left. HTTP 429 and 402 pause the queue and are
never retried.

## CLI

Most of the UI is also a CLI command. `gen` makes exactly one image per run, behind the same battery guard:

```bash
uv run nai --help
uv run nai sub                                   # battery / Anlas
uv run nai gen -a wlop -t example                # one artist x one template
uv run nai gen -a wlop -a "ilya kuvshinov" -t example  # a combo
uv run nai gen --base -t example                 # the baseline cell
uv run nai gen -a wlop -t example --dry-run      # check the battery, show what would run
uv run nai import ~/Downloads/*.png              # register UI-made PNGs as cells
uv run nai template-from-image img.png --id catgirl --name "Catgirl"
uv run nai templates                             # ids, hashes, counts
uv run nai cells                                 # what the DB knows
uv run nai body -a wlop -t example               # the exact request body, no network
uv run nai refs -a wlop                          # fetch wlop's top-scored Gelbooru posts as refs
uv run nai refs --missing                        # every single artist never fetched
uv run nai refs -a wlop --query "wlop_(artist)"  # search another booru tag for this artist
```

File arguments get `~` and wildcards expanded by `nai` itself, so `import` and `meta` take `*.png` in cmd and
PowerShell too.

## Templates

One file per template, front matter then the prompt body verbatim, **without** artist tokens. The app prepends
`artist_line` from `settings.toml` at generation time.

```
---
name: Solo girl
primary: true
enabled: true
chars:
  - caption: "girl, "
    x: 0.5
    y: 0.5
---
1::solo, 1girl::

1.1:: location, very aesthetic, masterpiece, best quality, ...::,

outdoors,
```

`examples/templates/example.txt` is a deliberately bland starter so the app has one primary column on first
run. Delete it once you have your own.

The easiest way to make one: generate an image in the NovelAI UI, then either import it and click
"new template" or run `nai template-from-image`. The `artist:` tokens are stripped, the rest becomes the body.

Editing a body or any generation setting changes the template hash. Images generated under the old hash show a
`stale` badge; clicking ⟳ on such a cell regenerates it in place.

## Data

```
data/nai.db                       artists, images, ratings, jobs (never prompts)
data/full/<artist>/<template>.png originals with NovelAI metadata intact
data/thumbs/<artist>/<template>.webp
data/inbox/                       imported files that matched nothing
```

Prompts are only ever read from the PNGs themselves. Delete `data/` and you lose images and ratings, not templates
or settings.

## Development

```bash
uv run pytest -q     # no network, ever
```

Tests never read your `templates/` or `data/`. Fixtures live in `tests/fixtures/`: two real V5 PNGs generated
with the made-up tag `artist:fuhrriel` in the NovelAI UI's prompt layout, and the template the solo one reproduces.

API notes: `docs/nai_api.md`. Official spec: <https://image.novelai.net/docs/index.html>.

## License

[AGPL-3.0-or-later](LICENSE).
