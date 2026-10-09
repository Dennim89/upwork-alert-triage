import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from bot import ledger, schedule
from bot.parse import Job
from bot.store import Store

UTC = timezone.utc


def utc(*a) -> float:
    return datetime(*a, tzinfo=UTC).timestamp()


def berlin():
    tz, ok = schedule.zone("Europe/Berlin")
    return tz if ok else None


class LedgerTextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.s = Store(Path(self.tmp.name) / "jobs.db")
        self.addCleanup(self.s.close)

    def test_balance_text(self):
        self.assertEqual(ledger.balance_text(self.s), "not set (/balance N)")
        self.s.set_balance(60)
        self.assertEqual(ledger.balance_text(self.s), "60")

    def test_stats_and_summary_texts(self):
        self.s.set_balance(60)
        self.s.record_decision(job_id="j1", title="T", status="sent", connects=8, ts="2026-05-14T19:00:00Z")
        self.s.add_job(Job(id="a", url="u", title="Job a"), msgid="<a>", email_date=None, status="ready",
                       now="2026-05-14T17:00:00Z")
        self.s.mark_sent("a", now="2026-05-14T17:05:00Z")
        self.s.add_job(Job(id="b", url="u", title="Job b"), msgid="<b>", email_date=None, status="skip_rule",
                       now="2026-05-14T17:00:00Z")
        self.s.add_event(msgid="<m1>", kind="offer", subject="s", email_date="2026-05-14T16:00:00Z", status="sent")
        self.s.add_event(msgid="<m2>", kind="proposal_viewed", subject="s", email_date="2026-05-13T10:00:00Z",
                         status="sent")
        text = ledger.stats_text(self.s, utc(2026, 5, 14, 20, 30), UTC, paused=True, llm_used=3, llm_max=20,
                                 owner_queue=1)
        for s in ("Connects balance: 52", "Today: cards 1 · proposals 1 (8 Connects) · skipped 0",
                  "filtered by rules 1", "offers 1", "views 1", "LLM calls this hour 3/20", "texts 1", "Paused: yes"):
            self.assertIn(s, text)
        summary = ledger.summary_text(self.s, date(2026, 5, 14), UTC, llm_calls=7, paused=False)
        self.assertIn("<b>Daily summary · 2026-05-14</b>", summary)
        self.assertIn("Job cards: 1 (filtered by rules 1, by the model 0)", summary)
        self.assertIn("offers 1", summary)
        self.assertIn("views 0", summary)                                  # yesterday's view is not today's
        self.assertIn("LLM calls: 7", summary)


class ScheduleTest(unittest.TestCase):
    def test_zone(self):
        self.assertEqual(schedule.zone(""), (UTC, True))
        self.assertEqual(schedule.zone("utc"), (UTC, True))
        self.assertEqual(schedule.zone("Not/AZone"), (UTC, False))

    def test_utc_day_bounds_and_summary(self):
        self.assertEqual(schedule.day_bounds(date(2026, 5, 14), UTC), ("2026-05-14T00:00:00Z", "2026-05-15T00:00:00Z"))
        self.assertEqual(schedule.local_day(utc(2026, 5, 14, 23, 59), UTC), date(2026, 5, 14))
        self.assertIsNone(schedule.summary_due(utc(2026, 5, 14, 20, 59), None, 21, UTC))
        self.assertEqual(schedule.summary_due(utc(2026, 5, 14, 21, 0), None, 21, UTC), "2026-05-14")
        self.assertIsNone(schedule.summary_due(utc(2026, 5, 14, 22, 0), "2026-05-14", 21, UTC))
        self.assertEqual(schedule.summary_due(utc(2026, 5, 14, 23, 30), "2026-05-13", 21, UTC), "2026-05-14")  # late
        self.assertIsNone(schedule.summary_due(utc(2026, 5, 15, 6, 0), "2026-05-13", 21, UTC))   # morning: wait
        self.assertEqual(schedule.summary_due(utc(2026, 5, 15, 0, 1), "2026-05-14", 0, UTC), "2026-05-15")
        self.assertIsNone(schedule.summary_due(utc(2026, 5, 15, 23, 0), None, None, UTC))        # summary off

    def test_named_zone_with_daylight_saving(self):
        tz = berlin()
        if tz is None:
            self.skipTest("no time zone database here (pip install tzdata)")
        self.assertEqual(schedule.to_local(utc(2026, 5, 14, 19, 0), tz).hour, 21)        # CEST, UTC+2
        self.assertEqual(schedule.to_local(utc(2026, 12, 1, 20, 0), tz).hour, 21)        # CET, UTC+1
        self.assertEqual(schedule.day_bounds(date(2026, 5, 14), tz), ("2026-05-13T22:00:00Z", "2026-05-14T22:00:00Z"))
        self.assertEqual(schedule.day_bounds(date(2026, 10, 25), tz), ("2026-10-24T22:00:00Z", "2026-10-25T23:00:00Z"))
        self.assertIsNone(schedule.summary_due(utc(2026, 5, 14, 18, 59), None, 21, tz))
        self.assertEqual(schedule.summary_due(utc(2026, 5, 14, 19, 0), None, 21, tz), "2026-05-14")


if __name__ == "__main__":
    unittest.main()
