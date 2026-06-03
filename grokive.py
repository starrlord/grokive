from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
# All generated state (grok_auth.txt, metadata.json, gallery/, ...) is written
# relative to DATA_DIR. Defaults to the code directory for plain CLI use; set
# GROK_DATA_DIR (e.g. /data in Docker) to keep state on a persistent volume.
DATA_DIR = Path(os.environ.get("GROK_DATA_DIR", ROOT)).resolve()


def script(name: str) -> str:
    return str(ROOT / name)


def default_curl() -> str:
    """The auth file to read, relative to DATA_DIR. Prefer the current name but
    fall back to the legacy ``curl_samples.txt`` so existing data dirs keep working."""
    if (DATA_DIR / "grok_auth.txt").exists():
        return "grok_auth.txt"
    if (DATA_DIR / "curl_samples.txt").exists():
        return "curl_samples.txt"
    return "grok_auth.txt"


def run(args: list[str]) -> int:
    print(" ".join(args))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return subprocess.call(args, cwd=DATA_DIR)


def has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def check() -> int:
    ok = True
    print("Grokive environment check")
    print(f"Python: {sys.version.split()[0]}")
    if sys.version_info < (3, 10):
        print("ERROR: Python 3.10+ is required.")
        ok = False
    for module in ("httpx", "curl_cffi", "PIL"):
        installed = has_module(module)
        print(f"{module}: {'ok' if installed else 'missing'}")
        ok = ok and installed
    ffmpeg = shutil.which("ffmpeg")
    print(f"ffmpeg: {ffmpeg or 'missing - video thumbnails will be placeholders'}")
    print(f"data dir: {DATA_DIR}")
    have_auth = (DATA_DIR / "grok_auth.txt").exists() or (DATA_DIR / "curl_samples.txt").exists()
    print(f"grok_auth.txt: {'found' if have_auth else 'missing'}")
    print(f"metadata.json: {'found' if (DATA_DIR / 'metadata.json').exists() else 'not created yet'}")
    if not ok:
        print("Run: python -m pip install -r requirements.txt")
        return 1
    return 0


def build_index() -> int:
    """Generate any missing thumbnails, then (re)build the SQLite read-model
    (index.db) that the web UI queries."""
    import db
    import thumbnails

    gallery_dir = DATA_DIR / "gallery"
    metadata = DATA_DIR / "metadata.json"
    db_file = DATA_DIR / "index.db"
    if not metadata.exists():
        print(f"no metadata.json in {DATA_DIR}; run 'download' first")
        return 1
    made = thumbnails.generate_missing(metadata, gallery_dir)
    print(f"thumbnails: {made} generated")
    rows = db.build_index(db_file, metadata, gallery_dir)
    print(f"index.db: {rows} media rows")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Grokive command helper.")
    sub = parser.add_subparsers(dest="command", required=True)

    dl = sub.add_parser("download", help="Download Grok Imagine favorites.")
    dl.add_argument("--curl", default=default_curl())
    dl.add_argument("--max-pages", default="5000")
    dl.add_argument("--verbose", action="store_true")

    agents = sub.add_parser("agents", help="Download media from Grok Agent canvases (/imagine/agent/<id>).")
    agents.add_argument("--curl", default=default_curl())
    agents.add_argument("canvas_ids", nargs="*", help="Specific canvas IDs/URLs; omit to archive all canvases.")
    agents.add_argument("--verbose", action="store_true")

    post = sub.add_parser("post", help="Download specific Grok Imagine posts by id or /imagine/post/<id> URL.")
    post.add_argument("--curl", default=default_curl())
    post.add_argument("post_ids", nargs="+", help="Post IDs or /imagine/post/<id> URLs (root media + child posts).")
    post.add_argument("--verbose", action="store_true")

    sub.add_parser("index", help="Generate missing thumbnails and (re)build the SQLite index (index.db) the web UI queries.")

    all_cmd = sub.add_parser("all", help="Download, then build the index.")
    all_cmd.add_argument("--curl", default=default_curl())
    all_cmd.add_argument("--max-pages", default="5000")

    sub.add_parser("reindex", help="Rebuild metadata.json from data.js / existing media (no downloads).")

    sub.add_parser("check", help="Check Python dependencies and local files.")
    args = parser.parse_args()

    if args.command == "check":
        return check()
    if args.command == "download":
        cmd = [
            sys.executable, script("gdownloader.py"),
            "--curl", args.curl,
            "--grok-favorites",
            "--max-pages", str(args.max_pages),
        ]
        if not args.verbose:
            cmd.append("--quiet")
        return run(cmd)
    if args.command == "agents":
        cmd = [
            sys.executable, script("gdownloader.py"),
            "--curl", args.curl,
            "--grok-agents", *args.canvas_ids,
        ]
        if not args.verbose:
            cmd.append("--quiet")
        return run(cmd)
    if args.command == "post":
        cmd = [
            sys.executable, script("gdownloader.py"),
            "--curl", args.curl,
            "--grok-posts", *args.post_ids,
        ]
        if not args.verbose:
            cmd.append("--quiet")
        return run(cmd)
    if args.command == "index":
        return build_index()
    if args.command == "reindex":
        return run([sys.executable, script("reindex.py")])
    if args.command == "all":
        code = run([
            sys.executable, script("gdownloader.py"),
            "--curl", args.curl,
            "--grok-favorites",
            "--max-pages", str(args.max_pages),
            "--quiet",
        ])
        if code:
            return code
        return build_index()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

