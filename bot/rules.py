"""Cheap prefilter before any LLM call: hard red flags, niche fit hint, too-small budget (when parseable).

The red-flag list is fixed (edit RED_FLAGS). The niche keywords and the budget floors are settings
(NICHE_KEYWORDS, MIN_HOURLY_USD, MIN_FIXED_USD); RuleSettings carries them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# (reason label, pattern) - case-insensitive; the first match is the skip reason. One list, edit here.
RED_FLAGS = (
    ("volume bot", r"\bvolume[\s-]*(?:bot|booster)s?\b"),
    ("wash trading", r"\bwash[\s-]*trad"),
    ("bundler", r"\bbundlers?\b"),
    ("rug pull", r"\brug[\s-]*pull"),
    ("pump and dump", r"\bpump[\s-]*(?:and|&|n|'n')[\s-]*dump"),
    ("drainer", r"\bdrainers?\b"),
    ("phishing", r"phishing"),
    ("contact off-platform", r"\b(?:contact|message|text|reach|dm|ping|add|call|write\s+to)\s+(?:me|us)\s+"
                             r"(?:on|via|at|through|in)\s+(?:telegram|whats\s*app|skype|signal|discord)\b"),
    ("contact off-platform", r"\b(?:my|our)\s+(?:telegram|whats\s*app|skype)\s*(?:id|handle|username|number)?"
                             r"\s*(?:is)?\s*[:\-]?\s*[@+]"),
    ("outside upwork", r"\boutside\s+(?:of\s+)?upwork\b|\boff[\s-]+(?:upwork|platform)\b"),
    ("pay for training", r"\bpay\s+(?:for\s+)?(?:the\s+|your\s+)?training\b|\btraining\s+fee\b"),
    # money asked from the freelancer, not a product's deposit flow ("users deposit USDC" must pass)
    ("deposit", r"\b(?:security|refundable|registration|activation|upfront)\s+deposit\b|\bdeposit\s+fee\b"
                r"|\b(?:you|applicants?|freelancers?|candidates?)\b[^.\n]{0,40}?\b(?:make|pay|send|transfer|need\s+to"
                r"|have\s+to|must|required\s+to)\s+(?:make\s+|pay\s+|send\s+)?(?:(?:an?|the|small|initial|first)\s+)*"
                r"deposit\b"),
    ("unpaid test", r"\bunpaid\s+(?:test|trial|task|sample)s?\b"),
    ("clone our repo and run", r"\bclone\s+(?:our|my)\s+(?:\w+\s+)?repo(?:sitory)?\b[^.\n]{0,60}?\brun\b"),
    ("download and run", r"\bdownload\s+and\s+(?:run|execute|launch)\b"),
    ("anydesk", r"\bany\s*desk\b"),
    ("teamviewer", r"\bteam\s*viewer\b"),
    ("fake reviews", r"\bfake\s+reviews?\b"),
    ("account rental", r"\baccount\s+rent(?:al|ing)\b|\brent(?:ing)?\s+(?:your|an?)\s+(?:\w+\s+)?account\b"),
    ("sandwich attack", r"\bsandwich(?:ing)?[\s-]*(?:attack|bot)s?\b"),
)

# default niche keywords -> fit hint (number of distinct niches matched); NICHE_KEYWORDS replaces this list
DEFAULT_NICHE = (
    ("telegram bot", r"\btelegram\s*bots?\b"),
    ("chatbot", r"\bchat\s*bots?\b"),
    ("ai agent", r"\bai[\s-]*agents?\b"),
    ("llm", r"\bllms?\b"),
    ("openai", r"\bopen\s*ai\b|\bchat\s*gpt\b|\bgpt-?\d"),
    ("claude", r"\bclaude\b|\banthropic\b"),
    ("scraping", r"\bscrap(?:e|er|ers|ing)\b|\bcrawl(?:er|ers|ing)?\b"),
    ("python automation", r"\bpython\b[^.\n]{0,40}\b(?:automat\w*|scripts?)\b|\b(?:automat\w*|scripts?)\b[^.\n]{0,40}"
                          r"\bpython\b"),
    ("api integration", r"\bapi\s+integrations?\b|\bintegrat\w*\s+(?:\w+\s+){0,3}apis?\b"),
    ("data pipeline", r"\bdata\s+pipelines?\b|\betl\b"),
    ("dashboard", r"\bdashboards?\b"),
)
MIN_HOURLY_MAX = 15.0
MIN_FIXED = 30.0

_RED = tuple((label, re.compile(p, re.I)) for label, p in RED_FLAGS)


def keyword_pattern(word: str) -> re.Pattern:
    """'telegram bot' -> matches 'Telegram bot', 'telegram-bots', 'TelegramBot'; 'c++' stays literal."""
    parts = [re.escape(p) for p in word.lower().split()]
    if not parts:
        raise ValueError("empty keyword")
    return re.compile(r"(?<!\w)" + r"[\s-]*".join(parts) + r"(?:s|es)?(?!\w)", re.I)


@dataclass(frozen=True)
class RuleSettings:
    niche: tuple[tuple[str, re.Pattern], ...] = tuple((label, re.compile(p, re.I)) for label, p in DEFAULT_NICHE)
    min_hourly: float = MIN_HOURLY_MAX
    min_fixed: float = MIN_FIXED


DEFAULT = RuleSettings()


def settings(keywords: tuple[str, ...] | list[str] = (), min_hourly: float = MIN_HOURLY_MAX,
             min_fixed: float = MIN_FIXED) -> RuleSettings:
    """RuleSettings from config values; no keywords = the default niche list."""
    niche = tuple((" ".join(w.lower().split()), keyword_pattern(w)) for w in keywords if w.strip())
    return RuleSettings(niche=niche or DEFAULT.niche, min_hourly=float(min_hourly), min_fixed=float(min_fixed))


@dataclass(frozen=True)
class RuleResult:
    skip: bool
    reason: str | None
    fit: int
    tags: tuple[str, ...]


def red_flag(text: str) -> str | None:
    for label, rx in _RED:
        if rx.search(text or ""):
            return label
    return None


def fit_tags(text: str, cfg: RuleSettings = DEFAULT) -> tuple[str, ...]:
    return tuple(label for label, rx in cfg.niche if rx.search(text or ""))


def budget_problem(fields: dict, cfg: RuleSettings = DEFAULT) -> str | None:
    bt, hmax, fixed = fields.get("budget_type"), fields.get("hourly_max"), fields.get("fixed_budget")
    # Upwork prints "Hourly: $0-$0" / "$0" when the client left the rate blank: that is "unknown", not "free".
    if bt == "hourly" and hmax is not None and 0 < hmax < cfg.min_hourly:
        return f"low hourly: max ${hmax:g}/h < ${cfg.min_hourly:g}"
    if bt == "fixed" and fixed is not None and 0 < fixed < cfg.min_fixed:
        return f"low budget: ${fixed:g} < ${cfg.min_fixed:g}"
    return None


def check(title: str, text: str, fields: dict | None = None, cfg: RuleSettings = DEFAULT) -> RuleResult:
    """text = everything readable about the job (title, description, terms, client, skills)."""
    full = f"{title}\n{text}"
    tags = fit_tags(full, cfg)
    flag = red_flag(full)
    if flag:
        return RuleResult(True, f"red flag: {flag}", len(tags), tags)
    low = budget_problem(fields or {}, cfg)
    if low:
        return RuleResult(True, low, len(tags), tags)
    return RuleResult(False, None, len(tags), tags)
