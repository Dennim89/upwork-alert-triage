"""Composite job score, 0-10 = model's fit (0-4) + chance to win (0-3) + client (0-2) + money (0-1).

The model judges only what needs judgement (fit, chance to win, red flags). Client quality and money come from the
alert fields, so the scale is stable and every point can be explained on the card. Answers in the older schema
(the model's own score/verdict, no fit/win) pass through unchanged.
"""
from __future__ import annotations

import re

SEND_MIN = 5.0          # cards below this are logged, not sent
FIT_MIN = 2.5           # ...and so are jobs outside your field, however good the client and budget are
HOT_MIN = 7.0           # 🔥 on the card when the total is at least this...
HOT_WIN_MIN = 2.0       # ...and the model sees a real chance to win (long-term/senior jobs stay 🤔)


def spent_usd(value) -> float | None:
    """'$2k' -> 2000, '$201k' -> 201000, '$1.2M' -> 1200000, '$549' -> 549; None when absent/unreadable."""
    if value is None:
        return None
    m = re.search(r"\$?\s*(\d[\d,]*(?:\.\d+)?)\s*([kKmM])?\b", str(value))
    if not m:
        return None
    n = float(m.group(1).replace(",", ""))
    return n * {"k": 1e3, "m": 1e6}.get((m.group(2) or "").lower(), 1.0)


def client_points(f: dict) -> float:
    """0-2: payment verified 0.5, rating 0.5 (no reviews yet = 0.25), money spent on Upwork up to 1."""
    p = 0.5 if f.get("payment_verified") is True else 0.0
    rating = f.get("client_rating")
    if rating is None:
        p += 0.25
    elif rating >= 4.5:
        p += 0.5
    elif rating >= 4.0:
        p += 0.25
    spent = spent_usd(f.get("client_spend"))
    if spent is not None:
        p += 1.0 if spent >= 10_000 else 0.5 if spent >= 1_000 else 0.25 if spent >= 100 else 0.0
    return min(2.0, p)


def money_points(f: dict) -> float:
    """0-1 from the budget; a rate or amount left blank by the client is neutral, not zero."""
    bt = f.get("budget_type")
    if bt == "fixed":
        b = f.get("fixed_budget")
        if not b:
            return 0.5
        return 1.0 if b >= 150 else 0.5 if b >= 50 else 0.0
    if bt == "hourly":
        hi = f.get("hourly_max")
        if not hi:
            return 0.75
        return 1.0 if hi >= 25 else 0.5 if hi >= 15 else 0.0
    return 0.5


def _clamp(x, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(x)))


def finalize(draft: dict | None, fields: dict | None) -> dict | None:
    """Adds score (0-10), verdict ('apply' when score >= SEND_MIN and no red flag) and the breakdown."""
    if draft is None or draft.get("fit") is None or draft.get("win") is None:
        return draft
    f = fields or {}
    fit, win = _clamp(draft["fit"], 0, 4), _clamp(draft["win"], 0, 3)
    client, money = client_points(f), money_points(f)
    red = bool(draft.get("red_flag"))
    score = 0.0 if red else round(fit + win + client + money, 1)
    verdict = "apply" if not red and score >= SEND_MIN and fit >= FIT_MIN else "skip"
    return dict(draft, fit=fit, win=win, client_pts=client, money_pts=money, score=score, verdict=verdict)


def _pts(x: float) -> str:
    return f"{x:g}"


def label(draft: dict) -> str:
    """'8.5/10 🔥 apply · fit 4/4 · chance 2/3 · client 1.5/2 · money 1/1' (old schema: '6/10')."""
    score = draft.get("score")
    head = "-/10" if score is None else f"{_pts(score)}/10"
    if draft.get("red_flag"):
        return f"{head} ⛔ red flag"
    if draft.get("fit") is None:
        return head
    if draft["fit"] < FIT_MIN:
        tag = "outside your field"
    elif score >= HOT_MIN and draft["win"] >= HOT_WIN_MIN:
        tag = "🔥 apply"
    elif score >= SEND_MIN:
        tag = "🤔 maybe"
    else:
        tag = "weak"
    return (f"{head} {tag} · fit {_pts(draft['fit'])}/4 · chance {_pts(draft['win'])}/3 · "
            f"client {_pts(draft['client_pts'])}/2 · money {_pts(draft['money_pts'])}/1")
