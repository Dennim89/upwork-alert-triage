"""IMAP reader (Gmail by default): Upwork messages of the last N days, strictly read-only (EXAMINE + BODY.PEEK).

Nothing is marked as read; dedupe is the store's job (Message-ID). Headers are fetched first and the caller's
`select(head)` decides which bodies to download, so known mail is never downloaded twice (Gmail caps daily
IMAP download volume). Reusable for any Upwork mail kind: pass a different `select`.
"""
from __future__ import annotations

import email
import email.policy
import email.utils
import hashlib
import imaplib
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.parser import BytesHeaderParser
from typing import Callable

from .config import LOOKBACK_DAYS, Config, redact

IMAP_PORT = 993
IMAP_TIMEOUT_S = 30
SENDER_WORD = "upwork"
MAX_HEADERS = 300          # newest matching UIDs inspected per run
MAX_BODIES = 25            # bodies downloaded per run, newest first
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_UID_RE = re.compile(rb"\bUID (\d+)")
_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?:"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.+?)\s*$')
_HEAD_ITEMS = "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID DATE SUBJECT FROM)])"

log = logging.getLogger(__name__)


class MailError(Exception):
    """IMAP failure; the text is already redacted."""


@dataclass
class MailItem:
    uid: str
    msgid: str
    date: datetime | None
    subject: str
    sender: str
    html: str | None
    text: str | None
    message: EmailMessage | None = field(default=None, repr=False)   # the raw parsed message
    raw: bytes | None = field(default=None, repr=False)             # original RFC822 bytes (event samples)


def imap_date(d: date | datetime) -> str:
    """IMAP SINCE format (01-Oct-2026), independent of the OS locale."""
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(str(value))
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def synthetic_msgid(*parts: object) -> str:
    h = hashlib.sha1("\x1f".join(str(p) for p in parts).encode("utf-8", "replace")).hexdigest()[:24]
    return f"<{h}@synthetic.local>"


def _header(msg, name: str) -> str:
    try:
        v = msg.get(name)
    except Exception:
        return ""
    return " ".join(str(v).split()) if v is not None else ""


def _text_part(msg: EmailMessage, subtype: str) -> str | None:
    try:
        part = msg.get_body(preferencelist=(subtype,))
    except Exception:
        part = None
    if part is None:
        part = next((p for p in msg.walk()
                     if p.get_content_type() == f"text/{subtype}" and not p.is_attachment()), None)
    if part is None:
        return None
    try:
        content = part.get_content()
        return content if isinstance(content, str) else None
    except Exception:
        payload = part.get_payload(decode=True) or b""
        try:
            return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")


def decode_message(raw: bytes, uid: str = "", fallback_msgid: str | None = None) -> MailItem:
    """Raw RFC822 bytes -> MailItem (html preferred by the parser, text/plain kept as fallback)."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    msgid = _header(msg, "Message-ID") or fallback_msgid or synthetic_msgid(hashlib.sha1(raw).hexdigest())
    return MailItem(uid=uid, msgid=msgid, date=parse_date(_header(msg, "Date")), subject=_header(msg, "Subject"),
                    sender=_header(msg, "From"), html=_text_part(msg, "html"), text=_text_part(msg, "plain"),
                    message=msg, raw=raw)


def _reason(e: BaseException) -> str:
    arg = e.args[0] if e.args else ""
    if isinstance(arg, bytes):
        return arg.decode("utf-8", "replace")
    return str(e) or e.__class__.__name__


def _connect(host: str) -> imaplib.IMAP4:
    return imaplib.IMAP4_SSL(host, IMAP_PORT, timeout=IMAP_TIMEOUT_S)


def _quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def pick_mailbox(conn: imaplib.IMAP4) -> str:
    """Gmail 'All Mail' (\\All flag, any UI language) so archived alerts count too; else INBOX (other servers)."""
    try:
        typ, data = conn.list()
    except (imaplib.IMAP4.error, OSError):
        return "INBOX"
    if typ != "OK":
        return "INBOX"
    for line in data or []:
        if isinstance(line, tuple):
            line = line[0]
        if not isinstance(line, bytes):
            continue
        m = _LIST_RE.match(line)
        if m and b"\\all" in m.group("flags").lower().split():
            name = m.group("name").decode("ascii", "replace")
            return name if name.startswith('"') else _quote(name)
    return "INBOX"


def _uid_payloads(data) -> list[tuple[str, bytes]]:
    """(uid, payload) pairs from a UID FETCH response; UID may sit before or after the literal."""
    out, pending = [], None
    for part in data or []:
        if isinstance(part, tuple) and len(part) >= 2:
            m = _UID_RE.search(part[0] or b"")
            if m:
                out.append((m.group(1).decode(), part[1] or b""))
                pending = None
            else:
                pending = part[1] or b""
        elif isinstance(part, bytes) and pending is not None:
            m = _UID_RE.search(part)
            if m:
                out.append((m.group(1).decode(), pending))
            pending = None
    return out


def _fetch_heads(conn: imaplib.IMAP4, uids: list[str]) -> dict[str, dict]:
    parser = BytesHeaderParser(policy=email.policy.default)
    heads: dict[str, dict] = {}
    for i in range(0, len(uids), 100):
        typ, data = conn.uid("FETCH", ",".join(uids[i:i + 100]), _HEAD_ITEMS)
        if typ != "OK":
            raise MailError("header fetch failed")
        for uid, payload in _uid_payloads(data):
            msg = parser.parsebytes(payload)
            d, subject, sender = _header(msg, "Date"), _header(msg, "Subject"), _header(msg, "From")
            msgid = _header(msg, "Message-ID") or synthetic_msgid(d, subject, sender)
            heads[uid] = {"uid": uid, "msgid": msgid, "date": parse_date(d), "subject": subject, "sender": sender}
    return heads


def _fetch_raw(conn: imaplib.IMAP4, uid: str) -> bytes | None:
    typ, data = conn.uid("FETCH", uid, "(BODY.PEEK[])")
    if typ != "OK":
        return None
    return next((p[1] for p in data or [] if isinstance(p, tuple) and len(p) >= 2 and p[1]), None)


def fetch_upwork(cfg: Config, *, select: Callable[[dict], bool] = lambda head: True, days: int = LOOKBACK_DAYS,
                 connect: Callable[[], imaplib.IMAP4] | None = None, now: datetime | None = None,
                 max_bodies: int = MAX_BODIES) -> list[MailItem]:
    """Upwork mail of the last `days` days whose header dict passes `select`, newest first. Read-only."""
    now = now or datetime.now(timezone.utc)
    secrets = cfg.secrets()
    try:
        conn = connect() if connect else _connect(cfg.imap_host)
    except (OSError, imaplib.IMAP4.error) as e:
        raise MailError(redact(f"connect failed: {_reason(e)}", secrets)) from None
    try:
        try:
            conn.login(cfg.imap_user, cfg.imap_password)
        except imaplib.IMAP4.error as e:
            raise MailError(redact(f"login failed: {_reason(e)}", secrets)) from None
        box = pick_mailbox(conn)
        typ, _ = conn.select(box, readonly=True)                       # EXAMINE: flags never change
        if typ != "OK":
            raise MailError(f"cannot open mailbox {box}")
        since = imap_date((now - timedelta(days=days)).date())
        typ, data = conn.uid("SEARCH", "FROM", f'"{SENDER_WORD}"', "SINCE", since)
        if typ != "OK":
            raise MailError("search failed")
        uids = sorted({u.decode() for u in (data[0] if data and data[0] else b"").split()}, key=int)[-MAX_HEADERS:]
        heads = _fetch_heads(conn, uids) if uids else {}
        todo = []
        for uid in reversed(uids):                                     # newest first
            h = heads.get(uid)
            if h is None or SENDER_WORD not in h["sender"].lower() or not select(h):
                continue
            todo.append(h)
            if len(todo) >= max_bodies:
                break
        items = []
        for h in todo:
            raw = _fetch_raw(conn, h["uid"])
            if raw is None:
                log.warning("imap: empty body for uid %s", h["uid"])
                continue
            try:
                items.append(decode_message(raw, h["uid"], fallback_msgid=h["msgid"]))
            except Exception as e:                                     # one bad mail never stops the run
                log.warning("imap: cannot decode uid %s: %s", h["uid"], e.__class__.__name__)
        log.info("imap: %s matched=%d selected=%d fetched=%d", box, len(uids), len(todo), len(items))
        return items
    except MailError:
        raise
    except (OSError, imaplib.IMAP4.error) as e:
        raise MailError(redact(f"imap error: {_reason(e)}", secrets)) from None
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def check_login(cfg: Config, connect: Callable[[], imaplib.IMAP4] | None = None) -> None:
    """Login + logout only (check_setup.py). Raises MailError with a redacted reason."""
    secrets = cfg.secrets()
    try:
        conn = connect() if connect else _connect(cfg.imap_host)
    except (OSError, imaplib.IMAP4.error) as e:
        raise MailError(redact(f"connect failed: {_reason(e)}", secrets)) from None
    try:
        conn.login(cfg.imap_user, cfg.imap_password)
    except imaplib.IMAP4.error as e:
        raise MailError(redact(f"login failed: {_reason(e)}", secrets)) from None
    except OSError as e:
        raise MailError(redact(f"imap error: {_reason(e)}", secrets)) from None
    finally:
        try:
            conn.logout()
        except Exception:
            pass
