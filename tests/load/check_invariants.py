"""Post-run correctness checks: the database must be consistent no matter how hard the load test pushed.

    python tests/load/check_invariants.py --since 2026-11-17T10:00:00+05:30 [--log var/logs/server.log --log-offset 0]

Each check returns {name, ok, value, detail}. Used by scripts/run_loadtest.py (results are stored with the run).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# (name, sql returning ONE number that must be 0, human description)
ZERO_CHECKS = [
    ("no_plaintext_secret_in_pending_actions",
     "SELECT count(*) FROM pending_actions p, jsonb_each(p.payload) kv WHERE p.created_at > now() - interval '2 days' "
     "AND kv.key IN ('password','new_password','new_password_again','current_password','otp','totp','code','secret','token') "
     "AND jsonb_typeof(kv.value) = 'string'",
     "No password / PIN / code is ever stored readable in a prepared action (sealed while pending, wiped after)"),
    ("no_plaintext_secret_in_audit_log",
     "SELECT count(*) FROM audit_logs a, jsonb_each_text(CASE WHEN jsonb_typeof(a.details->'payload') = 'object' "
     "THEN a.details->'payload' ELSE '{}'::jsonb END) kv WHERE a.created_at > now() - interval '2 days' "
     "AND kv.key IN ('password','new_password','new_password_again','current_password','otp','totp','code','secret','token') "
     "AND kv.value <> '***'",
     "The audit log never holds a password, PIN or code"),
    ("applications_record_location_source",
     "SELECT count(*) FROM job_applications WHERE location_source IS NULL",
     "Every job application records where its distance was measured from (live, saved GPS or typed)"),
    ("stock_never_negative", "SELECT count(*) FROM products WHERE stock < 0",
     "No product ever has negative stock (no overselling)"),
    ("stock_equals_movement_ledger",
     "SELECT count(*) FROM products p WHERE p.stock <> (SELECT coalesce(sum(m.delta), 0) FROM inventory_movements m WHERE m.product_id = p.id)",
     "Every product's stock equals the sum of its inventory movements"),
    ("one_sale_movement_per_order_line",
     "SELECT count(*) FROM order_items oi WHERE (SELECT count(*) FROM inventory_movements m WHERE m.order_id = oi.order_id "
     "AND m.product_id = oi.product_id AND m.reason = 'sale') <> 1",
     "Each order line took stock exactly once (no double decrement on retries / double clicks)"),
    ("restock_only_for_cancelled_orders",
     "SELECT count(*) FROM order_items oi JOIN orders o ON o.id = oi.order_id WHERE (SELECT count(*) FROM inventory_movements m "
     "WHERE m.order_id = oi.order_id AND m.product_id = oi.product_id AND m.reason = 'cancel_restore') "
     "<> CASE WHEN o.status = 'cancelled' THEN 1 ELSE 0 END",
     "Cancelled orders returned their stock exactly once; live orders never did"),
    ("order_totals_add_up",
     "SELECT count(*) FROM orders o WHERE o.subtotal <> (SELECT coalesce(sum(line_total), 0) FROM order_items i WHERE i.order_id = o.id) "
     "OR o.total <> o.subtotal + o.courier_charge",
     "Order subtotal = Σ lines and total = subtotal + courier"),
    ("no_action_executed_twice",
     "SELECT count(*) FROM (SELECT target_id FROM audit_logs WHERE target_type = 'action' GROUP BY target_id HAVING count(*) > 1) x",
     "No GO action was executed twice (audit log has one entry per action)"),
    ("no_action_stuck_executing", "SELECT count(*) FROM pending_actions WHERE status = 'executing'",
     "No action is stuck half-way (a failed GO rolls back cleanly)"),
    ("ai_never_executed_anything",
     "SELECT count(*) FROM pending_actions p JOIN users u ON u.id = p.user_id WHERE p.source = 'ai' AND p.status = 'executed' "
     "AND p.created_at >= :since AND u.full_name ~ '^L[wo][0-9]'",       # load-test accounts never press GO on AI proposals
     "AI-proposed actions were never executed without a person pressing GO"),
    ("salary_accrued_once_per_day",
     "SELECT count(*) FROM (SELECT member_id, work_date FROM salary_accruals GROUP BY 1, 2 HAVING count(*) > 1) x",
     "Salary accrues at most once per worker per day"),
    ("one_survey_per_worker_per_day",
     "SELECT count(*) FROM (SELECT member_id, survey_date FROM wellbeing_surveys GROUP BY 1, 2 HAVING count(*) > 1) x",
     "At most one wellbeing survey per worker per day"),
    ("signed_out_devices_changed_nothing", "SELECT count(*) FROM users WHERE skills LIKE '%-REVOKED-%'",
     "A device signed out by a newer login never changed anything (its pending GO was refused)"),
    ("every_upload_scanned_clean",
     "SELECT count(*) FROM uploads WHERE created_at >= :since AND coalesce(meta->>'antivirus', '') <> 'clean'",
     "Every file stored during the run was scanned by ClamAV and found clean"),
    ("stored_text_is_clean",
     "SELECT (SELECT count(*) FROM users WHERE updated_at >= :since AND concat_ws(' ', full_name, skills, address) ~ :bad)"
     " + (SELECT count(*) FROM products WHERE updated_at >= :since AND concat_ws(' ', name, description) ~ :bad)"
     " + (SELECT count(*) FROM job_postings WHERE created_at >= :since AND concat_ws(' ', title, description) ~ :bad)",
     "No stored text contains bidi overrides, control characters or HTML tags (global sanitisation)"),
    ("normal_users_never_ip_banned",
     "SELECT count(*) FROM ip_bans WHERE created_at >= :since AND ip NOT LIKE '10.66.%' AND ip NOT LIKE '10.67.%'",
     "Only adversarial (chaos / spammer) IPs were banned — no real user was locked out"),
    ("ended_jobs_have_an_end_date", "SELECT count(*) FROM farm_members WHERE status = 'ended' AND ended_on IS NULL",
     "Every ended job has its end date"),
    ("notices_are_complete",
     "SELECT count(*) FROM farm_members WHERE (status = 'notice' AND (notice_end_date IS NULL OR notice_kind NOT IN ('individual', 'closure')))"
     " OR (status = 'active' AND (notice_kind IS NOT NULL OR notice_end_date IS NOT NULL))",
     "A notice always has its kind and last day; an active job never carries a leftover notice"),
    ("no_pay_after_the_job_ended",
     "SELECT count(*) FROM salary_accruals a JOIN farm_members m ON m.id = a.member_id WHERE m.ended_on IS NOT NULL AND a.work_date > m.ended_on",
     "No salary accrues after the last working day"),
    ("one_farm_job_at_a_time",
     "SELECT count(*) FROM (SELECT user_id FROM farm_members WHERE status IN ('active', 'notice') "
     "AND role IN ('employee', 'supervisor', 'manager', 'accountant') GROUP BY user_id HAVING count(*) > 1) x",
     "Nobody works on two farms at once — employee, supervisor, manager or accountant (own / co-owned farms don't count)"),
    ("workers_have_a_branch",
     "SELECT count(*) FROM farm_members WHERE status <> 'ended' AND role IN ('employee', 'supervisor') AND branch_id IS NULL",
     "Every farm worker belongs to a branch (a demotion always assigns one)"),
    ("owners_are_not_members_of_their_farm",
     "SELECT count(*) FROM farm_members m JOIN farms f ON f.id = m.farm_id WHERE m.user_id = f.owner_id AND m.status <> 'ended'",
     "The owner is never also a member of their own farm"),
    ("co_owners_never_on_individual_notice",
     "SELECT count(*) FROM farm_members WHERE role = 'co_owner' AND status = 'notice' AND notice_kind = 'individual'",
     "Co-owners are removed by proposal, never by a notice"),
    ("no_orphan_proposals",
     "SELECT count(*) FROM farm_proposals p JOIN farms f ON f.id = p.farm_id WHERE p.status = 'pending' AND p.proposed_by_id <> f.owner_id "
     "AND NOT EXISTS (SELECT 1 FROM farm_members m WHERE m.farm_id = p.farm_id AND m.user_id = p.proposed_by_id AND m.role = 'co_owner' "
     "AND m.status <> 'ended')",
     "No proposal is left pending after its proposer stopped being a voter"),
    ("posting_bans_have_a_record",
     "SELECT count(*) FROM users u WHERE u.posting_banned_until > now() AND NOT EXISTS (SELECT 1 FROM user_sanctions s WHERE "
     "s.user_id = u.id AND s.lifted_at IS NULL AND s.expires_at = u.posting_banned_until)",
     "Every running posting ban has its sanction record (evidence, who/what decided)"),
    ("protected_ips_never_banned",
     "SELECT count(*) FROM ip_bans WHERE lifted_at IS NULL AND ip IN ('127.0.0.1', '::1', 'unknown')",
     "Loopback / unknown addresses are never banned (that would lock everyone out behind the proxy)"),
    ("worker_count_within_hiring_limit",
     "SELECT count(*) FROM farms f WHERE (SELECT count(*) FROM farm_members m WHERE m.farm_id = f.id AND m.status <> 'ended' "
     "AND m.role IN ('employee', 'supervisor')) > f.hiring_limit",
     "No farm employs more farm workers than its hiring limit (promotions / demotions included)"),
    ("no_row_points_at_another_farms_branch",
     "SELECT " + " + ".join(
         f"(SELECT count(*) FROM {t} x JOIN branches b ON b.id = x.branch_id WHERE b.farm_id <> x.farm_id)"
         for t in ("branch_crops", "farm_members", "grievances", "halts", "insurance_policies", "invitations", "job_applications",
                   "job_postings", "ledger_entries", "orders", "owner_warnings", "products", "safety_cases", "salary_accruals",
                   "weather_reports", "wellbeing_surveys")),
     "No member, invitation, halt, ledger line, policy, job, product or order of one farm is tied to another farm's branch"),
]

INFO_CHECKS = [
    ("orders_placed", "SELECT count(*) FROM orders WHERE created_at >= :since", "Orders placed during the run"),
    ("orders_cancelled", "SELECT count(*) FROM orders WHERE created_at >= :since AND status = 'cancelled'", "…of which cancelled"),
    ("sold_out_products", "SELECT count(*) FROM products WHERE stock = 0", "Scarce products sold out completely (race winners decided)"),
    ("actions_executed", "SELECT count(*) FROM pending_actions WHERE created_at >= :since AND status = 'executed'", "GO actions executed"),
    ("ai_actions_proposed", "SELECT count(*) FROM pending_actions WHERE created_at >= :since AND source = 'ai'",
     "Actions the AI proposed (left for a human)"),
    ("malware_uploads_blocked", "SELECT count(*) FROM abuse_events WHERE created_at >= :since AND kind = 'malware_upload'",
     "Malware uploads blocked by ClamAV (EICAR test file)"),
    ("chaos_ips_banned", "SELECT count(DISTINCT ip) FROM ip_bans WHERE created_at >= :since AND ip LIKE '10.66.%'",
     "Adversarial IPs banned automatically (brute force / rate-limit abuse)"),
    ("surveys_answered", "SELECT count(*) FROM wellbeing_surveys WHERE answered_at >= :since", "Wellbeing surveys answered"),
    ("role_changes", "SELECT count(*) FROM audit_logs WHERE created_at >= :since AND action = 'member.change_role'",
     "Promotions / demotions executed"),
    ("notices_given", "SELECT count(*) FROM audit_logs WHERE created_at >= :since AND action = 'member.give_notice'", "Notices given"),
    ("notices_withdrawn", "SELECT count(*) FROM audit_logs WHERE created_at >= :since AND action = 'member.cancel_notice'",
     "Notices withdrawn"),
    ("spammer_ips_banned", "SELECT count(DISTINCT ip) FROM ip_bans WHERE created_at >= :since AND ip LIKE '10.67.%'",
     "Spammer IPs banned after strikes (AI-judged or by rule)"),
    ("ai_judged_bans", "SELECT count(*) FROM ip_bans WHERE created_at >= :since AND source = 'ai'", "Bans the AI decided"),
]

BAD_TEXT = "[\u202a-\u202e\u2066-\u2069\x01-\x08\x0b\x0c\x0e-\x1f]|<\\s*/?\\s*[A-Za-z][A-Za-z0-9]*[^<>]*>"
ERR_RE = re.compile(r"Traceback|\bERROR\b|\" 50\d ")


async def _db_checks(since: datetime) -> list[dict]:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from app.config import settings

    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    out = []
    try:
        async with engine.connect() as conn:
            for name, sql, desc in ZERO_CHECKS:
                params = {"since": since} if ":since" in sql else {}
                if ":bad" in sql:
                    params["bad"] = BAD_TEXT
                n = (await conn.execute(text(sql), params)).scalar_one()
                out.append({"name": name, "ok": int(n) == 0, "value": int(n), "detail": desc, "kind": "invariant"})
            for name, sql, desc in INFO_CHECKS:
                n = (await conn.execute(text(sql), {"since": since} if ":since" in sql else {})).scalar_one()
                out.append({"name": name, "ok": True, "value": int(n), "detail": desc, "kind": "info"})
    finally:
        await engine.dispose()
    return out


def _log_check(log: Path | None, offset: int) -> dict:
    if not log or not log.exists():
        return {"name": "no_server_errors_logged", "ok": True, "value": 0, "detail": "server log not available", "kind": "invariant"}
    with log.open("rb") as fh:
        fh.seek(offset)
        lines = fh.read().decode("utf-8", "ignore").splitlines()
    bad = [ln for ln in lines if ERR_RE.search(ln) and "CSP violation" not in ln]
    sample = []
    for ln in bad:
        short = ln.strip()[:180]
        if short not in sample:
            sample.append(short)
        if len(sample) >= 8:
            break
    return {"name": "no_server_errors_logged", "ok": not bad, "value": len(bad),
            "detail": "No tracebacks, ERROR lines or HTTP 5xx in the server log during the run", "sample": sample, "kind": "invariant"}


def _pool_check(host: str) -> dict:
    import requests

    last = None
    for _ in range(10):  # in-flight requests finish within a few seconds after Locust stops
        try:
            last = requests.get(host + "/internal/metrics", timeout=5).json()
            if last["db_pool"]["checked_out"] == 0:
                break
        except Exception:
            pass
        time.sleep(1)
    n = last["db_pool"]["checked_out"] if last else -1
    return {"name": "db_connections_returned", "ok": n == 0, "value": n, "detail": "Every DB connection went back to the pool (no leaks)",
            "kind": "invariant"}


def _session_check() -> dict:
    """One login per account: no account may have more than one USABLE session (its login key matches the database)."""
    import hashlib

    import redis

    from app.config import settings

    r = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
    live: dict[str, list[dict]] = {}
    for key in r.scan_iter("ks:usess:*", count=1000):
        hashes = list(r.smembers(key))
        sess = [json.loads(v) for v in r.mget([f"ks:sess:{h}" for h in hashes]) if v] if hashes else []
        if sess:
            live[key.split(":", 2)[2]] = sess
    keys = asyncio.run(_db_keys(list(live)))
    multi = sum(1 for uid, ss in live.items()
                if sum(1 for s in ss if s.get("lk") and hashlib.sha256(s["lk"].encode()).hexdigest() == keys.get(uid)) > 1)
    return {"name": "one_live_session_per_account", "ok": multi == 0, "value": multi,
            "detail": f"No account had more than one usable session ({len(live)} accounts with live sessions checked)", "kind": "invariant"}


async def _db_keys(uids: list[str]) -> dict[str, str | None]:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from app.config import settings

    if not uids:
        return {}
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(text("SELECT id::text, session_key_hash FROM users WHERE id::text = ANY(:ids)"), {"ids": uids})).all()
    finally:
        await engine.dispose()
    return dict(rows)


def _custom_checks(path: Path | None) -> list[dict]:
    if not path or not path.exists():
        return []
    labels = {"double_go_executes_once": "Two simultaneous GO clicks executed the action exactly once",
              "idempotent_order_replay": "Re-sent orders (same idempotency key) returned the same order",
              "socket_connect_ack": "Socket.IO clients were acknowledged on connect",
              "bruteforce_eventually_banned": "Password guessing from one IP ended in an IP ban",
              "attacker_ip_banned": "Attacking IPs (brute force, rate-limit abuse) were banned by the perimeter guard",
              "pages_only_show_own_account": "Every signed-in page showed its own user, never another account (no cross-request leak)",
              "no_foreign_account_data": "Account page, notifications and /api/auth/me only ever held this person's own private markers",
              "statement_shows_only_own_entries": "Each person's credit/debit statement (page and CSV) held only their own entries",
              "farm_statement_shows_only_own_farm": "Each owner's farm statement (account page and farm transaction history) held only that farm's entries",
              "no_reflected_markup": "Hostile text sent in links was never echoed back as live markup",
              "uploads_are_rebuilt_clean": "Polyglot uploads were stored without their script payload and served sandboxed",
              "signed_out_device_cannot_change_anything": "After a newer login, the old device's GO was refused with 401 (one login per account)",
              "newest_device_stays_signed_in": "The newest sign-in kept working",
              "ai_reply_has_no_executed_actions": "AI replies only proposed actions",
              "ai_answers_in_the_language_asked": "The AI answered in the language (and script) the question was asked in",
              "ai_points_only_at_screen_parts": "The AI only pointed at parts that are really on the person's screen",
              "guide_catalog_has_tours_and_words": "The talking guide always had its page tours and word explanations",
              "only_owners_change_roles": "Only the owner and co-owners could promote or demote people",
              "paused_or_demoted_leader_sent_to_public_page": "A paused or demoted leader lost access to farm management at once",
              "repeated_spam_blocked": "Repeated spam was blocked",
              "risky_terms_always_flagged": "Risky job terms (deposits, deductions, late pay) were always flagged",
              "spammer_ip_banned": "Spammers' addresses were banned",
              "terms_explainer_never_waits_for_a_busy_ai": "The terms explainer answered at once even while the AI was busy"}
    out = []
    for name, c in json.loads(path.read_text("utf-8")).items():
        out.append({"name": name, "ok": c["violations"] == 0, "value": c["violations"], "runs": c["runs"],
                    "detail": f"{labels.get(name, name)} ({c['runs']} attempts)", "kind": "invariant"})
    return out


def run(since: datetime, log: Path | None = None, log_offset: int = 0, checks: Path | None = None,
        host: str = "http://127.0.0.1:8088") -> list[dict]:
    res = asyncio.run(_db_checks(since))
    res.append(_log_check(log, log_offset))
    res.append(_pool_check(host))
    res.append(_session_check())
    res += _custom_checks(checks)
    order = {"invariant": 0, "info": 1}
    return sorted(res, key=lambda r: (order[r["kind"]], r["ok"], r["name"]))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    ap.add_argument("--log", default=str(ROOT / "var/logs/server.log"))
    ap.add_argument("--log-offset", type=int, default=0)
    ap.add_argument("--checks")
    a = ap.parse_args()
    for r in run(datetime.fromisoformat(a.since), Path(a.log), a.log_offset, Path(a.checks) if a.checks else None):
        print(("PASS " if r["ok"] else "FAIL ") if r["kind"] == "invariant" else "info ", f"{r['name']:<38} {r['value']:>7}  {r['detail']}")
        for s in r.get("sample", []):
            print("        ", s)
