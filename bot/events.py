"""Upwork e-mail events: one classification table (regex -> event), short hints, light extraction, cards.

Only job alerts were checked against real Upwork mail. The formats of the other notifications are educated
guesses, so: the rules live in one table, SAVE_EVENT_SAMPLES=1 keeps the first mails of every class (raw .eml,
in data/samples/events) for tuning, and any mail no rule recognises still reaches you as a short card
(subject + ~500 chars): nothing is dropped silently.
"""
from __future__ import annotations

import hashlib
import html
import logging
import re
from pathlib import Path

from . import parse
from .telegram import esc

log = logging.getLogger(__name__)

# (event, where, pattern): top-down, first match wins; 'subject' = mail subject, 'body' = visible text.
EVENT_RULES = (
    ("job_alert", "subject", r"^new job alert\b"),
    ("security", "subject", r"verification code|security code|one[- ]time (?:code|passcode)|\bpassword\b|"
                            r"\bsign[- ]?in\b|\blog[- ]?in\b|two[- ]step|\b2[- ]?(?:fa|step)\b|authenticat|"
                            r"unusual activity|new device"),
    ("digest", "subject", r"jobs? picked for you|jobs you might like|recommended jobs|top job picks|"
                          r"new jobs? (?:that )?match"),
    ("milestone_funded", "subject", r"\bfunded\b|\bfunding\b|\bescrow\b"),
    # your own billing / membership mails (receipts, card added, Plus, Connects): never client money
    ("account", "subject", r"payment received\s*-\s*thank you|thank you for your payment|\breceipt\b|\binvoice\b|"
                           r"\bwas added to\b|billing method|payment method|\bmembership\b|freelancer plus|"
                           r"\bconnects\b|\bsubscription\b|working style|profile is approved|"
                           r"account has been verified|security question"),
    ("payment", "subject", r"\b(?:milestone|work|submission|timesheet|hours)\b.*\bapproved\b|"
                           r"\bapproved\b.*\b(?:milestone|work|submission|payment)\b|\bpayment\b|"
                           r"\byou(?:'|’)?ve been paid\b|\bpaid you\b|\bfunds? (?:are |is )?(?:now )?available\b|"
                           r"\bearnings\b"),
    ("offer", "subject", r"^(?!.*(?:\d+\s*%|\bdiscount\b|\bsale\b|\bpromo)).*(?:\b(?:sent you an|received an|new|"
                         r"your) offer\b|\boffer (?:from|for|to)\b|^offer\b|\bhas offered\b)"),
    ("interview_invite", "subject", r"\binterview\b|\binvited you\b|\binvitation to\b"),
    ("contract_started", "subject", r"\bcontract\b.*\b(?:started|begun|began|is active)\b|"
                                    r"\bstarted (?:a|the|your) contract\b|\bhired\b"),
    ("proposal_viewed", "subject", r"\bviewed your proposal\b|\bproposal (?:was |has been )?viewed\b"),
    ("proposal_declined", "subject", r"\b(?:job|posting|proposal|position)\b.*\b(?:closed|declined|filled|"
                                     r"cancel+ed|withdrawn|expired)\b|\b(?:declined|withdrew)\b.*\bproposal\b|"
                                     r"\bno longer (?:available|accepting)\b|\bnot selected\b"),
    ("client_message", "subject", r"\bmessages?\b|\breplied\b|\bwrote\b"),
    ("client_message", "body", r"sent you (?:a |an )?(?:new )?message|you have (?:\d+ |an? )?(?:new |unread )+"
                               r"messages?|replied to your message"),
    ("proposal_viewed", "body", r"viewed your proposal"),
    ("interview_invite", "body", r"invited you to (?:an )?interview"),
    ("offer", "body", r"sent you an offer"),
    ("proposal_declined", "body", r"declined your proposal|job (?:has been|was) closed"),
)
_RULES = tuple((event, where, re.compile(p, re.I)) for event, where, p in EVENT_RULES)

LABELS = {
    "client_message": "Client message",
    "proposal_viewed": "Proposal viewed",
    "interview_invite": "Interview invitation",
    "offer": "Offer",
    "contract_started": "Contract started (hired)",
    "milestone_funded": "Milestone funded",
    "payment": "Milestone approved / payment",
    "proposal_declined": "Proposal declined / job closed",
    "digest": "Job digest",
    "security": "Security e-mail",
    "account": "Your account / membership",
    "other": "E-mail without a rule",
}
HINTS = {
    "client_message": "Open Upwork > Messages and send the client's text here: I will draft a reply.",
    "proposal_viewed": "The client opened your proposal. Nothing urgent: wait for a message and answer it quickly.",
    "interview_invite": "Answer within a day in Upwork > Messages: thank them and ask 1-2 clarifying questions. "
                        "Calls and payment stay on Upwork; do not share contact details.",
    "offer": "Before you accept, check: amount and type (fixed or hourly), milestones and their amounts, deadline, "
             "the scope you agreed on; that it is not a request to leave Upwork or to pay something yourself. "
             "All good: Accept.",
    "contract_started": "The contract has started. On fixed price, work only on a funded milestone; send the client "
                        "a short plan and the date of the first delivery.",
    "milestone_funded": "The milestone is funded: you can start the work.",
    "payment": "Milestone approved or payment released. The money becomes available after Upwork's security "
               "period; you can politely ask for a review.",
    "proposal_declined": "Proposal declined or job closed: nothing to do. Check your Connects history: when a job "
                         "closes without a hire, Upwork may return them.",
    "digest": "An Upwork digest. Cards with a draft come from job alerts only; open the e-mail if it looks useful.",
    "security": "A security e-mail (code or sign-in). The text is not forwarded: open your mailbox. If the sign-in "
                "was not you, change your password.",
    "account": "This is about your own account or membership (your payments, card, Plus, Connects), not a client. "
               "Nothing to do. If you did not do this, change your Upwork password now.",
    "other": "An Upwork e-mail type without a rule. Read it; if it matters, add a rule in bot/events.py.",
}
STATS_KINDS = (("proposal_viewed", "views"), ("interview_invite", "interviews"), ("offer", "offers"),
               ("contract_started", "hires"), ("client_message", "messages"), ("milestone_funded", "funded"),
               ("payment", "payments"))
SAMPLES_PER_CLASS = 3
SAMPLES_OTHER = 30
EXCERPT_CHARS = 500

_ACTION_RE = re.compile(r"^(?:view|open|reply|respond|accept|review|go to|see|check|read|join|start)\b", re.I)
_NOT_ACTION_RE = re.compile(r"unsubscribe|privacy|notification|settings|support|help|terms|app store|google play|"
                            r"download|facebook|linkedin|twitter|youtube|instagram|reddit|find work|log in", re.I)
_MSG_START_RE = re.compile(r"sent you (?:a |an )?(?:new )?message|\bwrote\s*:|\breplied\s*:|\bsays\s*:", re.I)
_MSG_END_RE = re.compile(r"^(?:view (?:message|messages|conversation)|reply\b|respond\b|go to (?:messages|inbox)|"
                         r"open (?:message|messages|conversation)|see (?:message|conversation))", re.I)
_NAV_RE = re.compile(r"^(?:(?:find work|freelancer plus|ad products|connect via mcp|log in|upwork)\s*)+$", re.I)
# a person or company: "Michael R.", "Sarah K.", "Acme Labs"; never a sentence opener like "The"/"Your"/"Milestone"
_NAME = (r"(?!(?:The|Your|You|Hi|Hello|Milestone|Payment|Job|Contract|Offer|Proposal|Upwork|New|A|An|Invitation|"
         r"Congratulations|Here|This|Our|Client)\b)[A-Z][\w'’-]*\.?(?:\s+[A-Z][\w'’-]*\.?){0,2}")
_NAME_END = r"(?=\s+(?:about|regarding|re|for|on|in|sent|has)\b|[,:;!?]?\s*$|[.,:;!?]\s)"
_MSG_FROM_RE = re.compile(r"\b[Mm]essages?\s+from\s+(?P<name>" + _NAME + r")" + _NAME_END)       # subjects
_FROM_RE = re.compile(r"\b(?:[Mm]essages?\s+from|[Ff]rom|[Ww]ith)\s+(?P<name>" + _NAME + r")" + _NAME_END)  # body
_ACTOR_RE = re.compile(r"^(?P<name>" + _NAME + r")\s+(?:sent|funded|approved|viewed|declined|invited|hired|started|"
                       r"wrote|replied|says|closed|accepted|wants)\b")
_TITLE_ABOUT_RE = re.compile(r"\b(?:about|regarding|re:)\s+(?:the\s+)?(?:job|contract|project|proposal|offer)?\s*"
                             r"[:\-–]?\s*[\"“']?(?P<title>[^\"”'\n]{3,140}?)[\"”']?\s*[.!]?$", re.I)
_SUBJ_COLON_RE = re.compile(r"^(?P<left>[^:]*\b(?:offer|interview|invitation|contract|proposal|job|messages?|"
                            r"funded)\b[^:]*):\s*(?P<title>[^:]{3,140})$", re.I)
_TITLE_LINE_RE = re.compile(r"^(?:job|contract|project)\s*[:\-–]\s*(?P<title>.{3,140})$", re.I)
_TITLE_END = r"[\"”]?(?=\s+(?:was|has|is|were)\b|[.!?](?:\s|$)|$)"
_BODY_TITLE_RES = (
    re.compile(r"\bthe job\s+[\"“]?(?P<title>[A-Z][^\"”\n]{2,120}?)" + _TITLE_END),
    re.compile(r"\b(?:for|on|about|regarding)\s+(?:the\s+)?(?:job\s+|contract\s+|project\s+)?[\"“]?"
               r"(?P<title>(?!Upwork\b)[A-Z][^\"”\n]{2,120}?)" + _TITLE_END),
)


def classify_event(subject: str | None, text: str | None = "") -> str:
    """Event class of one Upwork mail; 'other' when no rule matches."""
    s = parse._clean_line(subject or "")
    b = " ".join(parse._clean_line(ln) for ln in (text or "")[:6000].splitlines())
    for event, where, rx in _RULES:
        if rx.search(s if where == "subject" else b):
            return event
    return "other"


def mail_text(item) -> tuple[str, list[tuple[str, str]]]:
    """Visible text (html preferred, else text/plain without URLs) and (label, href) links of a mail item."""
    if item.html:
        flat, anchors = parse.html_to_text(item.html)
        return flat, [(parse._clean_line(flat[a["start"]:a["end"]]) or a["alt"], a["href"]) for a in anchors]
    if item.text:
        flat, anchors = parse.plain_anchors(item.text)
        return parse._URL_RE.sub(" ", flat), anchors
    return "", []


def clean_lines(text: str) -> list[str]:
    return [ln for ln in (parse._clean_line(x) for x in (text or "").splitlines()) if ln]


def excerpt(text: str, n: int = EXCERPT_CHARS) -> str:
    s = "\n".join(ln for ln in clean_lines(parse._cut_footer(text or "")) if not _NAV_RE.match(ln))
    if len(s) <= n:
        return s
    cut = s[:n]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > n * 0.7 else cut).rstrip() + "…"


def action_link(anchors: list[tuple[str, str]]) -> str | None:
    for label, href in anchors:
        if label and href.startswith("http") and _ACTION_RE.match(label) and not _NOT_ACTION_RE.search(label):
            return href
    return None


def _name(m: re.Match | None) -> str | None:
    if not m:
        return None
    n = m.group("name").strip()
    return n[:-1] if n.endswith(".") and len(n.split()[-1]) > 2 else n      # "OpenAI." -> "OpenAI", keep "R."


def who_and_what(subject: str, lines: list[str]) -> tuple[str | None, str | None]:
    """(client name, job/contract title): subject first, then the body lines; None when not found."""
    s = parse._clean_line(subject)
    name = _name(_MSG_FROM_RE.search(s) or _ACTOR_RE.search(s))
    m = _TITLE_ABOUT_RE.search(s) or _SUBJ_COLON_RE.search(s)
    title = parse._clean_title(m.group("title")) if m else None
    for i, ln in enumerate([s] + list(lines)):                  # the subject is a title source too
        if name is None and i > 0:
            name = _name(_ACTOR_RE.search(ln) or _FROM_RE.search(ln))
        if title is None:
            m = _TITLE_LINE_RE.match(ln) or next((r.search(ln) for r in _BODY_TITLE_RES if r.search(ln)), None)
            title = parse._clean_title(m.group("title")) if m else None
        if name and title:
            break
    return name, title


def extract_client_message(subject: str, text: str) -> dict:
    """Best effort {client, title, body}: body is the client's text when the mail carries it, else None."""
    lines = clean_lines(parse._cut_footer(text or ""))
    start = next((i for i, ln in enumerate(lines) if _MSG_START_RE.search(ln)), None)
    name, title = who_and_what(subject, lines[:start + 1] if start is not None else lines)
    if start is not None:
        tail = lines[start + 1:]
        title = title or next((parse._clean_title(m.group("title")) for m in map(_TITLE_LINE_RE.match, tail) if m), None)
    body = None
    if start is not None:
        head = _MSG_START_RE.split(lines[start], 1)[-1].strip(" :\"“”")
        collected = [head] if head else []
        for ln in lines[start + 1:]:
            if _MSG_END_RE.match(ln):
                break
            if _TITLE_LINE_RE.match(ln):
                continue
            collected.append(ln)
        text_ = "\n".join(collected).strip().strip("\"“”").strip()
        body = text_ if len(text_) >= 15 and len(text_.split()) >= 3 else None
    return {"client": name, "title": title, "body": body}


def build_event(kind: str, subject: str, text: str, anchors: list[tuple[str, str]]) -> dict:
    """Fields stored for an event; security mails keep no body (codes / login links never leave the mailbox)."""
    ev = {"client": None, "title": None, "body": None, "link": action_link(anchors)}
    if kind == "client_message":
        ev.update(extract_client_message(subject, text))
    elif kind != "security":
        if kind not in ("digest", "other"):                     # one client / one job only for known events
            ev["client"], ev["title"] = who_and_what(subject, clean_lines(parse._cut_footer(text or ""))[:12])
        ev["body"] = excerpt(text) or None
    return ev


def _head(ev: dict, label: str) -> list[str]:
    lines = [f"<b>Upwork · {esc(label)}</b>" + (f" · {esc(ev['client'])}" if ev.get("client") else "")]
    if ev.get("subject"):
        lines.append(f"Subject: {esc(ev['subject'][:200])}")
    if ev.get("title"):
        lines.append(f"Job: {esc(ev['title'])}")
    if ev.get("link"):
        lines.append(f'<a href="{html.escape(ev["link"], quote=True)}">Open in Upwork</a>')
    return lines


def event_card(ev: dict) -> str:
    """Card for an event that needs no LLM (everything except a client message with text)."""
    kind = ev.get("kind") or "other"
    lines = _head(ev, LABELS.get(kind, kind))
    if ev.get("body") and kind not in ("client_message", "security"):
        lines += ["", esc(ev["body"])]
    lines += ["", "What to do: " + esc(HINTS.get(kind, HINTS["other"]))]
    return "\n".join(lines)


def client_message_cards(ev: dict, res: dict | None, note: str | None = None) -> list[str]:
    """Client message with text: [card with summary (+ translation), English reply in <pre>] or a fallback card."""
    lines = _head(ev, LABELS["client_message"])
    if res:
        if res.get("translation"):
            lines += ["", "<b>Translation:</b>", esc(res["translation"])]
        else:
            lines += ["", "<b>Client wrote:</b>", esc((ev.get("body") or "")[:1500])]
        lines += ["", "<b>Suggested reply:</b> " + esc(res.get("summary") or "-"),
                  "What to do: this is a draft. Check it, edit it and send it yourself in Upwork > Messages."]
        return ["\n".join(lines)] + ([f"<pre>{esc(res['reply_en'])}</pre>"] if res.get("reply_en") else [])
    lines += ["", "<b>Client wrote:</b>", esc((ev.get("body") or "")[:1500]), "",
              "No draft" + (f": {esc(note)}." if note else "."),
              "Send this text to the bot later and it will draft a reply."]
    return ["\n".join(lines)]


def save_sample(raw: bytes | None, kind: str, samples_dir: Path | str, stamp: str) -> Path | None:
    """Keep the first mails of each class (raw .eml) for tuning the rules; never fails the run.
    Only called when SAVE_EVENT_SAMPLES=1: these files hold real e-mails, keep them private."""
    if not raw:
        return None
    cap = SAMPLES_OTHER if kind == "other" else SAMPLES_PER_CLASS
    folder = Path(samples_dir) / "events"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        if len(list(folder.glob(f"{kind}_*.eml"))) >= cap:
            return None
        path = folder / f"{kind}_{stamp}_{hashlib.sha1(raw).hexdigest()[:8]}.eml"
        if path.exists():
            return None
        path.write_bytes(raw)
        log.info("sample saved: %s", path.name)
        return path
    except OSError as e:
        log.warning("cannot save sample for %s: %s", kind, e.__class__.__name__)
        return None
