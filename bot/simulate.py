"""Offline demo of the daemon (`python bot_daemon.py --simulate`): fake Bot API, fake mailbox, canned LLM answers,
temp data folder. Prints every message the bot would send (as Telegram shows it: tags dropped, links as
"label: url"), its buttons, button edits and button answers. Nothing here touches the network, your .env or
your data folder; all inputs are the synthetic test fixtures.
"""
from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import assist, llm, mail
from . import state as statemod
from .config import BASE_DIR, Config
from .telegram import TelegramClient, html_to_plain

FIXTURES = BASE_DIR / "tests" / "fixtures"
OWNER = 1001
SIM_MAILS = ("alert_csv.eml", "alert_slack.eml", "alert_red_flag.eml", "alert_low_rate.eml",
             "events/client_message_text.eml", "events/offer.eml", "events/unknown_newsletter.eml")
SIM_START = datetime(2026, 5, 14, 18, 0, tzinfo=timezone.utc)
CANNED_JOBS = {"Python script to merge supplier price lists into one CSV": "llm_job_csv.json",
               "Slack bot that answers HR policy questions with an LLM": "llm_job_slack.json"}


def render_keyboard(markup: dict | None) -> str:
    rows = (markup or {}).get("inline_keyboard") or []
    return " / ".join(" ".join(f"[{b['text']}]" for b in row) for row in rows)


class FakeBotApi:
    """urlopen stand-in for the Bot API: scripted getUpdates batches, auto message ids, every call recorded."""

    def __init__(self, out=None, first_id: int = 100):
        self.out, self.next_id = out, first_id
        self.batches: list[list[dict]] = []
        self.calls: list[tuple[str, dict]] = []
        self.fail: dict[str, list[BaseException]] = {}

    def push(self, *updates: dict) -> None:
        self.batches.append(list(updates))

    def sent(self) -> list[dict]:
        return [p for m, p in self.calls if m == "sendMessage"]

    def __call__(self, req, timeout=None):
        method = req.full_url.rsplit("/", 1)[-1]
        params = json.loads(req.data.decode("utf-8")) if req.data else {}
        self.calls.append((method, params))
        if self.fail.get(method):
            raise self.fail[method].pop(0)
        if method == "getUpdates":
            result = self.batches.pop(0) if self.batches else []
        elif method == "sendMessage":
            self.next_id += 1
            params["_id"] = self.next_id
            result = {"message_id": self.next_id, "chat": {"id": params["chat_id"]}}
        elif method == "getMe":
            result = {"id": 1, "is_bot": True, "username": "simulated_jobs_bot"}
        else:
            result = True
        if self.out is not None:
            self._print(method, params)
        return io.BytesIO(json.dumps({"ok": True, "result": result}).encode("utf-8"))

    def _print(self, method: str, p: dict) -> None:
        def w(s=""):
            print(s, file=self.out)
        if method == "sendMessage":
            reply = (p.get("reply_parameters") or {}).get("message_id")
            copy = " (a tap-to-copy block)" if p["text"].startswith("<pre>") else ""
            w(f"\n[bot -> you] message #{p['_id']}" + (f" (reply to #{reply})" if reply else "") + copy)
            w(html_to_plain(p["text"]) if p.get("parse_mode") == "HTML" else p["text"])
            if p.get("reply_markup"):
                w("buttons: " + render_keyboard(p["reply_markup"]))
        elif method == "editMessageReplyMarkup":
            w(f"[bot] buttons of message #{p['message_id']} -> {render_keyboard(p.get('reply_markup'))}")
        elif method == "answerCallbackQuery":
            w(f"[bot] answer to the button press: \"{p.get('text', '')}\"")
        elif method == "sendChatAction":
            w("[bot] typing...")


def message_update(update_id: int, text: str, message_id: int, reply_to: int | None = None, chat_id: int = OWNER,
                   reply_text: str | None = None) -> dict:
    msg = {"message_id": message_id, "text": text, "chat": {"id": chat_id, "type": "private"},
           "from": {"id": chat_id, "is_bot": False}}
    if reply_to:
        msg["reply_to_message"] = {"message_id": reply_to, "text": reply_text or ""}
    return {"update_id": update_id, "message": msg}


def callback_update(update_id: int, data: str, message_id: int, user_id: int = OWNER) -> dict:
    return {"update_id": update_id, "callback_query": {"id": f"cb{update_id}", "data": data,
                                                       "from": {"id": user_id, "is_bot": False},
                                                       "message": {"message_id": message_id,
                                                                   "chat": {"id": OWNER, "type": "private"}}}}


def canned_llm(fixtures: Path = FIXTURES):
    """llm_call(prompt, system_prompt) returning saved answers by prompt type (no CLI, no network)."""
    jobs = {title: llm.parse_cli_output((fixtures / name).read_text(encoding="utf-8"))[0]
            for title, name in CANNED_JOBS.items()}
    client = (fixtures / "llm_client_reply.json").read_text(encoding="utf-8")
    note = (fixtures / "llm_note.json").read_text(encoding="utf-8")
    default_job = jobs[next(iter(CANNED_JOBS))]

    def call(prompt: str, system_prompt: str):
        if system_prompt == assist.NOTE_SYSTEM:
            return note, None
        if system_prompt == assist.CLIENT_SYSTEM:
            return client, None
        return next((text for title, text in jobs.items() if f"Title: {title}\n" in prompt), default_job), None
    return call


def fixture_fetch(names, fixtures: Path = FIXTURES):
    items = [mail.decode_message((fixtures / n).read_bytes(), uid=str(i)) for i, n in enumerate(names, 1)]

    def fetch(select):
        return [it for it in items if select({"uid": it.uid, "msgid": it.msgid, "date": it.date,
                                             "subject": it.subject, "sender": it.sender})]
    return fetch


def sim_config(base: Path) -> Config:
    """Fake credentials, a temp data folder, the example profile."""
    return Config("you@example.com", "sim-password", "0:simulate", data_dir=base / "data", logs_dir=base / "logs",
                  profile_path=BASE_DIR / "profile.example.md", freelancer_name="Jordan")


def run(out=None) -> int:
    from .daemon import Daemon                                  # late import: daemon imports this package too
    out = out or sys.stdout
    if out is sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    tmp = Path(tempfile.mkdtemp(prefix="upwork_sim_"))
    say = lambda s: print(s, file=out)  # noqa: E731
    try:
        cfg = sim_config(tmp)
        statemod.save(cfg.state_path, {"owner_chat_id": OWNER, "owner_name": "you"})
        api = FakeBotApi(out)
        t0 = SIM_START.timestamp()
        clock = [t0]
        d = Daemon(cfg, TelegramClient("0:simulate", opener=api, sleep=lambda s: None), fetch=fixture_fetch(SIM_MAILS),
                   llm_call=canned_llm(), clock=lambda: clock[0], sleep=lambda s: None, poll_timeout=0)
        try:
            d.startup()
            say("SIMULATION: no network. Fake Telegram, a fake mailbox with synthetic e-mails, canned model answers.")
            say(f"Clock: {SIM_START:%Y-%m-%d %H:%M} UTC. Profile: profile.example.md. Signed as: Jordan.")
            say("\n== 1. You set your Connects balance ==")
            say("[you] /balance 60")
            api.push(message_update(1, "/balance 60", message_id=40))
            d.tg_tick(timeout=0)
            say(f"\n== 2. Mailbox pass: {len(SIM_MAILS)} e-mails (4 job alerts, a client message, an offer, "
                "a newsletter) ==")
            st = d.mail_tick(force=True)
            store = d.store()
            say(f"[mail] job alerts {st['emails']}: {st['candidate']} for the LLM, {st['skip_rule']} filtered by "
                f"rules; notifications {st['new'] + st['pending_llm']}: {st['event_sent']} sent now, "
                f"{st['pending_llm']} waiting for the LLM")
            for job in store.by_status("skip_rule"):
                say(f"[rules] skipped \"{job['title']}\": {job['reason']}")
            say("\n== 3. LLM queue: the client message first, then one job draft at a time ==")
            while d.llm_tick():
                pass
            cards = [p for p in api.sent() if p.get("reply_markup") and "s:" in json.dumps(p["reply_markup"])]
            card = cards[0]
            job_id = card["reply_markup"]["inline_keyboard"][0][0]["callback_data"][2:]
            say(f"\n== 4. You sent the proposal on Upwork and press [✅ Sent] under card #{card['_id']} ==")
            api.push(callback_update(2, f"s:{job_id}", card["_id"]))
            d.tg_tick(timeout=0)
            say("\n== 5. ...and pick 8 Connects ==")
            api.push(callback_update(3, f"c:{job_id}:8", card["_id"]))
            d.tg_tick(timeout=0)
            offer = next(p for p in api.sent() if "Upwork · Offer" in p["text"])
            text = ("/note thanks for the offer, will accept today, can start monday. need read access to both "
                    "shop APIs before that. which products to map first?")
            say(f"\n== 6. You reply to the offer card #{offer['_id']} with a rough note ==")
            say(f"[you] {text}")
            api.push(message_update(4, text, message_id=50, reply_to=offer["_id"]))
            d.tg_tick(timeout=0)
            d.llm_tick()
            say("\n== 7. You: /stats ==")
            api.push(message_update(5, "/stats", message_id=51))
            d.tg_tick(timeout=0)
            say("\n== 8. 21:00 UTC: the daily summary ==")
            clock[0] = t0 + 3 * 3600
            d.schedule_tick()
            say(f"\n[end] Connects balance {store.balance()}; jobs by status {dict(sorted(store.counts().items()))}")
        finally:
            d.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0
