"""Action framework: every change is PREPARED (validated + human summary) and only EXECUTED after the user presses GO.

* Double-clicking GO can never run an action twice (atomic status transition).
* Actions marked `consensus` on farms with co-owners become proposals that need unanimous approval
  (owner + every co-owner) before they execute.
* The AI assistant can only *propose* actions (whitelisted per action); people still press GO.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError, Conflict, Forbidden, NotFound
from app.i18n import t
from app.models import Farm, FarmProposal, PendingAction, ProposalVote, User
from app.services.audit import audit
from app.services.notify import notify, notify_many
from app.services.perms import coowner_ids
from app.utils import now_utc, to_jsonable

log = logging.getLogger("kisansetu.actions")
EXPIRY = timedelta(minutes=20)
_SEALED = "__sealed__"


def _secret(key: Any) -> bool:
    from app.security.sanitize import SECRET_KEYS

    return isinstance(key, str) and key.lower() in SECRET_KEYS


def seal_payload(user_id: uuid.UUID, payload: dict[str, Any]) -> dict[str, Any]:
    """Passwords / PINs / codes in a prepared action are stored encrypted (AES-GCM, bound to the person and field) for
    the minutes until GO — never as plain text in the database."""
    from app.security.crypto import cipher

    return {k: ({_SEALED: cipher.encrypt(v, f"pending:{user_id}:{k}")} if _secret(k) and isinstance(v, str) else v)
            for k, v in payload.items()}


def unseal_payload(user_id: uuid.UUID, payload: dict[str, Any]) -> dict[str, Any]:
    from app.security.crypto import cipher

    return {k: (cipher.decrypt_str(v[_SEALED], f"pending:{user_id}:{k}") if isinstance(v, dict) and _SEALED in v else v)
            for k, v in (payload or {}).items()}


def strip_secrets(payload: dict[str, Any] | None) -> dict[str, Any]:
    """The payload without any secret field — what is kept once an action is over (and what the audit log sees)."""
    return {k: v for k, v in (payload or {}).items() if not _secret(k)}


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


@dataclass
class Ctx:
    db: AsyncSession
    user: User
    lang: str
    ip: str | None = None
    source: str = "ui"
    action_id: uuid.UUID | None = None   # set at GO time → stable idempotency key for side effects
    sid_hash: str | None = None          # current session (kept when revoking other sessions)

    def t(self, key: str, **params: Any) -> str:
        return t(key, self.lang, **params)


@dataclass
class Prepared:
    summary: list[str]
    warnings: list[str] = field(default_factory=list)
    farm_id: uuid.UUID | None = None
    data: dict[str, Any] = field(default_factory=dict)


class Action:
    type: ClassVar[str]
    Payload: ClassVar[type[BaseModel]]
    roles: ClassVar[set[str] | None] = None   # platform roles allowed (None → any active user)
    consensus: ClassVar[bool] = False
    ai_allowed: ClassVar[bool] = False
    ai_hint: ClassVar[str] = ""

    async def prepare(self, ctx: Ctx, p: Any) -> Prepared:  # pragma: no cover - abstract
        raise NotImplementedError

    async def execute(self, ctx: Ctx, p: Any, prep: Prepared) -> dict[str, Any]:  # pragma: no cover - abstract
        raise NotImplementedError

    async def excluded_voters(self, db: AsyncSession, payload: dict[str, Any]) -> set[uuid.UUID]:
        """People who must not vote on this proposal (the co-owner being removed)."""
        return set()


REGISTRY: dict[str, Action] = {}


def register(cls: type[Action]) -> type[Action]:
    REGISTRY[cls.type] = cls()
    return cls


def _parse(action: Action, payload: dict[str, Any]) -> Any:
    try:
        return action.Payload.model_validate(payload or {})
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(x) for x in first.get("loc", ()))
        raise AppError("errors.invalid_field", field=loc or "?", detail=first.get("msg", "")[:120]) from exc


def _check_role(action: Action, user: User) -> None:
    if user.status != "active":
        raise Forbidden("errors.account_inactive")
    if action.roles is not None and user.role not in action.roles:
        raise Forbidden("errors.role_not_allowed")


async def proposal_voters(db: AsyncSession, action: Action, farm: Farm, payload: dict[str, Any]) -> list[uuid.UUID]:
    """Owner + every current co-owner, minus anyone the action excludes."""
    excluded = await action.excluded_voters(db, payload or {})
    return [v for v in dict.fromkeys([farm.owner_id, *await coowner_ids(db, farm.id)]) if v not in excluded]


async def _needs_consensus(ctx: Ctx, action: Action, prep: Prepared, payload: dict[str, Any]) -> list[uuid.UUID]:
    """Other voters (owner + co-owners except the proposer) — empty list → execute directly."""
    if not action.consensus or not prep.farm_id or ctx.source == "proposal":
        return []
    farm = await ctx.db.get(Farm, prep.farm_id)
    if not await coowner_ids(ctx.db, farm.id):
        return []                                   # no co-owners: the owner decides alone
    return [v for v in await proposal_voters(ctx.db, action, farm, payload) if v != ctx.user.id]


async def prepare_action(db: AsyncSession, user: User, lang: str, ip: str | None, action_type: str,
                         payload: dict[str, Any], source: str = "ui") -> dict[str, Any]:
    action = REGISTRY.get(action_type)
    if not action:
        raise NotFound("errors.unknown_action")
    if source == "ai" and not action.ai_allowed:
        raise Forbidden("errors.ai_action_not_allowed")
    _check_role(action, user)
    p = _parse(action, payload)
    ctx = Ctx(db, user, lang, ip, source)
    prep = await action.prepare(ctx, p)
    voters = await _needs_consensus(ctx, action, prep, p.model_dump(mode="json", exclude_unset=True))
    # exclude_unset: GO (and co-owner proposals) re-parse exactly what was sent, so partial updates stay partial —
    # storing the full dump made every field "set" and wiped unspecified fields (found by the stress test: HTTP 500)
    pa = PendingAction(user_id=user.id, action_type=action_type,
                       payload=seal_payload(user.id, to_jsonable(p.model_dump(mode="json", exclude_unset=True))),
                       summary=prep.summary, warnings=prep.warnings, farm_id=prep.farm_id, source=source,
                       expires_at=now_utc() + EXPIRY)
    db.add(pa)
    await db.flush()
    return {"action_id": str(pa.id), "type": action_type, "summary": prep.summary, "warnings": prep.warnings,
            "consensus": bool(voters), "voters": len(voters), "go_label": t("action.go", lang)}


async def go_action(db: AsyncSession, user: User, lang: str, ip: str | None, action_id: uuid.UUID,
                    sid_hash: str | None = None) -> dict[str, Any]:
    claimed = (await db.execute(update(PendingAction).where(
        PendingAction.id == action_id, PendingAction.user_id == user.id, PendingAction.status == "prepared",
        PendingAction.expires_at > now_utc()).values(status="executing").returning(PendingAction.id))).scalar_one_or_none()
    if not claimed:
        raise Conflict("errors.action_expired_or_done")
    pa = await db.get(PendingAction, action_id)
    action = REGISTRY[pa.action_type]
    _check_role(action, user)
    p = _parse(action, unseal_payload(user.id, pa.payload))
    public = strip_secrets(pa.payload)
    pa.payload = public                  # the secrets are used once, here, and never kept
    ctx = Ctx(db, user, lang, ip, pa.source, action_id=pa.id, sid_hash=sid_hash)
    prep = await action.prepare(ctx, p)  # re-validate against current state
    voters = await _needs_consensus(ctx, action, prep, public)
    if voters:
        prop = FarmProposal(farm_id=prep.farm_id, action_type=pa.action_type, payload=public, summary=prep.summary,
                            proposed_by_id=user.id)
        db.add(prop)
        await db.flush()
        db.add(ProposalVote(proposal_id=prop.id, user_id=user.id, vote="approve"))
        farm = await db.get(Farm, prep.farm_id)
        await notify_many(db, voters, "proposal", title_key="notif.proposal.title", body_key="notif.proposal.body",
                          params={"farm": farm.name, "by": user.full_name, "what": t(f"action.name.{pa.action_type}", "en")},
                          link=f"/farms/{farm.id}?tab=proposals#p-{prop.id}", severity="warning", channels=("email",))
        pa.status, pa.result = "proposed", {"proposal_id": str(prop.id)}
        await audit(db, user.id, f"propose:{pa.action_type}", "farm", prep.farm_id, {"proposal": str(prop.id)}, ip)
        return {"status": "proposed", "message": t("action.proposed", lang, count=len(voters)), "proposal_id": str(prop.id)}
    result = await action.execute(ctx, p, prep)
    pa.status, pa.result = "executed", to_jsonable(result)
    await audit(db, user.id, pa.action_type, "action", pa.id, {"payload": public, "source": pa.source}, ip)
    return {"status": "executed", **to_jsonable(result)}


async def cancel_action(db: AsyncSession, user: User, action_id: uuid.UUID) -> None:
    pa = (await db.execute(select(PendingAction).where(PendingAction.id == action_id, PendingAction.user_id == user.id,
                                                       PendingAction.status == "prepared").with_for_update())).scalar_one_or_none()
    if pa:
        pa.status, pa.payload = "cancelled", strip_secrets(pa.payload)


async def vote_proposal(db: AsyncSession, user: User, lang: str, ip: str | None, proposal_id: uuid.UUID, vote: str) -> dict[str, Any]:
    if vote not in ("approve", "reject"):
        raise AppError("errors.invalid_input")
    prop = (await db.execute(select(FarmProposal).where(FarmProposal.id == proposal_id).with_for_update())).scalar_one_or_none()
    if not prop:
        raise NotFound()
    if prop.status != "pending":
        raise Conflict("errors.proposal_closed")
    farm = await db.get(Farm, prop.farm_id)
    voters = await proposal_voters(db, REGISTRY[prop.action_type], farm, prop.payload)
    if user.id not in voters:
        raise Forbidden("errors.not_a_voter")
    existing = (await db.execute(select(ProposalVote).where(ProposalVote.proposal_id == prop.id,
                                                            ProposalVote.user_id == user.id))).scalar_one_or_none()
    if existing:
        raise Conflict("errors.already_voted")
    db.add(ProposalVote(proposal_id=prop.id, user_id=user.id, vote=vote))
    await db.flush()
    await audit(db, user.id, f"vote:{vote}", "proposal", prop.id, {}, ip)
    if vote == "reject":
        prop.status, prop.decided_at = "rejected", now_utc()
        await notify_many(db, voters, "proposal_result", title_key="notif.proposal_rejected.title",
                          body_key="notif.proposal_rejected.body", params={"farm": farm.name, "by": user.full_name},
                          link=f"/farms/{farm.id}?tab=proposals", severity="warning")
        return {"status": "rejected"}
    approved = set((await db.execute(select(ProposalVote.user_id).where(
        ProposalVote.proposal_id == prop.id, ProposalVote.vote == "approve"))).scalars())
    if not set(voters) <= approved:
        return {"status": "pending", "remaining": len(set(voters) - approved)}
    return await _finalize(db, prop, farm, voters, lang, ip)


async def _finalize(db: AsyncSession, prop: FarmProposal, farm: Farm, voters: list[uuid.UUID], lang: str,
                    ip: str | None) -> dict[str, Any]:
    """Run an approved proposal as its proposer. It runs inside a SAVEPOINT: if it fails half-way, none of its partial
    changes (nor their after-commit side effects) survive — only the 'failed' status does."""
    proposer = await db.get(User, prop.proposed_by_id)
    action = REGISTRY[prop.action_type]
    ctx = Ctx(db, proposer, proposer.language or lang, ip, "proposal", action_id=prop.id)
    fname, fid = farm.name, farm.id          # read now: a rolled-back savepoint expires what it touched (async: no lazy load)
    tasks, emits = db.info.setdefault("after_commit_tasks", []), db.info.setdefault("pending_emits", [])
    mark, emark = len(tasks), len(emits)
    running = db.info.setdefault("ks_finalizing", set())
    running.add(prop.id)            # its own effects (a co-owner removal settles proposals) must not run it a second time
    try:
        async with db.begin_nested():
            _check_role(action, proposer)
            p = _parse(action, prop.payload)
            prep = await action.prepare(ctx, p)
            result = await action.execute(ctx, p, prep)
        prop.status, prop.result = "executed", to_jsonable(result)
    except AppError as exc:
        del tasks[mark:]
        del emits[emark:]
        prop.status, prop.error = "failed", t(exc.key, lang, **exc.params)
    finally:
        running.discard(prop.id)
    prop.decided_at = now_utc()
    await notify_many(db, voters, "proposal_result", title_key=f"notif.proposal_{prop.status}.title",
                      body_key=f"notif.proposal_{prop.status}.body", params={"farm": fname, "error": prop.error or ""},
                      link=f"/farms/{fid}?tab=proposals", severity="success" if prop.status == "executed" else "danger")
    return {"status": prop.status, "error": prop.error}


async def settle_proposals(db: AsyncSession, farm_id: uuid.UUID, lang: str, ip: str | None) -> None:
    """A co-owner left or was removed: proposals THEY made can no longer pass (failed); proposals that were only waiting
    for their vote now have every remaining voter's approval and run. Rows being voted on right now are skipped (the
    vote in progress decides them) — no lock cycle with a concurrent vote."""
    farm = await db.get(Farm, farm_id)
    props = list((await db.execute(select(FarmProposal).where(FarmProposal.farm_id == farm_id, FarmProposal.status == "pending")
                                   .order_by(FarmProposal.created_at).with_for_update(skip_locked=True))).scalars())
    if not props:
        return
    base = list(dict.fromkeys([farm.owner_id, *await coowner_ids(db, farm_id)]))
    approvals: dict[uuid.UUID, set[uuid.UUID]] = {}
    for pid, uid in (await db.execute(select(ProposalVote.proposal_id, ProposalVote.user_id).where(
            ProposalVote.proposal_id.in_([p.id for p in props]), ProposalVote.vote == "approve"))).all():
        approvals.setdefault(pid, set()).add(uid)
    running = db.info.get("ks_finalizing", set())
    for prop in props:
        if prop.id in running:          # the proposal whose execution got us here (same transaction, so not skip-locked)
            continue
        action = REGISTRY.get(prop.action_type)
        if prop.proposed_by_id not in base or action is None:
            prop.status, prop.error, prop.decided_at = "failed", t("errors.proposer_left", lang), now_utc()
            continue
        excluded = await action.excluded_voters(db, prop.payload or {}) if prop.action_type == "coowner.remove" else set()
        voters = [v for v in base if v not in excluded]
        if set(voters) <= approvals.get(prop.id, set()):
            await _finalize(db, prop, farm, voters, lang, ip)


def ai_action_specs(role: str) -> list[dict[str, Any]]:
    out = []
    for name, a in REGISTRY.items():
        if not a.ai_allowed or (a.roles is not None and role not in a.roles):
            continue
        out.append({"type": name, "params": a.ai_hint})
    return out
