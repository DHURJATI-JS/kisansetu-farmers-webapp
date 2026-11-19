"""Hard safety boundary around every AI response.

The AI can NEVER run code or scripts — on the server or in the browser:
* No tools: the model only returns text. Nothing in the codebase passes model output to eval/exec/subprocess/
  os.system/importlib/pickle or a template engine (enforced by tests/test_security_invariants.py).
* Output is parsed with json.loads only, then validated against strict schemas.
* Actions: only whitelisted types (Action.ai_allowed) with pydantic-validated params; they are merely PREPARED —
  a human must still press GO.
* Text is stripped of markup/control characters and rendered with textContent (never innerHTML); the strict CSP
  blocks inline scripts as a second line of defence.
* Navigation suggestions map to a fixed set of internal routes (no URLs from the model are ever followed).
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

_TAG = re.compile(r"<[^>]{0,500}>")
_CTRL = {c for c in map(chr, range(32)) if c not in "\n\t"}
_JS_URL = re.compile(r"(javascript|vbscript|data)\s*:", re.I)

NAV_ROUTES = {
    "jobs": "/jobs", "shop": "/shop", "schemes": "/schemes", "profile": "/account", "survey": "/survey",
    "report": "/report", "orders": "/orders", "dashboard": "/dashboard", "farm_new": "/farms/new",
    "notifications": "/notifications", "policies": "/policies", "community": "/community", "assistant": "/assistant",
}
MAX_REPLY = 2500


def clean_text(text: Any, limit: int = MAX_REPLY) -> str:
    s = unicodedata.normalize("NFC", str(text or ""))
    s = _TAG.sub("", s)
    s = _JS_URL.sub("", s)
    s = "".join(ch for ch in s if ch not in _CTRL)
    return s.strip()[:limit]


def clean_suggestions(items: Any, limit: int = 4) -> list[str]:
    if not isinstance(items, list):
        return []
    out = []
    for it in items[:limit]:
        t = clean_text(it, 120)
        if t:
            out.append(t)
    return out


def clean_navigation(target: Any) -> str | None:
    return NAV_ROUTES.get(str(target or "").strip().lower())


def clean_actions(raw: Any, allowed: set[str], limit: int = 3) -> tuple[list[dict[str, Any]], list[str]]:
    """Keep only whitelisted action types with dict params; returns (actions, navigation routes)."""
    actions, nav = [], []
    if not isinstance(raw, list):
        return actions, nav
    for a in raw[:6]:
        if not isinstance(a, dict):
            continue
        typ = str(a.get("type") or "").strip()
        params = a.get("params") if isinstance(a.get("params"), dict) else {}
        if typ == "navigate":
            route = clean_navigation(params.get("to"))
            if route:
                nav.append(route)
            continue
        if typ in allowed and len(actions) < limit:
            safe_params = {str(k)[:40]: (clean_text(v, 500) if isinstance(v, str) else v) for k, v in params.items()
                           if isinstance(v, (str, int, float, bool, list)) or v is None}
            actions.append({"type": typ, "params": safe_params})
    return actions, nav
