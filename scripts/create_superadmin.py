"""Create (or promote) a superadmin account. The password is prompted, never passed on the command line.

    python scripts/create_superadmin.py --phone 9876543210 --email admin@example.in --name "Platform Admin"

The admin must set up two-step verification (TOTP) on first login before the admin panel opens.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def run(phone: str, email: str | None, name: str, password: str) -> None:
    from sqlalchemy import select

    from app.database import SessionLocal, engine
    from app.models import User
    from app.security.passwords import hash_password
    from app.utils import now_utc

    async with SessionLocal() as db:
        user = (await db.execute(select(User).where(User.phone == phone))).scalar_one_or_none()
        if user:
            user.role, user.status = "superadmin", "active"
            user.password_hash = await hash_password(password)
            user.token_version += 1
            print(f"promoted existing account {phone[:5]}… to superadmin")
        else:
            db.add(User(full_name=name, phone=phone, email=email, password_hash=await hash_password(password), role="superadmin",
                        phone_verified=True, photo_verified=True, terms_version="platform-v1", terms_accepted_at=now_utc(),
                        privacy_accepted_at=now_utc()))
            print(f"created superadmin {phone[:5]}…")
        await db.commit()
    await engine.dispose()


def main() -> int:
    from app.security.passwords import password_problems
    from app.utils import normalize_phone

    ap = argparse.ArgumentParser()
    ap.add_argument("--phone", required=True)
    ap.add_argument("--email")
    ap.add_argument("--name", default="Super Admin")
    a = ap.parse_args()
    phone = normalize_phone(a.phone)
    if not phone:
        ap.error("invalid Indian mobile number")
    pw = getpass.getpass("New superadmin password (≥10 chars, letters+digits): ")
    if pw != getpass.getpass("Repeat: "):
        print("passwords differ")
        return 1
    problems = password_problems(pw, "superadmin")
    if problems:
        print("weak password:", ", ".join(problems))
        return 1
    asyncio.run(run(phone, a.email, a.name, pw))
    return 0


if __name__ == "__main__":
    sys.exit(main())
