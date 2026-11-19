"""Promotion / demotion / notice / co-owner removal, end-to-end against a RUNNING server (TEST_MODE=true).

    python tests/e2e_roles.py

* Hierarchy: nobody acts on themselves, a peer, a superior, the owner or a co-owner; accountants and supervisors cannot
  give notice at all; only the notice giver (or the owner / a co-owner) can withdraw it; closure notices cannot be withdrawn.
* A leader under an individual notice keeps working but loses every power at once (pages, alerts) until it is withdrawn.
* Role changes need the owner + all co-owners; a stale proposal (role changed meanwhile) fails instead of guessing;
  an action prepared before a demotion cannot be executed after it.
* Removing a co-owner: the person removed has no vote; proposals that waited only for them run, theirs fail.
* Two leaders acting on one person at the same moment: exactly one wins, the state stays consistent.
* Workers see the owner's and managers' phone numbers (call / WhatsApp); outsiders and the public page never do.
"""
from __future__ import annotations

import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from e2e_flow import Client, finish, ok, reset_limits, signup, sql  # noqa: E402
from e2e_lifecycle import create_farm  # noqa: E402

RUN = uuid.uuid4().hex[:4]


def phone(prefix: str) -> str:
    return prefix + str(uuid.uuid4().int % 10**9).zfill(9)


def person(name: str, role: str, ip: str, prefix: str, pw: str, dob: str = "1990-05-05") -> tuple[Client, str]:
    c = Client(name)
    c.c.headers["X-Forwarded-For"] = ip
    ph = phone(prefix)
    signup(c, role, ph, f"{name} {RUN}", pw, 12.98, 77.60, email=f"{name.lower()}{ph[-5:]}@kisansetu.in" if role == "owner" else None, dob=dob)
    sql("UPDATE users SET photo_verified = true WHERE phone = :p", p="+91" + ph)
    return c, ph


def member(farm: str, ph: str) -> str:
    return str(sql("SELECT m.id FROM farm_members m JOIN users u ON u.id = m.user_id WHERE m.farm_id = :f AND u.phone = :p "
                   "ORDER BY m.created_at DESC LIMIT 1", f=farm, p="+91" + ph)[0][0])


def uid(ph: str) -> str:
    return str(sql("SELECT id FROM users WHERE phone = :p", p="+91" + ph)[0][0])


def mstate(mid: str):
    return sql("SELECT role, status, notice_kind, ended_on FROM farm_members WHERE id = :m", m=mid)[0]


def invite_and_join(owner: Client, farm: str, branch: str, who: Client, ph: str, role: str) -> str:
    worker = role in ("employee", "supervisor")
    payload = {"farm_id": farm, "phone": ph, "role": role, "salary_type": "daily" if worker else "monthly",
               "salary_amount": 650 if worker else 26000}
    if worker:
        payload["branch_id"] = branch
    owner.action("member.invite", payload)
    inv = str(sql("SELECT id FROM invitations WHERE farm_id = :f AND phone = :p AND status = 'pending'", f=farm, p="+91" + ph)[0][0])
    who.refresh("/dashboard")
    body = {"invitation_id": inv, "accept": True, "accept_terms": True}
    if worker:
        body.update(lat=12.98, lon=77.60)
    who.action("invitation.respond", body)
    return member(farm, ph)


def refused(c: Client, type_: str, payload: dict, code: int, err: str, label: str) -> None:
    prep, r = c.action(type_, payload, expect_ok=False)
    if prep is None:   # refused at prepare
        ok(r.status_code == code and r.json().get("error") == err, label, f"{r.status_code} {r.text[:160]}")
    else:              # prepare passed but GO refused (re-validated at GO)
        ok(r.status_code == code and r.json().get("error") == err, label + " (at GO)", f"{r.status_code} {r.text[:160]}")


def main() -> int:
    t0 = time.time()
    reset_limits()
    a, pa = person("Arjun", "owner", "10.82.0.1", "9", "OwnerRoles2026a", dob="1979-01-01")
    b, pb = person("Bhavna", "owner", "10.82.0.2", "9", "OwnerRoles2026b", dob="1981-01-01")
    c3, pc = person("Chetan", "owner", "10.82.0.3", "9", "OwnerRoles2026c", dob="1983-01-01")
    m1, p1 = person("Meera", "farmer", "10.82.0.4", "8", "571935")
    m2, p2 = person("Mahesh", "farmer", "10.82.0.5", "8", "571936")
    ac, pac = person("Anita", "farmer", "10.82.0.6", "8", "571937")
    sv, psv = person("Suresh", "farmer", "10.82.0.7", "8", "571938")
    e1, pe1 = person("Eshwar", "farmer", "10.82.0.8", "8", "571939")
    e2, pe2 = person("Ekta", "farmer", "10.82.0.9", "8", "571940")
    x, px = person("Xavier", "farmer", "10.82.0.10", "8", "571941")
    farm, branch = create_farm(a)

    print("1. team: co-owner Bhavna, managers Meera + Mahesh, accountant Anita, supervisor Suresh, workers Eshwar + Ekta")
    a.action("coowner.add", {"farm_id": farm, "phone": pb})
    b.refresh("/dashboard")
    b.action("invitation.respond", {"invitation_id": str(sql("SELECT id FROM invitations WHERE farm_id = :f AND role = 'co_owner' "
                                                             "AND status = 'pending'", f=farm)[0][0]), "accept": True, "accept_terms": True})
    mb = member(farm, pb)
    mm1 = invite_and_join(a, farm, branch, m1, p1, "manager")
    mm2 = invite_and_join(a, farm, branch, m2, p2, "manager")
    mac = invite_and_join(a, farm, branch, ac, pac, "accountant")
    msv = invite_and_join(a, farm, branch, sv, psv, "supervisor")
    me1 = invite_and_join(a, farm, branch, e1, pe1, "employee")
    me2 = invite_and_join(a, farm, branch, e2, pe2, "employee")

    print("1b. statements: the owner and the co-owner see the farm's statement in their account; managers use the farm tab")
    for who, cl in (("owner", a), ("co-owner", b)):
        r = cl.c.get(f"/account/statement?of={farm}")
        ok(r.status_code == 200 and "Farm statement" in r.text, f"the {who} opens the farm statement from the account", r.status_code)
    r = m1.c.get(f"/account/statement?of={farm}")
    ok(r.status_code == 404, "a manager does not get the farm statement in the account", r.status_code)
    r = ac.c.get(f"/farms/{farm}?tab=ledger")
    ok(r.status_code == 200 and "Closing balance" in r.text, "the accountant reads it in the farm's transaction history", r.status_code)

    print("2. hierarchy: nobody acts on themselves, a peer, a superior or a co-owner")
    for c in (m1, m2, ac, sv):
        c.refresh(f"/farms/{farm}")
    refused(m1, "member.give_notice", {"member_id": mm2, "reason": "peer"}, 403, "errors.rank_too_low", "manager cannot give notice to a peer manager")
    refused(m1, "member.give_notice", {"member_id": mm1, "reason": "self"}, 403, "errors.not_on_yourself", "manager cannot give notice to themselves")
    refused(m1, "member.give_notice", {"member_id": mb, "reason": "co-owner"}, 403, "errors.coowner_via_proposal", "manager cannot give notice to a co-owner")
    refused(ac, "member.give_notice", {"member_id": me1, "reason": "x"}, 403, "errors.no_farm_permission", "accountant cannot give notice")
    refused(sv, "member.give_notice", {"member_id": me1, "reason": "x"}, 403, "errors.no_farm_permission", "supervisor cannot give notice")
    refused(m1, "member.change_role", {"member_id": me1, "role": "supervisor", "expected_role": "employee"}, 403,
            "errors.no_farm_permission", "a manager cannot promote anyone (owner + co-owners decide)")
    refused(x, "member.give_notice", {"member_id": me1, "reason": "outsider"}, 403, "errors.no_farm_permission", "an outsider cannot touch the team")

    print("3. a leader on an individual notice loses every power at once")
    m1.action("member.give_notice", {"member_id": mac, "reason": "Accounts repeatedly late"})
    st = mstate(mac)
    ok(st[1] == "notice" and st[2] == "individual", "accountant is on an individual notice", st)
    r = ac.c.get(f"/farms/{farm}?tab=payroll")
    ok(r.status_code == 303 and r.headers.get("location", "").startswith(f"/f/{farm}"), "accountant on notice can no longer open the payroll", r.status_code)
    note = sql("SELECT body_key, params FROM notifications WHERE user_id = :u AND kind = 'notice' ORDER BY created_at DESC LIMIT 1", u=uid(pac))
    ok(bool(note) and note[0][1].get("reason") == "Accounts repeatedly late", "the person is told WHY (reason in the notice)", note)
    ok(bool(sql("SELECT 1 FROM notifications WHERE user_id = :u AND kind = 'notice_given'", u=uid(pa))),
       "the owner is told that a manager gave a notice (oversight)")
    e2.refresh("/dashboard")
    e2.action("member.resign", {"member_id": me2})
    got = {r[0] for r in sql("SELECT user_id::text FROM notifications WHERE kind = 'resigned' AND params->>'name' = :n", n=f"Ekta {RUN}")}
    ok(uid(p1) in got and uid(pac) not in got, "leader alerts reach the active manager but not the accountant on notice", got)
    refused(m2, "member.cancel_notice", {"member_id": mac}, 403, "errors.notice_not_yours", "another manager cannot withdraw a colleague's notice")
    b.refresh(f"/farms/{farm}")
    b.action("member.cancel_notice", {"member_id": mac})
    ok(mstate(mac)[1] == "active" and mstate(mac)[2] is None, "co-owner withdrew the notice; accountant active again")
    r = ac.c.get(f"/farms/{farm}?tab=payroll")
    ok(r.status_code == 200, "accountant's payroll access is back", r.status_code)

    print("4. promotion needs the owner AND the co-owner; stale proposals fail instead of guessing")
    a.refresh(f"/farms/{farm}?tab=team")
    _, g = a.action("member.change_role", {"member_id": me1, "role": "supervisor", "expected_role": "employee", "reason": "Reliable"})
    ok(g.json().get("status") == "proposed", "promotion became a proposal", g.text[:160])
    ok(mstate(me1)[0] == "employee", "nothing changed before approval")
    prop = str(sql("SELECT id FROM farm_proposals WHERE farm_id = :f AND status = 'pending' AND action_type = 'member.change_role'", f=farm)[0][0])
    r = b.post(f"/api/proposals/{prop}/vote", json={"vote": "approve"})
    ok(r.json().get("status") == "executed" and mstate(me1)[0] == "supervisor", "approved → Eshwar is a supervisor", r.text[:160])
    ok(bool(sql("SELECT 1 FROM notifications WHERE user_id = :u AND kind = 'role_changed'", u=uid(pe1))), "Eshwar is told about the new role")
    _, g1 = a.action("member.change_role", {"member_id": msv, "role": "manager", "expected_role": "supervisor"})
    _, g2 = a.action("member.change_role", {"member_id": msv, "role": "employee", "expected_role": "supervisor"})
    p_up, p_down = g1.json().get("proposal_id"), g2.json().get("proposal_id")
    r1 = b.post(f"/api/proposals/{p_up}/vote", json={"vote": "approve"})
    r2 = b.post(f"/api/proposals/{p_down}/vote", json={"vote": "approve"})
    ok(r1.json().get("status") == "executed" and r2.json().get("status") == "failed", "first proposal ran, the stale second one failed",
       (r1.text[:100], r2.text[:160]))
    ok(mstate(msv)[0] == "manager", "Suresh is a manager (not silently demoted by the stale proposal)")
    refused(a, "member.change_role", {"member_id": msv, "role": "employee", "expected_role": "supervisor"}, 409,
            "errors.role_changed_meanwhile", "a change based on an old view of the role is refused")
    refused(a, "member.change_role", {"member_id": mb, "role": "manager", "expected_role": "manager"}, 403,
            "errors.coowner_via_proposal", "a co-owner's role cannot be changed this way")

    print("5. an action prepared BEFORE a demotion cannot run after it")
    m2.refresh(f"/farms/{farm}?tab=team")
    r = m2.post("/api/actions/prepare", json={"type": "member.give_notice", "payload": {"member_id": me1, "reason": "prepared early"}})
    ok(r.status_code == 200, "manager Mahesh prepared a notice", r.text[:120])
    early = r.json().get("action_id")
    _, g = a.action("member.change_role", {"member_id": mm2, "role": "employee", "expected_role": "manager", "branch_id": branch})
    b.post(f"/api/proposals/{g.json().get('proposal_id')}/vote", json={"vote": "approve"})
    ok(mstate(mm2)[0] == "employee", "Mahesh demoted to farm worker (with a branch)")
    r = m2.post(f"/api/actions/{early}/go", json={})
    ok(r.status_code == 403, "his earlier prepared notice is refused at GO", f"{r.status_code} {r.text[:120]}")
    ok(mstate(me1)[1] == "active", "Eshwar unaffected")

    print("6. two leaders act on one person at the same moment: exactly one wins")
    m1.refresh(f"/farms/{farm}")
    sv.refresh(f"/farms/{farm}")
    preps = [c.post("/api/actions/prepare", json={"type": "member.give_notice", "payload": {"member_id": mm2, "reason": f"race {i}"}})
             for i, c in enumerate((m1, sv))]
    ok(all(p.status_code == 200 for p in preps), "both managers prepared a notice for Mahesh", [p.status_code for p in preps])
    res: list = [None, None]

    def go(i: int, c: Client) -> None:
        res[i] = c.post(f"/api/actions/{preps[i].json()['action_id']}/go", json={})
    ts = [threading.Thread(target=go, args=(i, c)) for i, c in enumerate((m1, sv))]
    for th in ts:
        th.start()
    for th in ts:
        th.join()
    codes = sorted(r.status_code for r in res)
    ok(codes == [200, 409], "one GO succeeded, the other saw the new state (409)", codes)
    ok(sql("SELECT count(*) FROM notifications WHERE user_id = :u AND kind = 'notice'", u=uid(p2))[0][0] == 1, "Mahesh got exactly one notice")

    print("7. closure notices cannot be withdrawn; resign vs notice race keeps a consistent row")
    sql("UPDATE farm_members SET notice_kind = 'closure' WHERE id = :m", m=mm2)
    refused(a, "member.cancel_notice", {"member_id": mm2}, 409, "errors.notice_is_closure", "a closure notice cannot be withdrawn")
    sql("UPDATE farm_members SET notice_kind = 'individual' WHERE id = :m", m=mm2)
    e1.refresh("/dashboard")
    p_res = e1.post("/api/actions/prepare", json={"type": "member.resign", "payload": {"member_id": me1}})
    p_not = m1.post("/api/actions/prepare", json={"type": "member.give_notice", "payload": {"member_id": me1, "reason": "race"}})
    out: list = [None, None]
    ts = [threading.Thread(target=lambda: out.__setitem__(0, e1.post(f"/api/actions/{p_res.json()['action_id']}/go", json={}))),
          threading.Thread(target=lambda: out.__setitem__(1, m1.post(f"/api/actions/{p_not.json()['action_id']}/go", json={})))]
    for th in ts:
        th.start()
    for th in ts:
        th.join()
    st = mstate(me1)
    ok(st[1] == "ended" and st[3] is not None, "resignation recorded with an end date whatever the order", (st, [o.status_code for o in out]))

    print("8. co-owner removal: the person removed has no vote; waiting proposals settle")
    a.action("coowner.add", {"farm_id": farm, "phone": pc})
    add = str(sql("SELECT id FROM farm_proposals WHERE farm_id = :f AND action_type = 'coowner.add' AND status = 'pending'", f=farm)[0][0])
    b.post(f"/api/proposals/{add}/vote", json={"vote": "approve"})
    c3.refresh("/dashboard")
    c3.action("invitation.respond", {"invitation_id": str(sql("SELECT id FROM invitations WHERE farm_id = :f AND phone = :p AND status = 'pending'",
                                                               f=farm, p="+91" + pc)[0][0]), "accept": True, "accept_terms": True})
    mc = member(farm, pc)
    _, g = a.action("farm.rename", {"farm_id": farm, "name": f"Roles Farm {RUN}"})
    waiting = g.json().get("proposal_id")
    b.post(f"/api/proposals/{waiting}/vote", json={"vote": "approve"})     # now only Chetan's vote is missing
    c3.refresh(f"/farms/{farm}")
    _, g = c3.action("farm.rename", {"farm_id": farm, "name": "Chetan's idea"})
    theirs = g.json().get("proposal_id")
    _, g = a.action("coowner.remove", {"member_id": mc, "reason": "Never attends"})
    rm = g.json().get("proposal_id")
    ok(g.json().get("status") == "proposed", "removal is a proposal (the other co-owner must approve)", g.text[:160])
    r = c3.post(f"/api/proposals/{rm}/vote", json={"vote": "reject"})
    ok(r.status_code == 403, "Chetan cannot vote on his own removal", r.status_code)
    failed_before = sql("SELECT count(*) FROM notifications WHERE title_key = 'notif.proposal_failed.title' AND user_id = :u", u=uid(pb))[0][0]
    r = b.post(f"/api/proposals/{rm}/vote", json={"vote": "approve"})
    ok(r.json().get("status") == "executed" and mstate(mc)[1] == "ended", "Chetan removed after Bhavna approved", r.text[:160])
    row = sql("SELECT status, error FROM farm_proposals WHERE id = :p", p=rm)[0]
    ok(row[0] == "executed" and row[1] is None, "the removal is not run a second time by its own settlement (no stale error)", row)
    failed_after = sql("SELECT count(*) FROM notifications WHERE title_key = 'notif.proposal_failed.title' AND user_id = :u", u=uid(pb))[0][0]
    ok(failed_after == failed_before, "voters got no false 'proposal failed' message", (failed_before, failed_after))
    ok(sql("SELECT status FROM farm_proposals WHERE id = :p", p=waiting)[0][0] == "executed"
       and sql("SELECT name FROM farms WHERE id = :f", f=farm)[0][0] == f"Roles Farm {RUN}", "the proposal waiting only for Chetan ran")
    ok(sql("SELECT status FROM farm_proposals WHERE id = :p", p=theirs)[0][0] == "failed", "Chetan's own pending proposal failed")
    r = c3.c.get(f"/farms/{farm}?tab=proposals")
    ok(r.status_code in (403, 404), "removed co-owner has no access to the farm any more", r.status_code)

    print("9. workers can call the owner and managers; nobody else sees those numbers")
    w = e1.c.get("/dashboard").text
    ok(f"tel:+91{pa}" in w and f"wa.me/91{pa}" in w, "ex-worker (owed money / left recently) sees the owner's number + WhatsApp")
    ok(f"tel:+91{p1}" in w, "and the manager's number")
    ok(f"+91{pc}" not in w, "the removed co-owner is not listed")
    ok(f"+91{pa}" not in x.c.get("/dashboard").text, "an outsider's dashboard never shows the owner's number")
    ok(f"+91{pa}" not in x.c.get(f"/f/{farm}").text, "the public farm page never shows the owner's number")
    sql("UPDATE farm_members SET ended_on = (now() AT TIME ZONE 'Asia/Kolkata')::date - 90 WHERE id = :m", m=me1)
    sql("DELETE FROM salary_accruals WHERE member_id = :m", m=me1)
    ok(f"+91{pa}" not in e1.c.get("/dashboard").text, "long-gone worker with nothing owed: contacts no longer shown")

    print("10. one working farm per person — employee, supervisor, manager or accountant; own / co-owned farms don't count")
    farm2, branch2 = create_farm(c3)                     # Chetan's own farm
    d, pd = person("Dev", "owner", "10.82.0.11", "9", "OwnerRoles2026d", dob="1985-01-01")
    farm3, branch3 = create_farm(d)                      # Dev's own farm
    w, pw = person("Wasim", "farmer", "10.82.0.12", "8", "571942")
    invite_and_join(a, farm, branch, w, pw, "employee")  # Wasim works on Arjun's farm

    def try_join(owner: Client, f: str, br: str, who: Client, ph: str, role: str):
        worker = role in ("employee", "supervisor")
        payload = {"farm_id": f, "phone": ph, "role": role, "salary_type": "daily" if worker else "monthly",
                   "salary_amount": 650 if worker else 26000, **({"branch_id": br} if worker else {})}
        owner.refresh(f"/farms/{f}")
        owner.action("member.invite", payload)
        inv = str(sql("SELECT id FROM invitations WHERE farm_id = :f AND phone = :p AND role = :r AND status = 'pending' "
                      "ORDER BY created_at DESC LIMIT 1", f=f, p="+91" + ph, r=role)[0][0])
        who.refresh("/dashboard")
        body = {"invitation_id": inv, "accept": True, "accept_terms": True, **({"lat": 12.98, "lon": 77.60} if worker else {})}
        prep, r = who.action("invitation.respond", body, expect_ok=False)
        sql("UPDATE invitations SET status = 'declined' WHERE id = :i AND status = 'pending'", i=inv)   # tidy for the next try
        return r

    for role in ("employee", "manager", "accountant"):
        r = try_join(c3, farm2, branch2, w, pw, role)
        ok(r.status_code == 409 and r.json().get("error") == "errors.one_job_at_a_time",
           f"a worker with a job cannot take a second job as {role}", f"{r.status_code} {r.text[:140]}")
    c3.refresh(f"/farms/{farm2}")
    c3.action("member.invite", {"farm_id": farm2, "phone": pw, "role": "manager", "salary_type": "monthly", "salary_amount": 26000})
    dash = w.c.get("/dashboard").text
    ok("You work on" in dash and "leave that job first" in dash, "the dashboard says so next to a new job offer (no Accept button)")
    sql("UPDATE invitations SET status = 'declined' WHERE farm_id = :f AND phone = :p AND status = 'pending'", f=farm2, p="+91" + pw)
    a.refresh(f"/farms/{farm}")
    a.action("job.create", {"farm_id": farm, "branch_id": branch, "title": "Harvest helper", "salary_type": "daily",
                            "salary_amount": 650, "positions": 3})
    job = str(sql("SELECT id FROM job_postings WHERE farm_id = :f AND status = 'open' ORDER BY created_at DESC LIMIT 1", f=farm)[0][0])
    page = w.c.get(f"/jobs/{job}").text
    ok("You work on" in page and 'id="apply-btn"' not in page, "a job page tells a working person why they can't apply")
    page = a.c.get(f"/jobs/{job}").text
    ok("This job is on your own farm" in page and 'id="apply-btn"' not in page, "the owner sees it is their own farm's job")
    refused(a, "job.apply", {"job_id": job, "accept_terms": True}, 409, "errors.own_farm_job", "an owner cannot apply to their own farm")
    refused(a, "member.invite", {"farm_id": farm, "phone": pb, "role": "manager", "salary_type": "monthly", "salary_amount": 26000},
            409, "errors.already_member", "a co-owner cannot also be hired as staff on the same farm")
    c3.refresh("/dashboard")
    ok('id="apply-btn"' in c3.c.get(f"/jobs/{job}").text, "an owner (own farm, no job) sees the Apply button on another farm's job")
    prep, r = c3.action("job.apply", {"job_id": job, "accept_terms": True}, expect_ok=False)
    ok(r.status_code == 200, "…and can apply: owning farms is fine, working is on one farm", r.text[:140])
    c3.refresh(f"/farms/{farm2}")
    c3.action("coowner.add", {"farm_id": farm2, "phone": pw})
    w.refresh("/dashboard")
    _, r = w.action("invitation.respond", {"invitation_id": str(sql(
        "SELECT id FROM invitations WHERE farm_id = :f AND role = 'co_owner' AND status = 'pending'", f=farm2)[0][0]),
        "accept": True, "accept_terms": True}, expect_ok=False)
    ok(r.status_code == 200, "…but can CO-OWN another farm (owning is not work)", r.text[:140])
    r = try_join(c3, farm2, branch2, d, pd, "manager")
    ok(r.status_code == 200, "an owner of their own farm can work on one other farm", r.text[:140])
    r = try_join(a, farm, branch, d, pd, "accountant")
    ok(r.status_code == 409 and r.json().get("error") == "errors.one_job_at_a_time", "…and only one", f"{r.status_code} {r.text[:140]}")

    rh, prh = person("Rhea", "farmer", "10.82.0.13", "8", "571943")
    for owner_, f in ((c3, farm2), (d, farm3)):
        owner_.refresh(f"/farms/{f}")
        owner_.action("member.invite", {"farm_id": f, "phone": prh, "role": "manager" if f == farm2 else "accountant",
                                        "salary_type": "monthly", "salary_amount": 26000})
    rh.refresh("/dashboard")
    preps = []
    for f in (farm2, farm3):
        inv = str(sql("SELECT id FROM invitations WHERE farm_id = :f AND phone = :p AND status = 'pending'", f=f, p="+91" + prh)[0][0])
        r = rh.post("/api/actions/prepare", json={"type": "invitation.respond", "payload": {"invitation_id": inv, "accept": True,
                                                                                               "accept_terms": True}})
        ok(r.status_code == 200, "both job offers can be looked at", r.text[:140])
        preps.append(r.json().get("action_id"))
    res: list = [None, None]

    def go(i: int) -> None:
        res[i] = rh.post(f"/api/actions/{preps[i]}/go", json={})

    threads = [threading.Thread(target=go, args=(i,)) for i in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    codes = sorted(r.status_code for r in res)
    jobs = sql("SELECT count(*) FROM farm_members WHERE user_id = :u AND status <> 'ended'", u=uid(prh))[0][0]
    ok(codes == [200, 409] and jobs == 1, "both offers accepted at the same moment: exactly one job, the other refused (no 500)",
       (codes, jobs, [r.text[:100] for r in res if r.status_code != 200]))

    print("   notice period: joining a new farm ends the old job that day (no more salary there; days worked stay owed)")
    today = sql("SELECT (now() AT TIME ZONE 'Asia/Kolkata')::date")[0][0]
    old = member(farm, pw)
    a.refresh(f"/farms/{farm}")
    a.action("member.give_notice", {"member_id": old, "reason": "Season is over"})
    ok(mstate(old)[1] == "notice", "Wasim is serving notice on Arjun's farm")
    d.refresh(f"/farms/{farm3}")
    d.action("member.invite", {"farm_id": farm3, "phone": pw, "role": "manager", "salary_type": "monthly", "salary_amount": 26000})
    inv = str(sql("SELECT id FROM invitations WHERE farm_id = :f AND phone = :p AND status = 'pending'", f=farm3, p="+91" + pw)[0][0])
    dash = w.c.get("/dashboard").text
    ok("You are serving notice at" in dash and f'data-invite-accept="{inv}"' in dash,
       "during notice the dashboard offers the new job, saying the old one ends")
    w.refresh("/dashboard")
    prep = w.post("/api/actions/prepare", json={"type": "invitation.respond", "payload": {"invitation_id": inv, "accept": True,
                                                                                           "accept_terms": True}})
    ok(prep.status_code == 200 and any("ends your job" in x for x in prep.json().get("warnings") or []),
       "the confirm step warns that the old job ends today", prep.text[:200])
    g = w.post(f"/api/actions/{prep.json().get('action_id')}/go", json={})
    ok(g.status_code == 200, "joining a new farm during the notice period works", g.text[:140])
    st = mstate(old)
    reason = sql("SELECT end_reason FROM farm_members WHERE id = :m", m=old)[0][0]
    ok(st[1] == "ended" and st[3] == today and reason == "joined_other_farm", "the old job ended today (joined another farm)",
       (st, reason))
    worked = sql("SELECT count(*), coalesce(sum(net), 0), max(work_date) FROM salary_accruals WHERE member_id = :m", m=old)[0]
    ok(worked[2] is None or worked[2] <= today, "no salary counts on the old farm after that day", worked)
    ok(worked[0] >= 1 and worked[1] > 0, "pay for the days already worked stays owed", worked)
    ok(sql("SELECT role, status FROM farm_members WHERE user_id = :u AND farm_id = :f", u=uid(pw), f=farm3) == [("manager", "active")],
       "…and Wasim now works on the new farm")
    ok(sql("SELECT count(*) FROM notifications WHERE user_id = :u AND kind = 'left_for_other_farm'", u=uid(pa))[0][0] >= 1,
       "the old farm's owner is told (and how much is still to pay)")
    ok(sql("SELECT count(*) FROM notifications WHERE user_id = :u AND kind = 'notice_job_ended'", u=uid(pw))[0][0] >= 1,
       "the worker is told their old job ended and what they are still owed")
    r = try_join(a, farm, branch, w, pw, "employee")
    ok(r.status_code == 409 and r.json().get("error") == "errors.one_job_at_a_time",
       "an ACTIVE job still blocks a second farm (only a notice period lets them move)", f"{r.status_code} {r.text[:140]}")
    return finish(t0)


if __name__ == "__main__":
    sys.exit(main())
