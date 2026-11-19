"""Self-host pinned third-party front-end files and the OpenCV face model (no runtime CDN dependency).

    python scripts/fetch_vendor.py

Prints the SHA-384 of every file so you can pin/compare them.
"""
from __future__ import annotations

import base64
import hashlib
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "app" / "static" / "vendor"
MODELS = ROOT / "var" / "models"

FILES = {
    VENDOR / "lucide.min.js": "https://cdn.jsdelivr.net/npm/lucide@0.460.0/dist/umd/lucide.min.js",
    VENDOR / "socket.io.min.js": "https://cdn.jsdelivr.net/npm/socket.io-client@4.8.1/dist/socket.io.min.js",
    VENDOR / "chart.umd.js": "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.js",
    VENDOR / "leaflet" / "leaflet.js": "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js",
    VENDOR / "leaflet" / "leaflet.css": "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css",
    VENDOR / "leaflet" / "images" / "marker-icon.png": "https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon.png",
    VENDOR / "leaflet" / "images" / "marker-icon-2x.png": "https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon-2x.png",
    VENDOR / "leaflet" / "images" / "marker-shadow.png": "https://unpkg.com/leaflet@1.9.4/dist/images/marker-shadow.png",
    VENDOR / "leaflet" / "images" / "layers.png": "https://unpkg.com/leaflet@1.9.4/dist/images/layers.png",
    VENDOR / "leaflet" / "images" / "layers-2x.png": "https://unpkg.com/leaflet@1.9.4/dist/images/layers-2x.png",
    MODELS / "haarcascade_frontalface_default.xml":
        "https://raw.githubusercontent.com/opencv/opencv/4.x/data/haarcascades/haarcascade_frontalface_default.xml",
}


def main() -> int:
    failed = 0
    with httpx.Client(timeout=60, follow_redirects=True) as c:
        for dest, url in FILES.items():
            try:
                r = c.get(url)
                r.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                print(f"✘ {url}: {exc}")
                failed += 1
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(r.content)
            sri = "sha384-" + base64.b64encode(hashlib.sha384(r.content).digest()).decode()
            print(f"✔ {dest.relative_to(ROOT)} ({len(r.content):,} bytes) {sri}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
