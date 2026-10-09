"""Connects ledger and summaries. The alert e-mail never shows Connects: you set the balance with /balance N and
the card buttons spend it ([✅ Sent] -> 4/8/11/16/20/24). One decision per job; history in the `ledger` table.
"""
from __future__ import annotations

from datetime import date, tzinfo

from . import events, schedule
from .store import Store
from .telegram import esc

QUICK_CONNECTS = (4, 8, 11, 16, 20, 24)


def balance_text(store: Store) -> str:
    bal = store.balance()
    return str(bal) if bal is not None else "not set (/balance N)"


def period(store: Store, since: str | None = None, until: str | None = None) -> dict:
    p = store.proposal_stats(since, until)
    if since or until:
        cards = store.count_jobs(since=since, until=until, by="sent_at")
    else:
        cards = store.count_jobs(status="sent")
    return {"cards": cards, "skip_rule": store.count_jobs(status="skip_rule", since=since, until=until),
            "skip_llm": store.count_jobs(status="skip_llm", since=since, until=until),
            "sent": p["sent"], "connects": p["connects"], "skipped": p["skipped"],
            "events": store.event_counts(since, until)}


def _events_line(counts: dict) -> str:
    return " · ".join(f"{label} {counts.get(kind, 0)}" for kind, label in events.STATS_KINDS)


def stats_text(store: Store, now: float, tz: tzinfo, *, paused: bool, llm_used: int, llm_max: int,
               owner_queue: int) -> str:
    since, until = schedule.day_bounds(schedule.local_day(now, tz), tz)
    today, total = period(store, since, until), period(store)
    return "\n".join([
        "<b>Stats</b>",
        f"Connects balance: {esc(balance_text(store))}",
        f"Today: cards {today['cards']} · proposals {today['sent']} ({today['connects']} Connects) · "
        f"skipped {today['skipped']}",
        f"All time: cards {total['cards']} · proposals {total['sent']} ({total['connects']} Connects) · "
        f"skipped {total['skipped']} · filtered by rules {total['skip_rule']}",
        f"Events, all time: {_events_line(total['events'])}",
        f"Queue: jobs {store.count_jobs(status='candidate')} · texts {owner_queue} · "
        f"LLM calls this hour {llm_used}/{llm_max}",
        f"Paused: {'yes (no job cards)' if paused else 'no'}",
    ])


def summary_text(store: Store, day: date, tz: tzinfo, *, llm_calls: int, paused: bool) -> str:
    since, until = schedule.day_bounds(day, tz)
    d = period(store, since, until)
    return "\n".join([
        f"<b>Daily summary · {day.isoformat()}</b>",
        f"Job cards: {d['cards']} (filtered by rules {d['skip_rule']}, by the model {d['skip_llm']})",
        f"Proposals: sent {d['sent']} ({d['connects']} Connects), skipped {d['skipped']}",
        f"Connects balance: {esc(balance_text(store))}",
        f"Events: {_events_line(d['events'])}",
        f"LLM calls: {llm_calls}" + (" · paused" if paused else ""),
    ])
