"""Upwork emails -> jobs. Pure functions, tolerant of layout drift; nothing here touches the network.

* classify(subject): 'job_alert', 'digest' (multi-job mails), 'other'.
* parse_job_alert: Upwork "New job alert: <title>" mails, one job each. The layout handled here is the one
  Upwork alerts had in 2026 (synthetic look-alikes in tests/fixtures). Every link in them is an opaque click-tracking
  redirect, so the id is derived from Message-ID + title and the card link is the "View job details" href
  (never resolved here).
* parse_links: generic multi-job layout keyed by real upwork.com/jobs/...~<id> links; unwraps tracking
  redirects whose target sits in a query/path parameter (percent-encoded or base64). Used for digests.
When Upwork changes the layout: add a sample to tests/fixtures, fix the parser and bump PARSER_VERSION so
recent mail is parsed again.
"""
from __future__ import annotations

import base64
import binascii
import bisect
import hashlib
import html as htmllib
import re
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from urllib.parse import unquote

PARSER_VERSION = 1
SNIPPET_MAX = 1500
TITLE_MAX = 200
JOB_URL = "https://www.upwork.com/jobs/~{}"
_INVISIBLE = dict.fromkeys(map(ord, "\u034f\u00ad\u180e\u200b\u200c\u200d\u200e\u200f\u2060\u2061\u2062\u2063\ufeff"))


@dataclass
class Job:
    id: str
    url: str
    title: str
    snippet: str = ""                    # description text near the job (<= SNIPPET_MAX)
    query: str | None = None             # saved search that matched (alerts)
    terms: str | None = None             # meta line, e.g. "Hourly: $30-$45 • 1 to 3 months • Less than 30 hrs/week"
    budget_type: str | None = None       # "hourly" | "fixed"
    hourly_min: float | None = None
    hourly_max: float | None = None
    fixed_budget: float | None = None
    experience: str | None = None        # Entry | Intermediate | Expert
    posted: str | None = None
    client: str | None = None            # raw client line, e.g. "Payment verified • 4.8 • $3k spent • Canada"
    client_country: str | None = None
    client_spend: str | None = None
    client_rating: float | None = None
    payment_verified: bool | None = None
    proposals: str | None = None
    skills: list[str] | None = None

    def fields(self) -> dict:
        d = asdict(self)
        for k in ("id", "url", "title", "snippet"):
            d.pop(k)
        return {k: v for k, v in d.items() if v is not None and v != [] and v != ""}

    def text(self) -> str:
        """Everything a human would read about the job (input for the rules)."""
        return "\n".join(p for p in (self.title, self.snippet, self.terms, self.client,
                                     ", ".join(self.skills or [])) if p)

    def fingerprint(self) -> str | None:
        """Content hash (title + description) to catch one job arriving via two alerts; None without a description."""
        body = " ".join(self.snippet.split()).casefold()[:400]
        if not body:
            return None
        return hashlib.sha1(f"{self.title.casefold()}|{body}".encode("utf-8")).hexdigest()[:20]


def classify(subject: str | None) -> str:
    """'job_alert' (one job per mail), 'digest' (several jobs), 'other' (not a job mail)."""
    s = " ".join(str(subject or "").split()).lower()
    if s.startswith("new job alert"):
        return "job_alert"
    if "jobs picked for you" in s:
        return "digest"
    return "other"


def parse_email(subject: str | None, html: str | None, text: str | None, msgid: str = "") -> tuple[list[Job], dict]:
    """Dispatch by subject; returns (jobs, diagnostics)."""
    kind = classify(subject)
    if kind == "job_alert":
        jobs = parse_job_alert(subject, html, text, msgid)
        if jobs:
            return jobs, {"kind": kind, "parser": "alert", "jobs": len(jobs)}
        jobs = parse_links(html, text)          # layout drift: maybe real job links appeared
        return jobs, {"kind": kind, "parser": "links", "jobs": len(jobs)}
    if kind == "digest":
        jobs = parse_links(html, text)          # digests with direct job links only
        return jobs, {"kind": kind, "parser": "links", "jobs": len(jobs)}
    return [], {"kind": kind, "parser": None, "jobs": 0}


# ---------- small text helpers ----------

def _clean_line(s: str) -> str:
    return " ".join(str(s).translate(_INVISIBLE).split())


_GENERIC_WORDS = frozenset(
    "a an the to on in of for this it here now more all view see open apply submit read learn check click show "
    "details detail job jobs link url go out proposal proposals full post posting description upwork new find "
    "work similar".split())


def _is_generic(label: str) -> bool:
    s = (label or "").strip()
    if len(s) < 3 or re.match(r"(?:https?://|www\.)", s, re.I):
        return True
    words = re.findall(r"[^\W\d_]+", s.lower())
    return not words or all(w in _GENERIC_WORDS for w in words)


def _clean_title(s: str) -> str:
    s = _clean_line(s).strip(" -–—:|»›→•·*\"'")
    while s and s[-1] in "([<{":
        s = s[:-1].rstrip(" -–—:|»›→•·*")
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1].strip()
    return s[:TITLE_MAX].rstrip()


def _cap(s: str, n: int = SNIPPET_MAX) -> str:
    if len(s) <= n:
        return s
    cut = s[:n]
    sp = cut.rfind(" ")
    if sp > n * 0.8:
        cut = cut[:sp]
    return cut.rstrip() + "…"


def _tidy(text: str) -> str:
    """Normalise lines, drop separator lines and generic link labels, keep single blank lines."""
    out: list[str] = []
    for raw in text.splitlines():
        line = _clean_line(raw)
        letters = re.findall(r"[^\W\d_]+", line.lower())
        if not any(ch.isalnum() for ch in line) or (
                letters and len(line) < 40 and all(w in _GENERIC_WORDS for w in letters)):
            if out and out[-1]:
                out.append("")
            continue
        out.append(line)
    return "\n".join(out).strip()


_FOOTER_RE = re.compile(
    r"^[^\n]*\b(?:unsubscribe|email\s+preferences|notification\s+settings|manage\s+(?:your\s+)?(?:email|notification)s?"
    r"|you(?:'|’)?re\s+receiving|you\s+are\s+receiving|you\s+received\s+this|this\s+email\s+was\s+sent"
    r"|privacy\s+policy)\b|^\s*(?:©|\(c\)\s*\d{4}|copyright\b)", re.I | re.M)


def _cut_footer(region: str) -> str:
    m = _FOOTER_RE.search(region)
    return region[:m.start()] if m else region


# ---------- field heuristics ----------

_N = r"(\d[\d,]*(?:\.\d+)?)\s*([kK](?![a-z]))?"
_TO = r"\s*(?:-|–|—|to)\s*"
_PER_HOUR = r"\s*(?:/\s*h(?:ou)?r\b|per\s+hour\b|an\s+hour\b)"
_BUDGET_RES = (          # the earliest match in the text wins; ties go to the earlier (more specific) pattern
    ("hourly_range", re.compile(r"\bhourly(?:\s+rate)?(?:\s+range)?\s*[:(\-–—]?\s*\$\s?" + _N + _TO + r"\$?\s?" + _N, re.I)),
    ("hourly_range", re.compile(r"\$\s?" + _N + _TO + r"\$?\s?" + _N + _PER_HOUR, re.I)),
    ("hourly_one", re.compile(r"\bhourly(?:\s+rate)?\s*[:(\-–—]?\s*\$\s?" + _N, re.I)),
    ("hourly_one", re.compile(r"\$\s?" + _N + _PER_HOUR, re.I)),
    ("fixed_amount", re.compile(r"\bfixed[\s-]*price\b[^$\n]{0,40}?\$\s?" + _N, re.I)),
    ("fixed_amount", re.compile(r"\b(?:est(?:imated)?\.?\s*)?budget\s*[:\-–—]?\s*\$\s?" + _N, re.I)),
    ("hourly", re.compile(r"\bhourly\b", re.I)),
    ("fixed", re.compile(r"\bfixed[\s-]*price\b|\bfixed\s+budget\b", re.I)),
)
_META_RE = re.compile(r"\$\s?\d|\bpayment\b|\bposted\b|\bhourly\b|\bfixed[\s-]*price\b|\bbudget\b|\bproposals?\b"
                      r"|\bspent\b|\s\|\s|•", re.I)
_EXP_LABEL_RE = re.compile(r"(?:experience(?:\s+level)?|level)\s*[:\-–—]\s*(entry(?:[\s-]*level)?|intermediate|expert)\b", re.I)
_EXP_RE = re.compile(r"\b(Entry[\s-]*[Ll]evel|Intermediate|Expert)\b")
_POSTED_RE = re.compile(
    r"\bposted\s*(?:on\s*)?:?\s*((?:\d+|an?|a few)\s+(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|months?)"
    r"\s+ago|recently|just now|yesterday|today|[a-z]{3,9}\.?\s+\d{1,2}(?:,\s*\d{4})?|\d{4}-\d{2}-\d{2})", re.I)
_PROPOSALS_RE = re.compile(r"\bproposals?\s*[:\-–—]\s*((?:less than|fewer than|more than|over)\s+\d+"
                           r"|\d+\s*(?:to|-|–)\s*\d+|\d+\+?)", re.I)
_SPENT_RE = re.compile(r"(\$\s?\d[\d,.]*\s*[KkMm]?\+?)\s*(?:total\s+)?spent"
                       r"|\bspent\s*[:\-–—]?\s*(\$\s?\d[\d,.]*\s*[KkMm]?\+?)", re.I)
_PAY_RE = re.compile(r"\bpayment(?:\s+method)?\s+(?:is\s+)?(not\s+verified|unverified|verified)\b", re.I)
_RATING_RE = re.compile(r"^(?:★+\s*)?(\d(?:\.\d{1,2})?)(?:\s*(?:/\s*5|stars?))?$")
_CLIENT_HINT_RE = re.compile(r"\bpayment\b|\bspent\b|\bclient\b|\blocation\b|\bcountry\b|★|\breviews?\b"
                             r"|\bhire rate\b|\bmember since\b", re.I)
_COUNTRY_LABEL_RE = re.compile(r"(?i:\b(?:client\s+)?(?:location|country))\s*[:\-–—]\s*([A-Z][A-Za-z .'\-]{1,40}?)"
                               r"\s*(?:[|•·,\n]|$)", re.M)
_NOT_COUNTRY_RE = re.compile(r"\b(?:payment|verified|unverified|spent|reviews?|rating|hire|member|jobs?|posted|"
                             r"client|hourly|fixed|budget|proposals?)\b", re.I)
_COUNTRIES = (
    "United States", "USA", "United Kingdom", "UK", "Canada", "Australia", "New Zealand", "Ireland", "Germany",
    "France", "Netherlands", "Belgium", "Switzerland", "Austria", "Sweden", "Norway", "Denmark", "Finland",
    "Spain", "Portugal", "Italy", "Greece", "Cyprus", "Malta", "Poland", "Czech Republic", "Slovakia", "Hungary",
    "Romania", "Bulgaria", "Serbia", "Croatia", "Slovenia", "Estonia", "Latvia", "Lithuania", "Ukraine",
    "Georgia", "Armenia", "Kazakhstan", "Turkey", "Türkiye", "Israel", "United Arab Emirates", "UAE",
    "Saudi Arabia", "Qatar", "Kuwait", "Bahrain", "Oman", "Egypt", "Morocco", "Nigeria", "Kenya", "South Africa",
    "India", "Pakistan", "Bangladesh", "Sri Lanka", "Nepal", "Singapore", "Malaysia", "Indonesia", "Philippines",
    "Thailand", "Vietnam", "Hong Kong", "Taiwan", "Japan", "South Korea", "China", "Mexico", "Brazil",
    "Argentina", "Chile", "Colombia", "Peru", "Uruguay", "Puerto Rico", "Luxembourg", "Iceland")
_COUNTRY_RE = re.compile(r"\b(" + "|".join(sorted(map(re.escape, _COUNTRIES), key=len, reverse=True)) + r")\b")
_COUNTRY_ALIASES = {"USA": "United States", "UK": "United Kingdom", "UAE": "United Arab Emirates"}


def _amt(m: re.Match, i: int) -> float:
    v = float(m.group(i).replace(",", ""))
    return v * 1000 if m.group(i + 1) else v


def budget_fields(text: str) -> dict:
    """budget_type / hourly_min / hourly_max / fixed_budget from labelled patterns only.

    Upwork prints "Hourly: $0-$0" / "$0" when the client left the amount blank: type known, amount unknown.
    """
    best = None
    for idx, (kind, rx) in enumerate(_BUDGET_RES):
        m = rx.search(text or "")
        if m and (best is None or (m.start(), idx) < (best[0], best[1])):
            best = (m.start(), idx, kind, m)
    if best is None:
        return {}
    kind, m = best[2], best[3]
    if kind == "hourly_range":
        lo, hi = _amt(m, 1), _amt(m, 3)
        if max(lo, hi) <= 0:
            return {"budget_type": "hourly"}
        return {"budget_type": "hourly", "hourly_min": min(lo, hi), "hourly_max": max(lo, hi)}
    if kind == "hourly_one":
        v = _amt(m, 1)
        return {"budget_type": "hourly", "hourly_min": v, "hourly_max": v} if v > 0 else {"budget_type": "hourly"}
    if kind == "fixed_amount":
        v = _amt(m, 1)
        return {"budget_type": "fixed", "fixed_budget": v} if v > 0 else {"budget_type": "fixed"}
    return {"budget_type": kind}


def _experience(text: str) -> str | None:
    m = _EXP_LABEL_RE.search(text or "")
    if not m:
        for line in (text or "").splitlines():
            if _META_RE.search(line):
                m = _EXP_RE.search(line)
                if m:
                    break
    if not m:
        return None
    v = m.group(1).lower()
    return "Entry" if v.startswith("entry") else v.capitalize()


def _is_country_like(seg: str) -> bool:
    return bool(re.fullmatch(r"[A-Z][A-Za-z.'\- ]{1,40}", seg)) and not _NOT_COUNTRY_RE.search(seg)


def client_fields(line: str) -> dict:
    """From a client line like 'Payment verified • 4.8 • $3k spent • Canada'."""
    out: dict = {}
    line = line or ""
    m = _PAY_RE.search(line)
    if m:
        out["payment_verified"] = m.group(1).lower() == "verified"
    m = _SPENT_RE.search(line)
    if m:
        out["client_spend"] = "".join((m.group(1) or m.group(2)).split())
    segs = [s.strip() for s in re.split(r"[•|·]", line) if s.strip()]
    for s in segs:
        r = _RATING_RE.match(s)
        if r and 0 <= float(r.group(1)) <= 5:
            out["client_rating"] = float(r.group(1))
            break
    m = _COUNTRY_LABEL_RE.search(line)
    if m:
        out["client_country"] = m.group(1).strip()
    else:
        seg = next((s for s in reversed(segs) if _is_country_like(s)), None) if len(segs) > 1 else None
        if seg is None:
            mc = _COUNTRY_RE.search(line)
            seg = _COUNTRY_ALIASES.get(mc.group(1), mc.group(1)) if mc else None
        if seg:
            out["client_country"] = seg
    return out


def extract_fields(text: str) -> dict:
    """Heuristic fields for the generic parser (terms line, budget, level, posted, proposals, client)."""
    text = text or ""
    out = budget_fields(text)
    if out:
        for line in text.splitlines():
            if _BUDGET_LINE_RE.search(line):
                out["terms"] = _clean_line(line)[:200]
                break
    exp = _experience(text)
    if exp:
        out["experience"] = exp
    m = _POSTED_RE.search(text)
    if m:
        out["posted"] = _clean_line(m.group(1))
    m = _PROPOSALS_RE.search(text)
    if m:
        out["proposals"] = _clean_line(m.group(1))
    line = next((ln for ln in text.splitlines() if _CLIENT_HINT_RE.search(ln)), None)
    if line:
        out["client"] = _clean_line(line)[:200]
        out.update(client_fields(_clean_line(line)))
    return out


_BUDGET_LINE_RE = re.compile(r"\bhourly\b|\bfixed[\s-]*price\b|\bbudget\b|\$\s?\d[\d,.]*\s*(?:-|–|to)\s*\$", re.I)


# ---------- html -> text with anchor spans ----------

_BLOCK = frozenset("address article aside blockquote center dd div dl dt fieldset figcaption figure footer form "
                   "h1 h2 h3 h4 h5 h6 header hr li main nav ol p pre section table tbody tfoot thead tr ul".split())
_PARA = frozenset("blockquote h1 h2 h3 h4 h5 h6 hr ol p table ul".split())
_CELL = frozenset(("td", "th"))
_SKIP = frozenset("head script style title noscript template".split())
_VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())
_HIDDEN_STYLE_RE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden|mso-hide\s*:\s*all", re.I)


class _Flattener(HTMLParser):
    """HTML -> text (blocks become line breaks; hidden, script, style dropped) + <a> spans within that text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.n = 0
        self.nl = 0
        self.sp = False
        self.stack: list[tuple[str, bool, dict | None]] = []
        self.hidden = 0
        self.anchors: list[dict] = []
        self.cur: dict | None = None

    def _emit(self, s: str) -> None:
        self.parts.append(s)
        self.n += len(s)

    def _break(self, n: int) -> None:
        if self.n and not self.hidden:
            self.nl = min(2, max(self.nl, n))

    def handle_data(self, data: str) -> None:
        if self.hidden:
            return
        raw = data.translate(_INVISIBLE)
        s = " ".join(raw.split())
        if not s:
            if raw and self.n:
                self.sp = True
            return
        if self.n:
            if self.nl:
                self._emit("\n" * self.nl)
            elif self.sp or raw[:1].isspace():
                self._emit(" ")
        self.nl, self.sp = 0, False
        if self.cur is not None and self.cur["start"] is None:
            self.cur["start"] = self.n
        self._emit(s)
        self.sp = raw[-1:].isspace()

    def handle_starttag(self, tag: str, attrs) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "body" and any(t == "head" for t, _, _ in self.stack):
            self._close("head")
        if tag == "br":
            if self.n and not self.hidden:
                self.nl = min(2, self.nl + 1)
            return
        if tag == "img":
            if self.cur is not None and not self.hidden and not self.cur["alt"]:
                self.cur["alt"] = _clean_line(a.get("alt", ""))
            return
        if tag in _VOID:
            if tag == "hr":
                self._break(2)
            return
        hides = tag in _SKIP or "hidden" in a or bool(_HIDDEN_STYLE_RE.search(a.get("style", "")))
        if tag in _BLOCK:
            self._break(2 if tag in _PARA else 1)
        anchor = None
        if tag == "a":
            self._end_anchor(self.cur)
            anchor = {"href": a.get("href", ""), "start": None, "end": None, "alt": "",
                      "hidden": hides or self.hidden > 0}
            self.cur = anchor
        self.stack.append((tag, hides, anchor))
        if hides:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID or not any(t == tag for t, _, _ in self.stack):
            return
        self._close(tag)
        if tag in _BLOCK:
            self._break(2 if tag in _PARA else 1)
        elif tag in _CELL and self.n:
            self.sp = True

    def _close(self, tag: str) -> None:
        while self.stack:
            t, hides, anchor = self.stack.pop()
            if hides:
                self.hidden -= 1
            if anchor is not None:
                self._end_anchor(anchor)
            if t == tag:
                return

    def _end_anchor(self, anchor: dict | None) -> None:
        if anchor is None or anchor is not self.cur:
            return
        self.cur = None
        if anchor["start"] is None:
            anchor["start"] = self.n
        anchor["end"] = self.n
        if not anchor["hidden"]:
            self.anchors.append(anchor)

    def result(self) -> tuple[str, list[dict]]:
        self._end_anchor(self.cur)
        return "".join(self.parts), self.anchors


def html_to_text(html: str) -> tuple[str, list[dict]]:
    """(visible text, anchors with href/start/end/alt) for an HTML body."""
    p = _Flattener()
    p.feed(html or "")
    p.close()
    return p.result()


_PAREN_URL_RE = re.compile(r"(?P<label>[^\n()]*?)\s*\(\s*(?P<url>https?://[^\s()]+)\s*\)")


def plain_anchors(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Upwork's text/plain style 'label ( URL )': (text with the URL groups removed, [(label, url)]).

    Upwork's plain part carries HTML entities ("&lt; 30 hrs/week", "&amp;" in links): decoded first.
    """
    text = htmllib.unescape((text or "").replace("\r\n", "\n").replace("\r", "\n")).translate(_INVISIBLE)
    anchors = [(_clean_line(m.group("label")), m.group("url")) for m in _PAREN_URL_RE.finditer(text)]
    return _PAREN_URL_RE.sub(lambda m: m.group("label"), text), anchors


# ---------- "New job alert" parser (one job per mail) ----------

_SUBJECT_RE = re.compile(r"^\s*new job alert\s*[:\-–—]\s*(.+?)\s*$", re.I | re.S)
_READY_RE = re.compile(r"when\s+you(?:'|’)?re\s+ready|submit\s+a\s+proposal", re.I)
_VIEW_RE = re.compile(r"^view job", re.I)
_FOOTER_LINE_RE = re.compile(r"^(?:mobile app|follow us|download upwork|privacy policy|contact support|"
                             r"notification settings|©|\(c\)\s*\d)", re.I)
_TERMS_LINE_RE = re.compile(r"^(?:hourly|fixed[\s-]*price|fixed\b|budget|est(?:imated)?\.?\s*budget)", re.I)
_CLIENT_LINE_RE = re.compile(r"^payment\b", re.I)
_POSTED_LINE_RE = re.compile(r"^posted\b", re.I)
_QUERY_RE = re.compile(r"saved\s+search:\s*((?:(?!saved\s+search:).)+?)\s*\.?\s*take\s+a\s+look", re.I | re.S)


def subject_title(subject: str | None) -> str:
    m = _SUBJECT_RE.match(" ".join(str(subject or "").split()))
    return _clean_title(m.group(1)) if m else ""


def alert_job_id(msgid: str, title: str) -> str:
    """Stable id when no real job link is visible: Message-ID + title (one alert = one job)."""
    return "a-" + hashlib.sha1(f"{msgid}|{title.casefold()}".encode("utf-8")).hexdigest()[:20]


def _title_index(lines: list[str], title: str) -> int | None:
    if title:
        key = title.casefold()
        for i, ln in enumerate(lines):
            if ln.casefold() == key:
                return i
        stem = key.rstrip(".… ")
        if len(stem) >= 12:
            for i, ln in enumerate(lines):
                if ln.casefold().startswith(stem):
                    return i
    for i, ln in enumerate(lines):
        if _READY_RE.search(ln):
            j = i + 1
            while j < len(lines) and not lines[j]:
                j += 1
            return j if j < len(lines) else None
    return None


def _last(lines: list[str], lo: int, hi: int, rx: re.Pattern) -> int | None:
    for i in range(hi - 1, lo - 1, -1):
        if rx.match(lines[i]):
            return i
    return None


def _absorb(lines: list[str], i: int, stop: int) -> tuple[str, int]:
    """Line i plus following '•' items (blank lines between them are skipped) and hard-wrapped continuations."""
    parts, j, blank = [lines[i]], i + 1, False
    while j < stop:
        ln = lines[j]
        if not ln:
            blank, j = True, j + 1
            continue
        if ln[0] in "•·":
            parts.append(ln.lstrip("•· ").strip())
        elif not blank:
            parts[-1] = f"{parts[-1]} {ln}"
        else:
            break
        blank, j = False, j + 1
    return " • ".join(p for p in parts if p), j


def _paragraphs(lines: list[str]) -> str:
    paras, cur = [], []
    for ln in lines:
        if ln:
            cur.append(ln)
        elif cur:
            paras.append(" ".join(cur))
            cur = []
    if cur:
        paras.append(" ".join(cur))
    return "\n".join(paras)


def _alert_job(flat: str, anchors: list[tuple[str, str]], subject: str | None, msgid: str) -> Job | None:
    lines = [_clean_line(ln) for ln in flat.splitlines()]
    title = subject_title(subject)
    ti = _title_index(lines, title)
    if ti is None:
        return None
    title = title or _clean_title(lines[ti])
    if not title:
        return None
    end = next((i for i in range(ti + 1, len(lines))
                if _VIEW_RE.match(lines[i]) or _FOOTER_LINE_RE.match(lines[i])), len(lines))
    posted_i = _last(lines, ti + 1, end, _POSTED_LINE_RE)
    stop = posted_i if posted_i is not None else end
    client_i = _last(lines, ti + 1, stop, _CLIENT_LINE_RE)
    terms_i = _last(lines, ti + 1, client_i if client_i is not None else stop, _TERMS_LINE_RE)
    desc_end = min(i for i in (terms_i, client_i, stop) if i is not None)
    description = _paragraphs(lines[ti + 1:desc_end])
    terms = client = None
    after = desc_end
    if terms_i is not None:
        terms, after = _absorb(lines, terms_i, client_i if client_i is not None else stop)
    if client_i is not None:
        client, after = _absorb(lines, client_i, stop)
    skills = [ln for ln in lines[after:stop] if ln and ln[0] not in "•·" and "$" not in ln and len(ln) <= 60][:15]
    posted = None
    if posted_i is not None:
        m = re.match(r"posted\s*:?\s*(.+?)\.?$", lines[posted_i], re.I)
        posted = m.group(1) if m else None
    m = _QUERY_RE.search(" ".join(ln for ln in lines if ln))
    query = _clean_line(m.group(1))[:300] if m else None
    view = next((h for lbl, h in anchors if lbl and _VIEW_RE.match(lbl)), "")
    tlink = next((h for lbl, h in anchors if lbl and lbl.casefold() == title.casefold()), "")
    real = find_job(view) or find_job(tlink)
    jid, url = (real[0], job_url(real[0])) if real else (alert_job_id(msgid, title), view or tlink)
    f = budget_fields(terms) if terms else budget_fields(description)
    f.update(client_fields(client) if client else {})
    return Job(id=jid, url=url, title=title, snippet=_cap(description), query=query, terms=terms, client=client,
               skills=skills or None, posted=posted, experience=_experience(terms or ""), **f)


def parse_job_alert(subject: str | None, html: str | None, text: str | None, msgid: str = "") -> list[Job]:
    """One job from an Upwork 'New job alert: <title>' mail; [] when the layout is not recognised."""
    sources = []
    if html:
        flat, anchors = html_to_text(html)
        sources.append((flat, [(_clean_line(flat[a["start"]:a["end"]]) or a["alt"], a["href"]) for a in anchors]))
    if text:
        sources.append(plain_anchors(text))
    for flat, anchors in sources:
        job = _alert_job(flat, anchors, subject, msgid)
        if job:
            return [job]
    return []


# ---------- generic job links (digests and other multi-job formats) ----------

_ID = r"~(?P<id>[0-9a-z]{6,40})(?![0-9a-z])"
_JOB_RE = re.compile(r"(?:^|[^a-z0-9.-])(?:[a-z0-9-]+\.)*upwork\.com/(?:[^\s\"'<>#?]*?/)?(?:freelance-)?jobs?/"
                     r"(?P<slug>[^\s\"'<>#?~]*?)" + _ID, re.I)
_REL_JOB_RE = re.compile(r"(?:^|[=/&?])(?:freelance-)?jobs?/(?P<slug>[^\s\"'<>#?~&=]*?)" + _ID, re.I)
_UPWORK_URL_RE = re.compile(r"^(?:https?:)?//(?:[a-z0-9-]+\.)*upwork\.com(?:[:/?#]|$)", re.I)
_B64_RE = re.compile(r"[A-Za-z0-9_-]{24,}={0,2}|[A-Za-z0-9+/]{24,}={0,2}")
_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"'()\[\]]+", re.I)


def job_url(job_id: str) -> str:
    return JOB_URL.format(job_id)


def _b64(tok: str) -> str | None:
    t = tok.rstrip("=").replace("-", "+").replace("_", "/")
    if len(t) % 4 == 1:
        return None
    try:
        raw = base64.b64decode(t + "=" * (-len(t) % 4))
    except (binascii.Error, ValueError):
        return None
    return raw.decode("utf-8", errors="ignore")


def _direct(s: str) -> tuple[str, str] | None:
    variants: list[str] = []
    for _ in range(4):                           # as-is, then up to three rounds of percent-decoding
        if s in variants:
            break
        variants.append(s)
        s = unquote(s)
    for v in variants:
        m = _JOB_RE.search(v)
        if m:
            return m.group("id"), m.group("slug")
    for v in variants:
        if _UPWORK_URL_RE.match(v):              # relative /jobs/~id inside an upwork.com redirect
            m = _REL_JOB_RE.search(v)
            if m:
                return m.group("id"), m.group("slug")
    return None


def find_job(href: str | None) -> tuple[str, str] | None:
    """(job_id, slug) of an Upwork job link, unwrapping redirect wrappers locally; None for anything else."""
    s = htmllib.unescape(str(href or "")).strip()
    if not s:
        return None
    found = _direct(s)
    if found:
        return found
    for v in (s, unquote(s)):
        for tok in _B64_RE.findall(v):
            dec = _b64(tok)
            if dec and "upwork" in dec.lower():
                found = _direct(dec)
                if found:
                    return found
    return None


@dataclass
class _Hit:
    id: str
    slug: str
    start: int          # region start
    t_start: int        # title span (cut out of the snippet)
    t_end: int
    title: str


def _slug_title(slug: str) -> str:
    segs = [p for p in (slug or "").split("/") if p]
    if not segs:
        return ""
    t = re.sub(r"[-_]+", " ", segs[-1]).strip()
    return "" if _is_generic(t) else _clean_title(t)


def _first_title_line(region: str) -> str:
    for line in _tidy(region).splitlines():
        if line and not _META_RE.search(line) and not _is_generic(line):
            return _clean_title(line)
    return ""


def _assemble(text: str, hits: list[_Hit], strip_urls: bool = False) -> list[Job]:
    hits = sorted(hits, key=lambda h: h.start)
    jobs = []
    for k, h in enumerate(hits):
        end = hits[k + 1].start if k + 1 < len(hits) else len(text)
        if h.start <= h.t_start < h.t_end <= end:
            region = text[h.start:h.t_start] + "\n" + text[h.t_end:end]
        else:
            region = text[h.start:end]
        if strip_urls:
            region = _URL_RE.sub(" ", region)
        region = _cut_footer(region)
        title = h.title or _slug_title(h.slug) or _first_title_line(region) or f"Upwork job ~{h.id}"
        jobs.append(Job(id=h.id, url=job_url(h.id), title=title, snippet=_cap(_tidy(region)),
                        **extract_fields(region)))
    return jobs


def _html_hits(flat: str, anchors: list[dict]) -> list[_Hit]:
    groups: dict[str, dict] = {}
    for a in anchors:
        found = find_job(a["href"])
        if not found:
            continue
        g = groups.setdefault(found[0], {"slug": found[1], "start": a["start"], "title": None})
        if g["title"] is None:
            seg = flat[a["start"]:a["end"]]
            lead = len(seg) - len(seg.lstrip())
            first = seg.strip().split("\n", 1)[0]          # whole job card wrapped in one <a>: first line
            label = first or a["alt"]
            if not _is_generic(label):
                ts = a["start"] + lead
                g["title"] = (ts, ts + len(first), label)
    hits = []
    for jid, g in groups.items():
        ts, te, label = g["title"] or (g["start"], g["start"], "")
        hits.append(_Hit(jid, g["slug"], g["start"], ts, te, _clean_title(label)))
    return hits


def _text_hits(text: str) -> list[_Hit]:
    lines = text.split("\n")
    starts, pos = [], 0
    for ln in lines:
        starts.append(pos)
        pos += len(ln) + 1
    hits: list[_Hit] = []
    seen: set[str] = set()
    for m in _URL_RE.finditer(text):
        found = find_job(m.group().rstrip(".,;:!?"))
        if not found or found[0] in seen:
            continue
        seen.add(found[0])
        li = bisect.bisect_right(starts, m.start()) - 1
        ls = starts[li]
        prefix = _clean_title(text[ls:m.start()])
        if prefix and not _is_generic(prefix) and not _META_RE.search(prefix):
            hits.append(_Hit(found[0], found[1], ls, ls, m.start(), prefix))
            continue
        k = li - 1
        while k >= 0 and not lines[k].strip():
            k -= 1
        cand = _clean_title(lines[k]) if k >= max(0, li - 2) else ""
        if cand and not _URL_RE.search(lines[k]) and not _is_generic(cand) and not _META_RE.search(cand):
            hits.append(_Hit(found[0], found[1], starts[k], starts[k], starts[k] + len(lines[k]), cand))
        else:
            hits.append(_Hit(found[0], found[1], ls, ls, ls, ""))
    return hits


def parse_links(html: str | None, text: str | None) -> list[Job]:
    """Generic layout: one job per distinct upwork.com/jobs/~id link; html first, text/plain adds missing ids."""
    jobs: list[Job] = []
    if html:
        flat, anchors = html_to_text(html)
        jobs = _assemble(flat, _html_hits(flat, anchors))
    if text:
        t = text.replace("\r\n", "\n").replace("\r", "\n").translate(_INVISIBLE).replace("\xa0", " ")
        seen = {j.id for j in jobs}
        jobs += [j for j in _assemble(t, _text_hits(t), strip_urls=True) if j.id not in seen]
    return jobs
