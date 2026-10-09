"""Telegram Bot API over urllib: owner binding (first private /start only), HTML-safe cards, <=4096-char chunks.

Reusable pieces: call(), get_updates()/poll() with offset bookkeeping, ensure_owner()/is_owner(),
send_message(), inline keyboards. Error texts never carry the token. Link previews are disabled for every message.
"""
from __future__ import annotations

import html
import json
import logging
import re
import time
import urllib.error
import urllib.request

from . import scoring
from .config import redact

API_BASE = "https://api.telegram.org"
MAX_LEN = 4096
HTTP_TIMEOUT_S = 20
CHECK_ON_PAGE = ("Check on the job page: proposals < 20, Interviewing < 5, the client's hire rate, the price in "
                 "Connects (the e-mail does not show them).")
VERDICT_TEXT = {"apply": "apply", "skip": "skip"}

log = logging.getLogger(__name__)


class TelegramError(Exception):
    def __init__(self, message: str, code: int | None = None, retry_after: int | None = None):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


# ---------- API ----------

def _error_payload(e: urllib.error.HTTPError) -> dict | None:
    try:
        data = json.loads(e.read().decode("utf-8", "replace"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def call(token: str, method: str, params: dict | None = None, *, timeout: float = HTTP_TIMEOUT_S, opener=None):
    """POST a Bot API method; returns `result` or raises TelegramError (token-free text)."""
    req = urllib.request.Request(f"{API_BASE}/bot{token}/{method}", data=json.dumps(params or {}).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with (opener or urllib.request.urlopen)(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        payload = _error_payload(e)
        if payload is None:
            raise TelegramError(redact(f"{method}: HTTP {e.code}", [token]), code=e.code) from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        detail = getattr(e, "reason", None) or e
        raise TelegramError(redact(f"{method}: {e.__class__.__name__}: {detail}", [token])) from None
    if not isinstance(payload, dict) or not payload.get("ok"):
        p = payload if isinstance(payload, dict) else {}
        raise TelegramError(redact(f"{method}: {p.get('error_code')} {p.get('description')}", [token]),
                            code=p.get("error_code"), retry_after=(p.get("parameters") or {}).get("retry_after"))
    return payload.get("result")


def get_me(token: str, opener=None) -> dict:
    return call(token, "getMe", opener=opener) or {}


def get_updates(token: str, offset: int | None = None, timeout: int = 0, allowed_updates: list[str] | None = None,
                opener=None) -> list[dict]:
    params: dict = {"timeout": int(timeout)}
    if offset is not None:
        params["offset"] = int(offset)
    if allowed_updates is not None:
        params["allowed_updates"] = list(allowed_updates)
    return call(token, "getUpdates", params, timeout=HTTP_TIMEOUT_S + int(timeout), opener=opener) or []


def poll(token: str, state: dict, timeout: int = 0, allowed_updates: list[str] | None = None, opener=None) -> list[dict]:
    """getUpdates from state['update_offset'] and advance it past what came back (the caller saves state)."""
    updates = get_updates(token, offset=state.get("update_offset"), timeout=timeout,
                          allowed_updates=allowed_updates, opener=opener)
    ids = [u["update_id"] for u in updates if isinstance(u.get("update_id"), int)]
    if ids:
        state["update_offset"] = max(ids) + 1
    return sorted(updates, key=lambda u: u.get("update_id", 0))


def find_owner(updates: list[dict], pinned: int | None = None) -> dict | None:
    """The first private chat that sent /start (only chat `pinned` when given); everything else is ignored."""
    for u in updates:
        msg = u.get("message") or {}
        chat = msg.get("chat") or {}
        text = (msg.get("text") or "").strip()
        if chat.get("type") == "private" and "id" in chat and re.match(r"^/start(?:@\w+)?(?:\s|$)", text):
            if pinned is not None and chat["id"] != pinned:
                continue
            return {"chat_id": chat["id"], "name": chat.get("username") or chat.get("first_name") or ""}
    return None


def ensure_owner(token: str, state: dict, opener=None) -> int | None:
    """Owner chat id from state, else bind the first private /start (once; later /start from others ignored)."""
    if state.get("owner_chat_id") is not None:
        return int(state["owner_chat_id"])
    owner = find_owner(poll(token, state, opener=opener))
    if owner is None:
        return None
    state["owner_chat_id"], state["owner_name"] = owner["chat_id"], owner["name"]
    return int(owner["chat_id"])


def update_chat_id(update: dict) -> int | None:
    for key in ("message", "edited_message", "callback_query"):
        obj = update.get(key)
        if isinstance(obj, dict):
            msg = obj.get("message") if key == "callback_query" else obj
            chat = (msg or {}).get("chat") or {}
            if "id" in chat:
                return chat["id"]
    return None


def is_owner(update: dict, state: dict) -> bool:
    owner = state.get("owner_chat_id")
    return owner is not None and update_chat_id(update) == int(owner)


# ---------- text: escaping and splitting ----------

def esc(s: object) -> str:
    return html.escape("" if s is None else str(s), quote=False)


def _u16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2                      # Telegram counts UTF-16 units


_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)\b[^>]*>")
_TOKEN_RE = re.compile(r"<[^>]*>|&#?\w+;|\s+|[^<&\s]+")


def _apply_tags(stack: list[tuple[str, str]], piece: str) -> list[tuple[str, str]]:
    stack = list(stack)
    for m in _TAG_RE.finditer(piece):
        name = m.group(2).lower()
        if m.group(1):
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0] == name:
                    del stack[i]
                    break
        else:
            stack.append((name, m.group(0)))
    return stack


def _prefix_u16(s: str, n: int) -> int:
    """Length (in code points) of the longest prefix of s that fits n UTF-16 units."""
    used = 0
    for i, ch in enumerate(s):
        used += 2 if ord(ch) > 0xFFFF else 1
        if used > n:
            return max(i, 1)
    return len(s)


def _pieces(text: str, maxlen: int):
    """Lines; an overlong line is cut between tags/entities/words (never inside one)."""
    for line in text.split("\n"):
        if _u16(line) <= maxlen:
            yield line
            continue
        cur = ""
        for tok in _TOKEN_RE.findall(line):
            while _u16(tok) > maxlen:                           # one giant word: plain text, safe to cut
                if cur:
                    yield cur
                    cur = ""
                k = _prefix_u16(tok, maxlen)
                yield tok[:k]
                tok = tok[k:]
            if _u16(cur + tok) > maxlen:
                yield cur
                cur = tok
            else:
                cur += tok
        if cur:
            yield cur


def split_html(text: str, limit: int = MAX_LEN) -> list[str]:
    """Chunks <= limit; tags open at a cut are closed and re-opened in the next chunk."""
    if _u16(text) <= limit:
        return [text]
    out: list[str] = []
    buf, stack = "", []

    def closing(st):
        return "".join(f"</{n}>" for n, _ in reversed(st))

    for piece in _pieces(text, limit // 2):
        new_stack = _apply_tags(stack, piece)
        if buf and _u16(buf + "\n" + piece + closing(new_stack)) > limit:
            out.append(buf + closing(stack))
            buf = "".join(t for _, t in stack) + piece
        else:
            buf = f"{buf}\n{piece}" if buf else "".join(t for _, t in stack) + piece
        stack = new_stack
    if buf:
        out.append(buf + closing(stack))
    return out


def html_to_plain(text: str) -> str:
    """Fallback when Telegram rejects our HTML: links become 'label: url', tags dropped, entities decoded."""
    text = re.sub(r'<a\s+href="([^"]*)"[^>]*>(.*?)</a>', lambda m: f"{m.group(2)}: {html.unescape(m.group(1))}",
                  text, flags=re.S | re.I)
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def send_message(token: str, chat_id: int | str, text: str, parse_mode: str | None = "HTML", *, opener=None,
                 sleep=time.sleep, reply_markup: dict | None = None, reply_to: int | None = None) -> list[int]:
    """Send text (split <= 4096); one bounded 429 wait; one plain-text retry if Telegram rejects the HTML (400).

    reply_markup (inline keyboard) goes on the last chunk, reply_to (thread to a message) on the first.
    """
    ids = []
    chunks = split_html(text)
    for i, chunk in enumerate(chunks):
        params: dict = {"chat_id": chat_id, "text": chunk, "link_preview_options": {"is_disabled": True}}
        if parse_mode:
            params["parse_mode"] = parse_mode
        if reply_to and i == 0:
            params["reply_parameters"] = {"message_id": int(reply_to), "allow_sending_without_reply": True}
        if reply_markup and i == len(chunks) - 1:
            params["reply_markup"] = reply_markup
        try:
            res = call(token, "sendMessage", params, opener=opener)
        except TelegramError as e:
            if e.code == 429 and e.retry_after and e.retry_after <= 30:
                sleep(e.retry_after)
                res = call(token, "sendMessage", params, opener=opener)
            elif e.code == 400 and parse_mode:                  # bad entities / rejected long link: plain text
                log.warning("telegram: HTML rejected (%s); resending as plain text", e)
                params.pop("parse_mode")
                params["text"] = html_to_plain(chunk)
                res = call(token, "sendMessage", params, opener=opener)
            else:
                raise
        ids.append((res or {}).get("message_id"))
    return ids


def inline_keyboard(rows: list[list[tuple[str, str]]]) -> dict:
    """[[(text, callback_data), ...], ...] -> reply_markup. callback_data must stay <= 64 bytes."""
    for row in rows:
        for _, data in row:
            if len(data.encode("utf-8")) > 64:
                raise ValueError(f"callback_data longer than 64 bytes: {data!r}")
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


def edit_reply_markup(token: str, chat_id: int, message_id: int, reply_markup: dict | None, opener=None):
    """Swap a message's inline keyboard; 'message is not modified' is not an error."""
    params = {"chat_id": chat_id, "message_id": message_id, "reply_markup": reply_markup or {"inline_keyboard": []}}
    try:
        return call(token, "editMessageReplyMarkup", params, opener=opener)
    except TelegramError as e:
        if e.code == 400 and "not modified" in str(e).lower():
            return None
        raise


def answer_callback(token: str, callback_id: str, text: str | None = None, opener=None):
    params: dict = {"callback_query_id": callback_id}
    if text:
        params["text"] = text[:200]
    return call(token, "answerCallbackQuery", params, opener=opener)


def send_chat_action(token: str, chat_id: int, action: str = "typing", opener=None):
    return call(token, "sendChatAction", {"chat_id": chat_id, "action": action}, opener=opener)


class TelegramClient:
    """Token + transport bundle for the daemon (opener is injectable: tests and --simulate never touch the network)."""

    def __init__(self, token: str, opener=None, sleep=time.sleep):
        self.token, self.opener, self.sleep = token, opener, sleep

    def get_updates(self, offset: int | None = None, timeout: int = 0, allowed_updates: list[str] | None = None):
        return get_updates(self.token, offset=offset, timeout=timeout, allowed_updates=allowed_updates,
                           opener=self.opener)

    def send(self, chat_id: int, text: str, *, reply_markup: dict | None = None, reply_to: int | None = None,
             parse_mode: str | None = "HTML") -> list[int]:
        return send_message(self.token, chat_id, text, parse_mode, opener=self.opener, sleep=self.sleep,
                            reply_markup=reply_markup, reply_to=reply_to)

    def edit_markup(self, chat_id: int, message_id: int, reply_markup: dict | None):
        return edit_reply_markup(self.token, chat_id, message_id, reply_markup, opener=self.opener)

    def answer(self, callback_id: str, text: str | None = None):
        return answer_callback(self.token, callback_id, text, opener=self.opener)

    def chat_action(self, chat_id: int, action: str = "typing"):
        return send_chat_action(self.token, chat_id, action, opener=self.opener)

    def get_me(self) -> dict:
        return get_me(self.token, opener=self.opener)


# ---------- job card ----------

def _money(x: float) -> str:
    return f"{x:,.0f}" if float(x).is_integer() else f"{x:,.2f}"


def _short(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def budget_text(f: dict) -> str:
    bt = f.get("budget_type")
    if bt == "hourly":
        lo, hi = f.get("hourly_min"), f.get("hourly_max")
        if lo is not None and hi is not None and lo != hi:
            return f"hourly ${_money(lo)}-${_money(hi)}/h"
        return f"hourly ${_money(hi)}/h" if hi is not None else "hourly (rate not given)"
    if bt == "fixed":
        b = f.get("fixed_budget")
        return f"fixed ${_money(b)}" if b is not None else "fixed (amount not given)"
    return "not given"


def client_text(f: dict) -> str:
    parts = []
    if f.get("client_country"):
        parts.append(f["client_country"])
    if f.get("client_rating") is not None:
        parts.append(f"rating {f['client_rating']:g}")
    if f.get("client_spend"):
        parts.append(f"spent {f['client_spend']}")
    if f.get("payment_verified") is True:
        parts.append("payment verified")
    elif f.get("payment_verified") is False:
        parts.append("payment NOT verified")
    return " · ".join(parts) or (f.get("client") or "")


def format_card(job: dict, draft: dict | None = None, note: str | None = None) -> str:
    """First message: what the job is and what the draft says."""
    f = job.get("fields") or {}
    lines = [f"<b>{esc(_short(job.get('title') or 'Untitled job', 250))}</b>"]
    url = job.get("url") or ""
    lines.append(f'<a href="{html.escape(url, quote=True)}">Open the job</a>' if url else "No link in the e-mail")
    if f.get("query"):
        lines.append(f"Search: {esc(_short(f['query'], 160))}")
    lines.append(f"Terms: {esc(f['terms'])}" if f.get("terms") else f"Budget: {esc(budget_text(f))}")
    if f.get("terms") and f.get("budget_type") and f.get("hourly_max") is None and f.get("fixed_budget") is None:
        lines.append(f"Budget: {esc(budget_text(f))}")              # e.g. "Hourly: $0-$0" = rate left blank
    if f.get("experience") and f["experience"].lower() not in (f.get("terms") or "").lower():
        lines.append(f"Level: {esc(f['experience'])}")
    client = client_text(f)
    if client:
        lines.append(f"Client: {esc(client)}")
    if f.get("skills"):
        lines.append(f"Skills: {esc(', '.join(f['skills']))}")
    if f.get("posted"):
        lines.append(f"Posted: {esc(f['posted'])}")
    lines.append(f"Proposals: {esc(f['proposals'])}" if f.get("proposals") else esc(CHECK_ON_PAGE))
    if job.get("fit_tags"):
        lines.append(f"Niche: {esc(', '.join(job['fit_tags']))}")
    lines.append("")
    if not draft:
        lines.append("No draft" + (f": {esc(note)}." if note else "."))
        return "\n".join(lines)
    if draft.get("fit") is not None and draft.get("win") is not None:
        lines.append(f"Score: {esc(scoring.label(draft))}")
        if draft.get("red_flag") and draft.get("red_flag_reason"):
            lines.append(f"⛔ {esc(draft['red_flag_reason'])}")
    else:
        score = draft.get("score")
        lines.append(f"Score: {'-' if score is None else f'{score:g}'}/10 · "
                     f"{VERDICT_TEXT.get(draft.get('verdict'), '-')}")
    if draft.get("why"):
        lines.append(f"Why: {esc(draft['why'])}")
    if draft.get("risks"):
        lines.append(f"Risks: {esc(draft['risks'])}")
    if draft.get("price_usd"):
        hourly = f.get("budget_type") == "hourly"
        lines.append(f"{'Rate' if hourly else 'Price'}: ${_money(draft['price_usd'])}{'/h' if hourly else ''}")
    if draft.get("milestones"):
        lines.append("Milestones: " + "; ".join(f"{i}) {esc(m['name'])}: ${_money(m['usd'])}"
                                                for i, m in enumerate(draft["milestones"], 1)))
    if draft.get("warnings"):
        lines.append("Check the text: " + esc("; ".join(draft["warnings"])))
    lines.append("The proposal follows in the next message (tap it to copy)."
                 if draft.get("proposal_en") else "The proposal text is empty.")
    return "\n".join(lines)


def format_proposal(draft: dict) -> str:
    """Second message: only the proposal, in <pre> so one tap copies it."""
    return f"<pre>{esc(draft.get('proposal_en') or '')}</pre>"
