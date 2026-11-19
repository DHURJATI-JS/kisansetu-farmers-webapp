"""Small shared helpers: time (IST), money, loose JSON parsing, misc."""
from __future__ import annotations

import calendar
import json
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
ZERO = Decimal("0.00")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_ist() -> datetime:
    return datetime.now(IST)


def today_ist() -> date:
    return now_ist().date()


def money(value: Any) -> Decimal:
    try:
        return Decimal(str(value if value is not None else 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return ZERO


def format_inr(value: Any, symbol: bool = True) -> str:
    """Indian digit grouping: 12,34,567.89"""
    amount = money(value)
    neg = amount < 0
    amount = abs(amount)
    whole, frac = f"{amount:.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    out = f"{whole}.{frac}"
    if frac == "00":
        out = whole
    return ("-" if neg else "") + ("₹" if symbol else "") + out


def days_in_month(d: date) -> int:
    return calendar.monthrange(d.year, d.month)[1]


def add_months(d: date, months: int, day: int | None = None) -> date:
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    dd = min(day or d.day, calendar.monthrange(y, m)[1])
    return date(y, m, dd)


def daterange(start: date, end: date):
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


def age_on(dob: date | None, on: date | None = None) -> int | None:
    if not dob:
        return None
    on = on or today_ist()
    return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))


_FENCE = re.compile(r"^```[a-zA-Z0-9]*\s*|\s*```$", re.M)


def parse_json_loose(text: str | None) -> Any:
    """Parse JSON from LLM output (handles ``` fences and leading/trailing prose)."""
    if not text:
        return None
    s = _FENCE.sub("", text.strip()).strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start = s.find(open_c)
        while start != -1:
            depth, in_str, esc = 0, False, False
            for i in range(start, len(s)):
                ch = s[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == open_c:
                    depth += 1
                elif ch == close_c:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(s[start : i + 1])
                        except Exception:
                            break
            start = s.find(open_c, start + 1)
    return None


def mask_phone(phone: str | None) -> str:
    if not phone:
        return ""
    return phone[:3] + "•" * max(0, len(phone) - 7) + phone[-4:]


def normalize_phone(raw: str) -> str | None:
    """Return E.164 Indian mobile (+91XXXXXXXXXX) or None."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    if digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    return None


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def short_id(value: Any) -> str:
    return str(value).replace("-", "")[:8].upper()


def clip(text: str | None, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def to_jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    if hasattr(value, "hex") and value.__class__.__name__ == "UUID":
        return str(value)
    return value
