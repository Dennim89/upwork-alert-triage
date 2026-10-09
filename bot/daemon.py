"""The daemon: Telegram long polling + Upwork mail every 3 min + an LLM queue, in one process.

Threads: main = getUpdates loop (your commands, card buttons, free text: fast handlers only); mail = mailbox pass
every MAIL_INTERVAL_S plus the daily summary; llm = one queued task at a time under the hourly budget (your texts >
client messages > job drafts). Every step is a plain method, so tests and --simulate drive them synchronously
with fake Telegram / IMAP / LLM. Only the owner chat is ever served.
"""
from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import assist, events, ledger, llm, parse, pipeline, rules, schedule
from . import state as statemod
from .config import EVENTS_BACKFILL_H, LOOKBACK_DAYS, MAIL_INTERVAL_S, POLL_TIMEOUT_S, Config
from .mail import MailError
from .store import Store, utc_iso
from .telegram import TelegramClient, TelegramError, esc, find_owner, format_card, format_proposal, inline_keyboard

log = logging.getLogger(__name__)
ALLOWED_UPDATES = ["message", "callback_query"]
NO_JSON = "the model's answer has no JSON"
HELP_TEXT = "\n".join([
    "<b>Commands</b>",
    "/stats: Connects balance, proposals, notifications, queue",
    "/balance N: set your Connects balance (without a number: show it)",
    "/pause: stop job cards (notifications and texts keep working)",
    "/resume: send job cards again",
    "/note TEXT: turn your own words (any language) into a message for the client",
    "/client TEXT: draft a reply to a client's message",
    "",
    "Plain text: a client's message pasted here gets a draft reply. Text mostly in a non-Latin script counts as "
    "your own note. Reply to a card to give me its context.",
    "Buttons on a job card: ✅ Sent, then the Connects it cost; ⏭ Skip.",
])
SEND_TEXT = ("Send text: a client's message (I draft a reply) or /note with your own words (I write the message "
             "for the client).")


def job_keyboard(job_id: str) -> dict:
    return inline_keyboard([[("✅ Sent", f"s:{job_id}"), ("⏭ Skip", f"k:{job_id}")]])


def connects_keyboard(job_id: str) -> dict:
    q = ledger.QUICK_CONNECTS
    rows = [[(str(n), f"c:{job_id}:{n}") for n in q[:3]], [(str(n), f"c:{job_id}:{n}") for n in q[3:]]]
    return inline_keyboard(rows + [[("↩ Back", f"b:{job_id}")]])


def status_keyboard(decision: dict, balance: int | None) -> dict:
    if decision.get("status") == "sent":
        text = f"✅ Sent · {decision.get('connects')} Connects" + (f" · balance {balance}" if balance is not None else "")
    else:
        text = "⏭ Skipped"
    return inline_keyboard([[(text, "x")]])


class StateBox:
    """state.json shared by threads: every change is applied and saved under one lock."""

    def __init__(self, path):
        self.path, self.lock = path, threading.RLock()
        self.data = statemod.load(path)

    def get(self, key, default=None):
        with self.lock:
            return self.data.get(key, default)

    def change(self, fn) -> None:
        with self.lock:
            fn(self.data)
            statemod.save(self.path, self.data)

    def wait(self, key: str, now: float) -> float:
        with self.lock:
            return statemod.backoff_wait(self.data, key, now)

    def has_backoff(self, key: str) -> bool:
        with self.lock:
            return key in (self.data.get("backoff") or {})


class JobCards:
    """pipeline.Sender for the daemon: job cards with the decision keyboard, message ids remembered."""

    def __init__(self, daemon: "Daemon"):
        self.d = daemon

    def owner_chat(self) -> int | None:
        return self.d.owner() if self.d.state.wait("telegram", self.d.clock()) <= 0 else None

    def send_card(self, job: dict, draft: dict | None, note: str | None) -> None:
        self.d.send(format_card(job, draft, note), kind="job_card", ref=job["id"], reply_markup=job_keyboard(job["id"]))
        if draft and draft.get("proposal_en"):
            self.d.send(format_proposal(draft), kind="job_proposal", ref=job["id"])


class Daemon:
    def __init__(self, cfg: Config, client: TelegramClient, *, fetch, llm_call, clock=time.time, sleep=None,
                 poll_timeout: int = POLL_TIMEOUT_S, mail_interval: float = MAIL_INTERVAL_S,
                 llm_per_hour: int | None = None):
        """fetch(select) -> [MailItem]; llm_call(prompt, system_prompt) -> (text, error)."""
        self.cfg, self.client, self.fetch, self.llm_call, self.clock = cfg, client, fetch, llm_call, clock
        self.poll_timeout, self.mail_interval = poll_timeout, mail_interval
        self.state = StateBox(cfg.state_path)
        self.stop_event = threading.Event()
        self.sleep = sleep or self.stop_event.wait
        self._tls = threading.local()
        self._stores: list[Store] = []
        self._stores_lock = threading.Lock()
        self.proof = llm.load_proof(cfg.profile_path)
        self.persona = llm.Persona(cfg.freelancer_name, cfg.owner_language)
        self.rules = rules.settings(cfg.niche_keywords, cfg.min_hourly_usd, cfg.min_fixed_usd)
        self.tz, self.tz_ok = schedule.zone(cfg.timezone)
        self.budget = assist.LlmBudget(self.store, llm_per_hour or cfg.llm_max_per_hour)
        self.cards = JobCards(self)
        self.last_mail = 0.0
        self.poll_fails = 0

    # ---------- plumbing ----------
    def store(self) -> Store:
        st = getattr(self._tls, "store", None)
        if st is None:
            st = Store(self.cfg.db_path, timeout=30)
            self._tls.store = st
            with self._stores_lock:
                self._stores.append(st)
        return st

    def close(self) -> None:
        with self._stores_lock:
            for st in self._stores:
                try:
                    st.close()
                except Exception:
                    pass
            self._stores.clear()

    def owner(self) -> int | None:
        v = self.state.get("owner_chat_id")
        return int(v) if v is not None else None

    def paused(self) -> bool:
        return self.store().kv_get("paused") == "1"

    def _iso(self, now: float) -> str:
        return utc_iso(datetime.fromtimestamp(now, timezone.utc))

    def _first_start(self) -> float:
        """Epoch of the daemon's first start (kv events_since); 0 when unknown."""
        since = self.store().kv_get("events_since")
        return datetime.fromisoformat(since.replace("Z", "+00:00")).timestamp() if since else 0.0

    def send(self, text: str, *, kind: str | None = None, ref=None, reply_markup: dict | None = None,
             reply_to: int | None = None) -> list[int]:
        """Owner-only send; transport errors start the Telegram backoff; message ids -> (kind, ref)."""
        owner = self.owner()
        if owner is None:
            raise TelegramError("owner chat unknown")
        try:
            ids = self.client.send(owner, text, reply_markup=reply_markup, reply_to=reply_to)
        except TelegramError as e:
            if e.code != 400:
                self.state.change(lambda d: statemod.backoff_fail(d, "telegram", self.clock()))
            raise
        if self.state.has_backoff("telegram"):
            self.state.change(lambda d: statemod.backoff_ok(d, "telegram"))
        if kind:
            for mid in ids:
                if mid:
                    self.store().add_tg_message(owner, mid, kind, ref)
        return ids

    def reply(self, text: str, reply_to: int | None = None) -> None:
        try:
            self.send(text, reply_to=reply_to)
        except TelegramError as e:
            log.warning("reply not sent: %s", e)

    def call_llm(self, prompt: str, system_prompt: str, purpose: str) -> tuple[str | None, str | None]:
        """Budgeted call; an exception becomes an error result, so a broken call never loops."""
        now = self.clock()
        try:
            text, error = self.llm_call(prompt, system_prompt)
        except Exception as e:
            log.exception("LLM call failed (%s)", purpose)
            text, error = None, f"LLM call error ({e.__class__.__name__})"
        if error != llm.NOT_FOUND:                              # no process started: not a budgeted call
            self.budget.record(now, purpose)
        return text, error

    def startup(self, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        store = self.store()
        if store.kv_get("events_since") is None:
            store.kv_set("events_since", self._iso(now))
        pinned = self.cfg.owner_chat_id
        if pinned is not None and self.owner() != pinned:
            self.state.change(lambda d: d.update(owner_chat_id=pinned, owner_name=""))
            log.info("owner chat pinned by TELEGRAM_OWNER_ID")
        if self.proof is None:
            log.warning("profile %s missing or without Overview/Proof bank: drafts unavailable", self.cfg.profile_path)
        if not self.tz_ok:
            log.warning("time zone %r not available (on Windows: pip install tzdata); the summary uses UTC",
                        self.cfg.timezone)
        log.info("daemon ready: owner=%s balance=%s paused=%s", "set" if self.owner() else "not yet",
                 store.balance(), self.paused())

    # ---------- Telegram ----------
    def tg_tick(self, timeout: int | None = None) -> int:
        """One getUpdates round; the offset is saved after every handled update."""
        try:
            updates = self.client.get_updates(offset=self.state.get("update_offset"),
                                              timeout=self.poll_timeout if timeout is None else timeout,
                                              allowed_updates=ALLOWED_UPDATES)
        except TelegramError as e:
            self.poll_fails += 1
            delay = min(300, 5 * 2 ** min(self.poll_fails - 1, 6))
            log.warning("telegram poll failed (%s); retry in %d s", e, delay)
            self.sleep(delay)
            return 0
        if self.poll_fails:
            log.info("telegram poll recovered after %d failures", self.poll_fails)
            self.poll_fails = 0
        for u in sorted(updates, key=lambda x: x.get("update_id", 0)):
            try:
                self.handle_update(u)
            except Exception:
                log.exception("update %s failed", u.get("update_id"))
            if isinstance(u.get("update_id"), int):
                nxt = u["update_id"] + 1
                self.state.change(lambda d: d.__setitem__("update_offset", nxt))
        return len(updates)

    def handle_update(self, u: dict) -> None:
        if "callback_query" in u:
            return self.on_callback(u["callback_query"])
        msg = u.get("message") or {}
        chat = msg.get("chat") or {}
        owner = self.owner()
        if owner is None:
            found = find_owner([u], pinned=self.cfg.owner_chat_id)
            if found:
                self.state.change(lambda d: d.update(owner_chat_id=found["chat_id"], owner_name=found["name"]))
                log.info("owner chat bound (%s)", found["name"] or "?")
                self.reply("Ready: job cards, Upwork notifications and reply drafts come to this chat. "
                           "/help lists the commands.")
            return
        if chat.get("id") != owner:
            log.info("ignored a message from a non-owner chat")
            return
        if isinstance(msg.get("date"), int) and msg["date"] < self._first_start():
            log.info("ignored a message sent before the daemon's first start")
            return
        text = (msg.get("text") or "").strip()
        if not text:
            self.reply(SEND_TEXT)
            return
        if text.startswith("/"):
            return self.on_command(text, msg)
        return self.on_owner_text(text, msg)

    def on_command(self, text: str, msg: dict) -> None:
        m = re.match(r"/(\w+)(?:@\w+)?(?:\s+(.*))?$", text, re.S)
        cmd, arg = (m.group(1).lower(), (m.group(2) or "").strip()) if m else ("", "")
        store, now = self.store(), self.clock()
        if cmd in ("start", "help"):
            self.reply(HELP_TEXT)
        elif cmd == "stats":
            self.reply(ledger.stats_text(store, now, self.tz, paused=self.paused(), llm_used=self.budget.used(now),
                                         llm_max=self.budget.max, owner_queue=store.count_owner_tasks()))
        elif cmd == "balance":
            if not arg:
                self.reply(f"Connects balance: {esc(ledger.balance_text(store))}")
            elif re.fullmatch(r"\d{1,6}", arg):
                n = store.set_balance(int(arg), ts=self._iso(now), note="/balance")
                self.reply(f"Connects balance: {n}")
            else:
                self.reply("Format: /balance 60")
        elif cmd == "pause":
            store.kv_set("paused", "1")
            self.reply("Paused: no job cards (jobs wait in the queue for up to 3 days). Notifications and texts "
                       "keep working. /resume to continue.")
        elif cmd == "resume":
            store.kv_set("paused", "0")
            self.reply(f"Resumed. Jobs in the queue: {store.count_jobs(status='candidate')}.")
        elif cmd in assist.MODES:
            if not arg:
                self.reply(f"Write the text after the command, for example: /{cmd} "
                           + ("can start Monday, need API access first" if cmd == "note"
                              else "Hi, can you also add CSV export?"))
                return
            self.on_owner_text(arg, msg, mode=cmd)
        else:
            self.reply("Unknown command. /help")

    def on_owner_text(self, text: str, msg: dict, mode: str | None = None) -> None:
        mode = mode or assist.text_mode(text)
        if mode is None:
            self.reply("No words found. Paste a client's message, or send /note with your own words.")
            return
        store, owner = self.store(), self.owner()
        rt = msg.get("reply_to_message") or {}
        ctx = ""
        if rt.get("message_id"):
            ctx = assist.context_for(store, store.tg_message(owner, rt["message_id"]), rt.get("text"))
        tid = store.add_owner_task(chat_id=owner, message_id=msg.get("message_id"), text=text[:4000], mode=mode,
                                   context=ctx[:3000] or None, now=self._iso(self.clock()))
        log.info("owner text queued #%s (%s, context %s)", tid, mode, "yes" if ctx else "no")
        wait = self.budget.wait_s(self.clock())
        if wait > 0:
            self.reply(f"The LLM limit ({self.budget.max} per hour) is used up: I will answer in about "
                       f"{math.ceil(wait / 60)} min.", reply_to=msg.get("message_id"))
            return
        try:
            self.client.chat_action(owner, "typing")
        except TelegramError:
            pass

    def on_callback(self, cq: dict) -> None:
        cq_id, data = cq.get("id"), cq.get("data") or ""
        msg = cq.get("message") or {}
        chat, mid = (msg.get("chat") or {}).get("id"), msg.get("message_id")
        owner = self.owner()
        if owner is None or (cq.get("from") or {}).get("id") != owner or chat != owner:
            self._answer(cq_id, "Not available")
            return
        action, _, rest = data.partition(":")
        if action == "x" or not rest:
            self._answer(cq_id, "Already recorded")
            return
        job_id, n = (rest.rsplit(":", 1) + [""])[:2] if action == "c" else (rest, "")
        store = self.store()
        decision = store.get_proposal(job_id)
        if decision:
            self._edit(chat, mid, status_keyboard(decision, store.balance()))
            self._answer(cq_id, "Already recorded")
            return
        job = store.get(job_id)
        title = job["title"] if job else None
        now = self._iso(self.clock())
        if action == "s":
            self._edit(chat, mid, connects_keyboard(job_id))
            self._answer(cq_id, "How many Connects did it cost?")
        elif action == "b":
            self._edit(chat, mid, job_keyboard(job_id))
            self._answer(cq_id)
        elif action == "k":
            store.record_decision(job_id=job_id, title=title, status="skipped", ts=now)
            self._edit(chat, mid, status_keyboard({"status": "skipped"}, None))
            self._answer(cq_id, "Skipped")
        elif action == "c" and n.isdigit() and 0 < int(n) <= 100:
            _, bal = store.record_decision(job_id=job_id, title=title, status="sent", connects=int(n), ts=now)
            self._edit(chat, mid, status_keyboard({"status": "sent", "connects": int(n)}, bal))
            self._answer(cq_id, f"Recorded: -{n} Connects" + (f", balance {bal}" if bal is not None else ""))
            log.info("proposal sent: %s (%s Connects, balance %s)", job_id, n, bal)
        else:
            self._answer(cq_id)

    def _answer(self, cq_id: str | None, text: str | None = None) -> None:
        if not cq_id:
            return
        try:
            self.client.answer(cq_id, text)
        except TelegramError as e:
            log.warning("answerCallbackQuery failed: %s", e)

    def _edit(self, chat: int, mid: int, markup: dict) -> None:
        try:
            self.client.edit_markup(chat, mid, markup)
        except TelegramError as e:
            log.warning("editMessageReplyMarkup failed: %s", e)

    # ---------- mail ----------
    def wants(self, head: dict) -> bool:
        """mail.fetch_upwork select(): every Upwork mail (any subject) not handled yet."""
        return not self.store().email_done(head["msgid"], parse.PARSER_VERSION)

    def mail_tick(self, now: float | None = None, force: bool = False) -> Counter:
        now = self.clock() if now is None else now
        if not force and now - self.last_mail < self.mail_interval:
            return Counter()
        self.last_mail = now
        wait = self.state.wait("imap", now)
        if wait > 0:
            log.info("imap: backoff, %d s left", wait)
            return Counter()
        try:
            items = self.fetch(self.wants)
        except MailError as e:
            delay = 0

            def fail(d):
                nonlocal delay
                delay = statemod.backoff_fail(d, "imap", now)
            self.state.change(fail)
            log.warning("imap: %s; next try in %d min", e, delay // 60)
            return Counter()
        if self.state.has_backoff("imap"):
            self.state.change(lambda d: statemod.backoff_ok(d, "imap"))
        st: Counter = Counter()
        store = self.store()
        for item in sorted(items, key=lambda i: i.date or datetime.min.replace(tzinfo=timezone.utc)):
            try:
                st.update(self.ingest_mail(store, item, now))
            except Exception:
                log.exception("mail %s failed", item.msgid)
                store.mark_email(item.msgid, kind="error", email_date=None, subject=item.subject, n_jobs=-1,
                                 parser_version=parse.PARSER_VERSION, error="ingest error")
                st["mail_error"] += 1
        st.update(self.deliver_events(store, now))
        if items:
            log.info("mail pass: %s", dict(st))
        return st

    def ingest_mail(self, store: Store, item, now: float) -> Counter:
        text, anchors = events.mail_text(item)
        kind = events.classify_event(item.subject, text)
        if self.cfg.save_event_samples:
            stamp = (item.date or datetime.fromtimestamp(now, timezone.utc)).astimezone(timezone.utc).strftime(
                "%Y%m%d-%H%M%S")
            events.save_sample(item.raw, kind, self.cfg.samples_dir, stamp)
        if kind == "job_alert" or (kind == "digest" and parse.parse_links(item.html, item.text)):
            return pipeline.ingest_email(store, item, now=self._iso(now), cfg=self.rules)
        email_date = utc_iso(item.date) if item.date else None
        ev = events.build_event(kind, item.subject, text, anchors)
        since = store.kv_get("events_since")
        cutoff = utc_iso(datetime.fromisoformat(since.replace("Z", "+00:00")) - timedelta(hours=EVENTS_BACKFILL_H)) \
            if since else None
        if cutoff and email_date and email_date < cutoff:
            status = "baseline"                                  # before the daemon existed: recorded, not sent
        elif kind == "client_message" and ev["body"]:
            status = "pending_llm"
        else:
            status = "new"
        store.add_event(msgid=item.msgid, kind=kind, subject=item.subject, email_date=email_date, status=status,
                        now=self._iso(now), **ev)
        store.mark_email(item.msgid, kind=kind, email_date=email_date, subject=item.subject, n_jobs=0,
                         parser_version=parse.PARSER_VERSION, now=self._iso(now))
        log.info("event %s (%s): %r", kind, status, item.subject[:100])
        return Counter({f"event:{kind}": 1, status: 1})

    def deliver_events(self, store: Store, now: float | None = None) -> Counter:
        st: Counter = Counter()
        if self.owner() is None or self.state.wait("telegram", self.clock() if now is None else now) > 0:
            return st
        for ev in store.events_by_status("new"):
            try:
                self.send(events.event_card(ev), kind="event", ref=ev["id"])
            except TelegramError as e:
                log.warning("event %s not sent: %s", ev["id"], e)
                st["event_send_error"] += 1
                break
            store.set_event(ev["id"], status="sent")
            st["event_sent"] += 1
        return st

    # ---------- LLM queue ----------
    def llm_tick(self, now: float | None = None) -> bool:
        """At most one task: your text > client message > job draft. True when something was done."""
        now = self.clock() if now is None else now
        if self.owner() is None or self.state.wait("telegram", now) > 0:
            return False
        store = self.store()
        self._resend_unsent(store)
        if self.budget.remaining(now) > 0:
            task = store.next_owner_task()
            if task:
                self.run_owner_task(store, task)
                return True
            pending = store.events_by_status("pending_llm", limit=1)
            if pending:
                self.run_client_event(store, pending[0])
                return True
        if self.paused():
            return False
        expire = utc_iso(datetime.fromtimestamp(now, timezone.utc) - timedelta(days=LOOKBACK_DAYS))
        st = pipeline.process(store, self.cards, self._draft_job, max_llm=1 if self.budget.remaining(now) > 0 else 0,
                              expire_before=expire, now=self._iso(now))
        return st.get("llm", 0) > 0

    def _draft_job(self, job: dict):
        return llm.draft(job, self.proof, self.persona,
                         caller=lambda prompt: self.call_llm(prompt, llm.SYSTEM_PROMPT, "job"))

    def _resend_unsent(self, store: Store) -> None:
        """Results whose Telegram delivery failed after the LLM call: resend without a new call."""
        for task in store.owner_tasks_by_status("unsent"):
            res = task.get("result") or {}
            try:
                self._send_task(task, res)
            except TelegramError:
                return
            store.finish_owner_task(task["id"], "done", result=res)
        for ev in store.events_by_status("unsent"):
            try:
                for m in events.client_message_cards(ev, ev.get("draft"), ev.get("note")):
                    self.send(m, kind="event", ref=ev["id"])
            except TelegramError:
                return
            store.set_event(ev["id"], status="sent", note=ev.get("note"))

    def _send_task(self, task: dict, res: dict) -> None:
        warning = assist.offplatform_warning(task["text"], res.get("message_en") or res.get("reply_en"))
        for i, m in enumerate(assist.owner_messages(task["mode"], res, warning)):
            self.send(m, kind="assist", ref=task["id"], reply_to=task.get("message_id") if i == 0 else None)

    def run_owner_task(self, store: Store, task: dict) -> None:
        context = task.get("context") or ""
        if task["mode"] == "note":
            prompt, system, parse_fn = assist.note_prompt(task["text"], context, self.persona), \
                assist.NOTE_SYSTEM, assist.parse_note
        else:
            prompt, system, parse_fn = assist.client_prompt(task["text"], self.proof, context,
                                                            persona=self.persona), \
                assist.CLIENT_SYSTEM, assist.parse_client
        text, error = self.call_llm(prompt, system, f"owner_{task['mode']}")
        res = None if error else parse_fn(text)
        if res is None:
            note = error or NO_JSON
            self.reply(f"Could not prepare the text: {esc(note)}. Try again later.", task.get("message_id"))
            store.finish_owner_task(task["id"], "failed", note=note)
            return
        try:
            self._send_task(task, res)
        except TelegramError:
            store.finish_owner_task(task["id"], "unsent", result=res)
            return
        store.finish_owner_task(task["id"], "done", result=res)

    def run_client_event(self, store: Store, ev: dict) -> None:
        ctx = assist.context_by_title(store, ev.get("title"))
        prompt = assist.client_prompt(ev["body"], self.proof, ctx, client=ev.get("client"), persona=self.persona)
        text, error = self.call_llm(prompt, assist.CLIENT_SYSTEM, "client_message")
        res = None if error else assist.parse_client(text)
        note = None if res else (error or NO_JSON)
        try:
            for m in events.client_message_cards(ev, res, note):
                self.send(m, kind="event", ref=ev["id"])
        except TelegramError:
            store.set_event(ev["id"], status="unsent", draft=res, note=note)
            return
        store.set_event(ev["id"], status="sent", draft=res, note=note)

    # ---------- schedule ----------
    def schedule_tick(self, now: float | None = None) -> list[str]:
        """The daily summary at SUMMARY_HOUR local time (once a day)."""
        now = self.clock() if now is None else now
        if self.owner() is None or self.state.wait("telegram", now) > 0:
            return []
        store = self.store()
        day = schedule.summary_due(now, store.kv_get("summary_day"), self.cfg.summary_hour, self.tz)
        if not day:
            return []
        d = schedule.local_day(now, self.tz)
        start = datetime.fromisoformat(schedule.day_bounds(d, self.tz)[0].replace("Z", "+00:00")).timestamp()
        try:
            self.send(ledger.summary_text(store, d, self.tz, llm_calls=store.llm_calls_between(start, start + 86400),
                                          paused=self.paused()), kind="summary")
        except TelegramError as e:
            log.warning("summary not sent: %s", e)
            return []
        store.kv_set("summary_day", day)
        store.prune_llm_calls(now - 7 * 86400)
        return ["summary"]

    # ---------- threads ----------
    def _loop(self, name: str, step, idle_s: float) -> None:
        while not self.stop_event.is_set():
            try:
                busy = step()
            except Exception:
                log.exception("%s step failed", name)
                busy = False
            if not busy:
                self.stop_event.wait(idle_s)
        log.info("%s thread stopped", name)

    def _mail_step(self) -> bool:
        self.mail_tick()
        self.schedule_tick()
        return False

    def run(self) -> None:
        """Blocks until stop(): getUpdates in this thread, mail and LLM workers in daemon threads."""
        self.startup()
        workers = [threading.Thread(target=self._loop, args=("mail", self._mail_step, 5), name="mail", daemon=True),
                   threading.Thread(target=self._loop, args=("llm", self.llm_tick, 3), name="llm", daemon=True)]
        for t in workers:
            t.start()
        try:
            while not self.stop_event.is_set():
                try:
                    self.tg_tick()
                except Exception:
                    log.exception("poll loop error")
                    self.stop_event.wait(5)
        except KeyboardInterrupt:
            log.info("interrupted")
        finally:
            self.stop_event.set()
            for t in workers:
                t.join(timeout=15)
            self.close()

    def stop(self) -> None:
        self.stop_event.set()
