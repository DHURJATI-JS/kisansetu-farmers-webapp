"""Uploads + global sanitisation, end-to-end against a RUNNING server with ClamAV (TEST_MODE=true).

    python tests/e2e_uploads.py

* ClamAV: the EICAR test file is refused (422), never stored, logged as an abuse event and reported to admins.
* Sanitising by reconstruction: a PNG with an HTML/JS payload appended is stored WITHOUT the payload; media responses
  are sandboxed (CSP sandbox, nosniff); PDFs download as attachments.
* PDFs with scripts — even hex-escaped or inside compressed streams — are refused; clean PDFs are accepted.
* Text: bidi overrides, NUL/control characters, lone surrogates and HTML are cleaned from every JSON field before any
  handler runs — while Indian scripts (with ZWJ) are kept exactly; passwords are never altered.
"""
from __future__ import annotations

import io
import sys
import time
import uuid
import zlib
from pathlib import Path

import httpx
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from e2e_flow import BASE, Client, finish, ok, reset_limits, signup, sql  # noqa: E402

# built at run time from pieces so no file on disk ever contains the antivirus test string
EICAR = "".join(["X5O!P%@AP[4", chr(92), "PZX54(P^)7CC)7}", chr(36), "EICAR-STANDARD-", "ANTIVIRUS-TEST-FILE!", chr(36), "H+H*"]).encode()


def png(color=(40, 160, 90)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buf, "PNG")
    return buf.getvalue()


def pdf(body: bytes) -> bytes:
    return b"%PDF-1.4\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n" + body + b"\ntrailer << /Root 1 0 R >>\n%%EOF\n"


def main() -> int:
    t0 = time.time()
    reset_limits()
    pa = "8" + str(uuid.uuid4().int % 10**9).zfill(9)
    c = Client("uploader")
    c.c.headers["X-Forwarded-For"] = "10.81.0.1"
    signup(c, "farmer", pa, "Kavya Uploads", "472910", 13.0, 77.6, dob="1994-06-06")

    print("1. ClamAV blocks malware before anything is decoded or stored (from its own account + network)")
    bad = Client("malware sender")                      # malware earns strikes → that network gets banned; keep it apart
    bad_ip = f"10.81.{uuid.uuid4().int % 200}.{uuid.uuid4().int % 250 + 1}"
    bad.c.headers["X-Forwarded-For"] = bad_ip
    signup(bad, "farmer", "8" + str(uuid.uuid4().int % 10**9).zfill(9), "Malware Sender", "472911", 13.0, 77.6, dob="1993-03-03")
    before = sql("SELECT count(*) FROM uploads")[0][0]
    r = bad.post("/api/uploads", data={"kind": "plant"}, files={"file": ("leaf.png", EICAR, "image/png")})
    ok(r.status_code == 422 and r.json().get("error") == "errors.upload_malware", "EICAR refused with 422 by the virus scanner", r.text[:160])
    ok(sql("SELECT count(*) FROM uploads")[0][0] == before, "nothing stored")
    ev = sql("SELECT details FROM abuse_events WHERE kind = 'malware_upload' ORDER BY created_at DESC LIMIT 1")
    ok(bool(ev) and ev[0][0].get("signature", "").lower().startswith("eicar"), "abuse event records the signature", ev and ev[0][0])
    ok(bool(sql("SELECT 1 FROM notifications WHERE title_key = 'notif.malware.title' AND dedupe_key LIKE 'malware:%'")),
       "superadmin alerted (one alert per file, never spammed)")
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:            # malware hidden in an archive renamed to .pdf: ClamAV unpacks it
        z.writestr("readme.txt", "policy documents")
        z.writestr("docs/claim.com", EICAR)
    r = bad.post("/api/uploads", data={"kind": "insurance"}, files={"file": ("policy.pdf", buf.getvalue(), "application/pdf")})
    ok(r.status_code in (403, 422), "malware inside an archive disguised as a PDF refused too", r.status_code)
    end = time.time() + 6
    while time.time() < end and bad.c.get("/").status_code != 403:
        time.sleep(0.5)
    ok(bad.c.get("/").status_code == 403, "two malware uploads (10 strike points): that network is banned", bad_ip)
    ok(bool(sql("SELECT 1 FROM ip_bans WHERE ip = :ip AND lifted_at IS NULL", ip=bad_ip)), "ban recorded with its evidence")
    sql("UPDATE ip_bans SET lifted_at = now() WHERE ip = :ip", ip=bad_ip)

    print("2. images are rebuilt: appended payloads are gone; media is sandboxed")
    poly = png() + b"<html><script>alert(document.cookie)</script></html>"
    r = c.post("/api/uploads", data={"kind": "plant"}, files={"file": ("../../evil‮gnp.png", poly, "image/png")})
    ok(r.status_code == 200, "polyglot PNG accepted (as an image)", r.text[:120])
    uid = r.json().get("upload_id")
    m = c.c.get(f"/media/{uid}")
    ok(m.status_code == 200 and b"<script" not in m.content and m.content.startswith(b"\x89PNG"), "stored copy has no script payload")
    ok("sandbox" in m.headers.get("content-security-policy", "") and m.headers.get("x-content-type-options") == "nosniff",
       "media served with CSP sandbox + nosniff", dict(m.headers))
    meta = sql("SELECT meta FROM uploads WHERE id = :i", i=uid)[0][0]
    ok(meta.get("original_name") == "evilgnp.png" and meta.get("antivirus") == "clean", "file name sanitised, scan recorded", meta)
    r = c.post("/api/uploads", data={"kind": "plant"}, files={"file": ("x.png", b"<?php system($_GET[1]); ?>", "image/png")})
    ok(r.status_code == 400, "fake image refused", r.status_code)

    print("3. PDFs: active content refused (plain, hex-escaped, compressed), clean ones download as attachments")
    for label, body in (("plain JavaScript", b"2 0 obj << /OpenAction << /S /JavaScript /JS (app.alert(1)) >> >> endobj"),
                        ("hex-escaped name", b"2 0 obj << /OpenAction << /S /J#61vaScript /JS (x) >> >> endobj"),
                        ("hidden in a compressed stream", b"3 0 obj << /Filter /FlateDecode >> stream\n"
                         + zlib.compress(b"<< /S /Launch /F (cmd.exe) >>") + b"\nendstream endobj")):
        r = c.post("/api/uploads", data={"kind": "insurance"}, files={"file": ("p.pdf", pdf(body), "application/pdf")})
        refused = r.status_code in (400, 422) and r.json().get("error") in ("errors.upload_pdf_active", "errors.upload_malware")
        ok(refused, f"PDF with {label} refused ({r.json().get('error')})", r.text[:120])
    r = c.post("/api/uploads", data={"kind": "insurance"}, files={"file": ("policy.pdf", pdf(b"2 0 obj << /Type /Pages /Count 0 >> endobj"),
                                                                           "application/pdf")})
    ok(r.status_code == 200, "clean PDF accepted", r.text[:120])
    m = httpx.get(f"{BASE}/media/{r.json().get('upload_id')}")
    ok(m.headers.get("content-disposition", "").startswith("attachment"), "PDF served as a download, never inline", m.headers.get("content-disposition"))

    print("4. text is cleaned before any handler runs (Indian scripts kept, passwords untouched)")
    nasty = "Kavya‮\u0000 <script>alert(1)</script><b>Devi</b>​"
    raw = ('{"type":"profile.update","payload":{"full_name":"' + nasty.replace("\u0000", "\\u0000") + '","skills":"Tractor\\ud800 driving",'
           '"address":"क्‍ष Nagar"}}').encode("utf-8", "surrogatepass")
    r = c.c.post("/api/actions/prepare", content=raw, headers={"X-CSRF-Token": c.csrf, "Content-Type": "application/json"})
    ok(r.status_code == 200, "lone surrogate / NUL / bidi / HTML payload accepted after cleaning (no 500)", r.text[:200])
    g = c.post(f"/api/actions/{r.json().get('action_id')}/go", json={}) if r.status_code == 200 else r
    row = sql("SELECT full_name, skills, address FROM users WHERE phone = :p", p="+91" + pa)[0]
    ok(g.status_code == 200 and row[0] == "Kavya Devi" and row[1] == "Tractor driving", "stored text is clean", row)
    ok(row[2] == "क्‍ष Nagar", "Devanagari with ZWJ kept byte-for-byte", repr(row[2]))
    r = c.c.get("/api/jobs/search", params={"q": "<script>x</script>ragi‮", "lat": "13", "lon": "77.6"})
    ok(r.status_code == 200 and "<script" not in r.text, "query strings cleaned too")
    c2 = Client("login-check")
    c2.c.headers["X-Forwarded-For"] = "10.81.0.2"
    c2.refresh("/login")
    r = c2.post("/api/auth/login", json={"phone": pa, "password": "472910"})
    ok(r.status_code == 200, "password unchanged by sanitisation (login still works)", r.text[:120])
    return finish(t0)


if __name__ == "__main__":
    sys.exit(main())
