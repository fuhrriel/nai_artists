"""CLI. `uv run nai --help`.

gen        generate one image (artist x template) into data/full/<slug>/<template>.png
sub        show battery / Anlas
meta       dump NAI metadata from a PNG
templates  list templates with hashes
body       print the request body that gen would send (no network)
import     register PNGs as cells: match prompt -> template, artists from artist: tokens
template-from-image  write templates/<id>.txt from a PNG's prompt and register the PNG as its cell
cells      list what the DB knows
refs       fetch an artist's top-scored Gelbooru posts as reference images
serve      run the web app (uvicorn) on localhost
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

from . import config, db
from .importer import import_png, register_image
from .meta import read_comment, read_text_chunks
from .nai import NAIClient, NAIError, anlas_short, battery_low, battery_summary, build_body
from .templates import TemplateError, create_from_image, get_template, load_settings, load_templates


def expand_paths(args: list[str]) -> list[Path]:
    """~ and wildcards, for shells that don't expand them (cmd, PowerShell). A pattern matching nothing stays
    as given, so the caller reports it missing."""
    out = []
    for a in args:
        a = str(Path(a).expanduser())
        hits = sorted(glob.glob(a)) if any(c in a for c in "*?[") and not Path(a).exists() else []
        out.extend(Path(h) for h in hits or [a])
    return out


def _norm_artist(s: str) -> str:
    s = s.strip()
    return s[len("artist:") :] if s.lower().startswith("artist:") else s


def cmd_gen(args) -> int:
    """One image per run: one artist (or a combo, or --base) x one template. Never a batch."""
    if len(args.template) != 1:
        print("gen makes one image per run: give exactly one --template", file=sys.stderr)
        return 2
    settings = load_settings()
    t = get_template(args.template[0], settings)
    artists = [_norm_artist(a) for a in args.artist]
    if not artists and not args.base:
        print("give --artist TAG (repeat it for a combo) or --base for the artist-less baseline", file=sys.stderr)
        return 2
    if artists and args.base:
        print("--base takes no --artist; run it separately", file=sys.stderr)
        return 2
    label = " + ".join(artists) if artists else "(base)"
    dest = config.FULL_DIR / config.combo_slug(artists) / f"{t.id}.png"
    if dest.exists() and not args.force:
        print(f"skip  {dest} (exists; --force to overwrite)")
        return 0

    b = settings.battery
    with NAIClient() as client:
        sub = battery_summary(client.subscription())
        print(f"battery {sub['battery_percent']}%  negative={sub['is_negative']}  anlas={sub['anlas']}  tier={sub['tier']}")
        if anlas_short(sub, b.anlas_per_image):
            print(f"battery empty and {sub['anlas']} Anlas left (< {b.anlas_per_image} per image). "
                  f"--force does not override this.", file=sys.stderr)
            return 3
        if battery_low(sub, b.min_percent) and not args.force:
            print(f"battery {sub['battery_percent']}% is at/below min_percent={b.min_percent} "
                  f"(or negative). Re-run with --force to accept.", file=sys.stderr)
            return 3
        if args.dry_run:
            print(f"would generate {dest}")
            return 0
        body = build_body(settings, t, artists)
        print(f"gen   {label} x {t.id} -> {dest} ...", end="", flush=True)
        try:
            png, seed = client.generate(body)
        except NAIError as e:
            print(f" ERROR {e}")
            if e.fatal:
                print("429/402 is never retried.", file=sys.stderr)
            return 4
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(png)
    print(f" ok seed={seed} ({len(png) // 1024} KiB)")
    return 0


def cmd_sub(args) -> int:
    with NAIClient() as client:
        sub = client.subscription()
    if args.raw:
        print(json.dumps(sub, indent=1))
    else:
        print(json.dumps(battery_summary(sub), indent=1))
    return 0


def cmd_meta(args) -> int:
    for p in expand_paths(args.file):
        print(f"== {p}")
        if not args.comment_only:
            for k, v in read_text_chunks(p).items():
                if k != "Comment":
                    print(f"{k}: {v!r}")
        c = read_comment(p)
        if c is None:
            print("no NovelAI metadata found")
            continue
        if args.short:
            keep = {k: c.get(k) for k in ("seed", "width", "height", "steps", "scale", "sampler", "noise_schedule", "model_name", "model_hash")}
            keep["v4_prompt"] = c.get("v4_prompt")
            print(json.dumps(keep, indent=1, ensure_ascii=False))
        else:
            print(json.dumps(c, indent=1, ensure_ascii=False))
    return 0


def cmd_templates(args) -> int:
    settings = load_settings()
    for t in load_templates(settings):
        flags = ("P" if t.primary else "-") + ("E" if t.enabled else "-")
        print(f"{flags} {t.id:<16} {t.hash[:12]}  chars={len(t.chars)}  {t.name}  ({t.path})")
    return 0


def cmd_body(args) -> int:
    settings = load_settings()
    t = get_template(args.template, settings)
    print(json.dumps(build_body(settings, t, [_norm_artist(a) for a in args.artist]), indent=1, ensure_ascii=False))
    return 0


def cmd_import(args) -> int:
    settings = load_settings()
    templates = load_templates(settings)
    force_t = get_template(args.template, settings) if args.template else None
    rc = 0
    conn = db.connect()
    for src in expand_paths(args.file):
        if not src.is_file():
            print(f"missing   {src}", file=sys.stderr)
            rc = 1
            continue
        r = import_png(conn, settings, templates, src, force_template=force_t, replace=args.replace, dry_run=args.dry_run)
        print(r.line())
        if r.status == "no_match":
            rc = 1
            for d in r.diffs:
                print(f"          {d}")
    return rc


def cmd_template_from_image(args) -> int:
    src = Path(args.file)
    comment = read_comment(src)
    if comment is None:
        print(f"error: {src} carries no NovelAI metadata", file=sys.stderr)
        return 1
    settings = load_settings()
    t, artists = create_from_image(comment, args.id, args.name, primary=args.primary, force=args.force, settings=settings)
    print(f"wrote {t.path}  name={t.name!r}  primary={t.primary}  chars={len(t.chars)}  hash={t.hash[:12]}")
    if args.no_register:
        return 0
    r = register_image(db.connect(), settings, t, artists, src, comment, exact=True, replace=args.force)
    print(r.line())
    return 0


def cmd_cells(args) -> int:
    conn = db.connect()
    for a in db.list_artists(conn):
        rows = db.list_images(conn, artist_id=a["id"])
        print(f"{a['slug']:<24} {a['tag'] or '(base)'}")
        for r in rows:
            flag = "" if r["params_match"] else f"  !! {r['mismatch_note']}"
            print(f"    {r['template_id']:<16} {r['source']:<9} seed={r['seed']}  {r['template_hash'][:12]}  {r['path']}{flag}")
    return 0


def cmd_refs(args) -> int:
    from .refs import BooruError, GelbooruClient, eligible, fetch_artist

    rs = load_settings().refs
    conn = db.connect()
    if args.artist:
        rows = [db.get_or_create_artist(conn, [_norm_artist(a)]) for a in args.artist]
    elif args.all or args.missing:
        fetched = {i for i, r in db.list_ref_fetches(conn).items() if r["fetched_at"]}
        rows = [r for r in db.list_artists(conn) if eligible(r) and (args.all or r["id"] not in fetched)]
    else:
        print("give --artist TAG (repeatable), --missing or --all", file=sys.stderr)
        return 2
    if args.query is not None:
        if len(rows) != 1:
            print("--query needs exactly one --artist", file=sys.stderr)
            return 2
        db.set_ref_override(conn, rows[0]["id"], args.query.strip())
    if not rows:
        print("nothing to do")
        return 0
    with GelbooruClient(per_second=rs.per_second) as client:
        for a in rows:
            print(f"refs  {a['tag']} ...", end="", flush=True)
            try:
                res = fetch_artist(conn, client, a, rs)
            except BooruError as e:
                print(f" ERROR {e}")
                if e.fatal:
                    print("stopping: 401/403/429 is never retried.", file=sys.stderr)
                    return 4
                continue
            print(f" {len(res.kept)} kept  [{res.query}]")
            for r in res.kept:
                print(f"      #{r['rank'] + 1} score={r['score']} {r['rating']}  {r['path']}")
            for e in res.errors:
                print(f"      skipped {e}")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    uvicorn.run("nai_artists.app:app", host=args.host, port=args.port, reload=args.reload, log_level="info")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="nai", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)

    g = sp.add_parser("gen", help="generate one image: one artist (or combo, or --base) x one template")
    g.add_argument("--artist", "-a", action="append", default=[], help="artist tag (repeat for a combo); 'artist:' prefix optional")
    g.add_argument("--base", action="store_true", help="artist-less baseline: prompt is the bare template body")
    g.add_argument("--template", "-t", action="append", default=[], help="template id (exactly one)")
    g.add_argument("--force", action="store_true", help="overwrite an existing file and ignore the battery threshold (never the Anlas floor)")
    g.add_argument("--dry-run", action="store_true", help="check the battery and show what would be generated")
    g.set_defaults(fn=cmd_gen)

    s = sp.add_parser("sub", help="show subscription / battery / Anlas")
    s.add_argument("--raw", action="store_true")
    s.set_defaults(fn=cmd_sub)

    m = sp.add_parser("meta", help="dump NAI metadata from PNGs")
    m.add_argument("file", nargs="+")
    m.add_argument("--short", action="store_true", help="only the fields we care about")
    m.add_argument("--comment-only", action="store_true", help="skip the other tEXt chunks")
    m.set_defaults(fn=cmd_meta)

    tl = sp.add_parser("templates", help="list templates")
    tl.set_defaults(fn=cmd_templates)

    b = sp.add_parser("body", help="print the request body for artist x template (no network)")
    b.add_argument("--artist", "-a", action="append", default=[], help="omit for the baseline body")
    b.add_argument("--template", "-t", required=True)
    b.set_defaults(fn=cmd_body)

    im = sp.add_parser("import", help="register PNGs as cells (matched by prompt) or drop them in data/inbox")
    im.add_argument("file", nargs="+")
    im.add_argument("--template", "-t", help="force-assign non-matching files to this template (params_match=false)")
    im.add_argument("--replace", action="store_true", help="overwrite a cell that already exists")
    im.add_argument("--dry-run", action="store_true", help="only show what each file would match")
    im.set_defaults(fn=cmd_import)

    tf = sp.add_parser("template-from-image", help="write templates/<id>.txt from a PNG and register it as a cell")
    tf.add_argument("file")
    tf.add_argument("--id", required=True, help="template id = filename stem, [a-z0-9_-]")
    tf.add_argument("--name", help="display name (default: derived from the first prompt block)")
    tf.add_argument("--primary", action="store_true")
    tf.add_argument("--force", action="store_true", help="overwrite an existing template file / cell")
    tf.add_argument("--no-register", action="store_true", help="only write the template, don't import the image")
    tf.set_defaults(fn=cmd_template_from_image)

    c = sp.add_parser("cells", help="list artists and their cells from the DB")
    c.set_defaults(fn=cmd_cells)

    rf = sp.add_parser("refs", help="fetch top-scored Gelbooru posts as reference images (needs GELBOORU_* in .env)")
    rf.add_argument("--artist", "-a", action="append", default=[], help="artist tag (repeatable, single artists only)")
    rf.add_argument("--missing", action="store_true", help="every single artist never fetched")
    rf.add_argument("--all", action="store_true", help="re-fetch every single artist")
    rf.add_argument("--query", help="booru tag to search instead of the derived one (one --artist); '' clears it")
    rf.set_defaults(fn=cmd_refs)

    sv = sp.add_parser("serve", help="run the web app")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--reload", action="store_true", help="dev: restart on code changes")
    sv.set_defaults(fn=cmd_serve)

    args = ap.parse_args(argv)
    if sys.platform == "win32":  # redirected output would otherwise be cp1252 and choke on prompts / tags
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
    try:
        return args.fn(args)
    except (TemplateError, RuntimeError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except NAIError as e:
        print(f"NovelAI error: {e}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
