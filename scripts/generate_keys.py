"""Create .env from .env.example (if missing) and fill empty secrets with strong random values.

    python scripts/generate_keys.py
"""
from __future__ import annotations

import base64
import os
import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV, EXAMPLE = ROOT / ".env", ROOT / ".env.example"

GENERATORS = {
    "SECRET_KEY": lambda: secrets.token_urlsafe(48),
    "PASSWORD_PEPPER": lambda: secrets.token_urlsafe(32),
    "FIELD_ENCRYPTION_KEYS": lambda: "k1:" + base64.urlsafe_b64encode(os.urandom(32)).decode(),
    "BLIND_INDEX_KEY": lambda: secrets.token_urlsafe(32),
}


def main() -> int:
    if not ENV.exists():
        ENV.write_text(EXAMPLE.read_text("utf-8"), "utf-8")
        print("created .env from .env.example")
    text = ENV.read_text("utf-8")
    for key, gen in GENERATORS.items():
        pattern = re.compile(rf"^{key}=\s*(#.*)?$", re.M)
        if pattern.search(text):
            text = pattern.sub(f"{key}={gen()}", text)
            print(f"generated {key}")
    ENV.write_text(text, "utf-8")
    try:
        os.chmod(ENV, 0o600)
    except OSError:
        pass
    print("done — keep .env secret and back up FIELD_ENCRYPTION_KEYS (encrypted IDs cannot be recovered without it).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
