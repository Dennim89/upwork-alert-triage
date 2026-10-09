"""One pass: mail items -> parse -> store (dedupe) -> rules -> LLM (<= N per pass) -> cards.

Library functions, no process wiring: the daemon and the tests call the same code.
Never dies on one bad email or job; Telegram trouble stops the pass and leaves work queued in the store.
"""
from __future__ import annotations

import logging
from collections import Counter
from typing import Callable, Protocol

from . import parse, rules, scoring
from .store import Store, utc_iso

log = logging.getLogger(__name__)
SEND_SCORE_MIN = 7          # older answer schema only: a "skip" verdict is still sent when the score is at least this


class Sender(Protocol):
    def owner_chat(self) -> int | None: ...
    def send_card(self, job: dict, draft: dict | None, note: str | None) -> None: ...


Drafter = Callable[[dict], "tuple[dict | None, str | None]"]


def should_send(draft: dict | None) -> bool:
    if draft is None:                                           # no draft: the card says so and still goes out
        return True
    if draft.get("red_flag"):
        return False
    if draft.get("fit") is not None and draft.get("win") is not None:   # current schema: composite score decides
        return draft.get("verdict") == "apply"
    score = draft.get("score")
    return draft.get("verdict") == "apply" or (score is not None and score >= SEND_SCORE_MIN)


def add_job(store: Store, job: parse.Job, *, msgid: str, email_date: str | None, now: str | None = None,
            cfg: rules.RuleSettings = rules.DEFAULT) -> str:
    """Rules + insert once. Returns 'seen' | 'dup' | 'skip_rule' | 'candidate'."""
    if store.has_job(job.id):
        return "seen"
    fp = job.fingerprint()
    twin = store.find_fingerprint(fp)
    res = rules.check(job.title, job.text(), job.fields(), cfg)
    if twin:
        status, reason = "dup", f"same title+description as {twin}"
    elif res.skip:
        status, reason = "skip_rule", res.reason
    else:
        status, reason = "candidate", None
    if not store.add_job(job, msgid=msgid, email_date=email_date, status=status, reason=reason, fit=res.fit,
                         fit_tags=res.tags, fingerprint=fp, now=now):
        return "seen"
    if status != "candidate":
        log.info("%s %s %r: %s", status, job.id, job.title[:80], reason)
    return status


def ingest_email(store: Store, item, now: str | None = None, cfg: rules.RuleSettings = rules.DEFAULT) -> Counter:
    """Parse one mail item and store its jobs; the email is remembered even when it yields nothing."""
    st: Counter = Counter()
    email_date = utc_iso(item.date) if item.date else None
    kind = parse.classify(item.subject)
    try:
        jobs, diag = parse.parse_email(item.subject, item.html, item.text, item.msgid)
    except Exception:
        log.exception("parse failed for %s", item.msgid)
        store.mark_email(item.msgid, kind=kind, email_date=email_date, subject=item.subject, n_jobs=-1,
                         parser_version=parse.PARSER_VERSION, error="parse error", now=now)
        st["email_error"] += 1
        return st
    if not jobs:
        log.warning("no jobs parsed from %s %r (%s)", item.msgid, item.subject[:100], diag)
    for job in jobs:
        try:
            st[add_job(store, job, msgid=item.msgid, email_date=email_date, now=now, cfg=cfg)] += 1
        except Exception:
            log.exception("job failed: %s", getattr(job, "id", "?"))
            st["job_error"] += 1
    store.mark_email(item.msgid, kind=kind, email_date=email_date, subject=item.subject, n_jobs=len(jobs),
                     parser_version=parse.PARSER_VERSION, now=now)
    st["emails"] += 1
    return st


def deliver(store: Store, sender: Sender, job: dict, st: Counter, now: str | None = None) -> bool:
    try:
        sender.send_card(job, job.get("draft"), job.get("reason"))
    except Exception as e:
        status = store.send_failed(job["id"], now=now)
        log.warning("send failed for %s (now %s): %s", job["id"], status, e)
        st["send_error"] += 1
        return False
    store.mark_sent(job["id"], now=now)
    log.info("sent %s %r", job["id"], job["title"][:80])
    st["sent"] += 1
    return True


def process(store: Store, sender: Sender, drafter: Drafter, max_llm: int = 3,
            expire_before: str | None = None, now: str | None = None) -> Counter:
    """Queued work: resend 'ready' cards, then draft and send up to max_llm new candidates.
    `now` (UTC ISO) stamps the rows; None = the wall clock."""
    st: Counter = Counter()
    if expire_before:
        st["expired"] += store.expire_candidates(expire_before, now=now)
    try:
        chat = sender.owner_chat()
    except Exception as e:
        log.warning("telegram unavailable: %s", e)
        st["tg_error"] += 1
        return st
    if chat is None:
        log.info("owner chat unknown (press Start in the bot) or telegram backoff: candidates wait")
        st["no_owner"] += 1
        return st
    for job in store.by_status("ready"):
        if not deliver(store, sender, job, st, now):
            return st
    for job in store.next_candidates(max_llm):
        st["llm"] += 1
        try:
            draft, note = drafter(job)
        except Exception:
            log.exception("draft failed for %s", job["id"])
            draft, note = None, "error while drafting"
        draft = scoring.finalize(draft, job.get("fields"))
        if not should_send(draft):
            store.save_draft(job["id"], draft, "skip_llm", now=now)
            log.info("skip_llm %s score=%s %r: %s", job["id"], draft.get("score"), job["title"][:80],
                     (draft.get("why") or "")[:200])
            st["skip_llm"] += 1
            continue
        if draft is None:
            log.info("no draft for %s: %s", job["id"], note)
        store.save_draft(job["id"], draft, "ready", reason=note, now=now)
        if not deliver(store, sender, store.get(job["id"]), st, now):
            return st
    return st


def run_pass(store: Store, items, sender: Sender, drafter: Drafter, max_llm: int = 3, now: str | None = None,
             expire_before: str | None = None, cfg: rules.RuleSettings = rules.DEFAULT) -> Counter:
    """ingest_email() for every item, then process(): one complete pass (tests, scripts)."""
    st: Counter = Counter()
    for item in items:
        st.update(ingest_email(store, item, now, cfg))
    st.update(process(store, sender, drafter, max_llm, expire_before, now))
    return st
