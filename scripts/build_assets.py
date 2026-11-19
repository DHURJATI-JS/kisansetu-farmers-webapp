"""Build production assets: minified JS/CSS with content hashes + SRI, and Tailwind CSS (no Node needed).

    python scripts/build_assets.py

Outputs app/static/dist/* and app/static/dist/manifest.json (templates pick them up automatically;
set DEBUG=true to use the unminified sources while developing).
"""
from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from rcssmin import cssmin
from rjsmin import jsmin

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
DIST = STATIC / "dist"


def _emit(rel: str, data: bytes, ext: str, manifest: dict) -> None:
    digest = hashlib.sha256(data).hexdigest()[:10]
    stem = rel.rsplit(".", 1)[0].replace("/", ".")
    name = f"{stem}.{digest}.min.{ext}"
    (DIST / name).write_bytes(data)
    sri = "sha384-" + base64.b64encode(hashlib.sha384(data).digest()).decode()
    manifest[rel] = {"file": f"dist/{name}", "integrity": sri, "bytes": len(data)}


def build_tailwind(manifest: dict) -> None:
    src = STATIC / "src" / "tailwind.css"
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "tw.css"
        cmd = None
        exe = shutil.which("tailwindcss") or shutil.which("tailwindcss", path=str(Path(sys.executable).parent))
        if exe:
            cmd = [exe, "-i", str(src), "-o", str(out), "--minify"]
        try:
            if cmd:
                subprocess.run(cmd, check=True, cwd=str(STATIC / "src"), capture_output=True, timeout=300)
            else:
                import pytailwindcss  # downloads the official standalone CLI on first use

                pytailwindcss.run(["-i", str(src), "-o", str(out), "--minify"], auto_install=True)
        except Exception as exc:  # noqa: BLE001
            print(f"! Tailwind build skipped ({exc}). Pages will use the Tailwind browser build instead.")
            return
        _emit("tailwind.css", out.read_bytes(), "css", manifest)
        print(f"tailwind.css → {manifest['tailwind.css']['file']} ({manifest['tailwind.css']['bytes']:,} bytes)")


def main() -> int:
    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True)
    manifest: dict = {}
    for path in sorted((STATIC / "js").rglob("*.js")):
        rel = path.relative_to(STATIC).as_posix()
        if rel == "js/sw.js":
            continue  # served at /sw.js unhashed so updates are picked up
        src = path.read_text("utf-8")
        _emit(rel, jsmin(src).encode("utf-8"), "js", manifest)
        print(f"{rel}: {len(src):,} → {manifest[rel]['bytes']:,} bytes")
    for path in sorted((STATIC / "css").glob("*.css")):
        rel = path.relative_to(STATIC).as_posix()
        src = path.read_text("utf-8")
        _emit(rel, cssmin(src).encode("utf-8"), "css", manifest)
        print(f"{rel}: {len(src):,} → {manifest[rel]['bytes']:,} bytes")
    build_tailwind(manifest)
    (DIST / "manifest.json").write_text(json.dumps(manifest, indent=1), "utf-8")
    print(f"manifest: {len(manifest)} assets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
