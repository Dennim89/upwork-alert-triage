import sqlite3
import tempfile
import unittest
from pathlib import Path

from bot.parse import Job
from bot.store import MIGRATIONS, Store


def job(i: str, **kw) -> Job:
    return Job(id=i, url=f"https://click.example.com/t/{i}/view", title=f"Job {i}", snippet=f"desc {i}", **kw)


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "data" / "jobs.db"
        self.s = Store(self.path)
        self.addCleanup(lambda: self.s.close())

    def add(self, i, date, fit=0, status="candidate", **kw):
        return self.s.add_job(job(i, **kw), msgid=f"<{i}@m>", email_date=date, status=status, fit=fit,
                              fit_tags=("dashboard",), fingerprint=f"fp{i}")


class JobsTest(StoreCase):
    def test_schema_version_and_reopen(self):
        self.assertEqual(self.s.conn.execute("PRAGMA user_version").fetchone()[0], len(MIGRATIONS))
        self.add("a", "2026-05-14T10:00:00Z")
        self.s.close()
        self.s = Store(self.path)
        self.assertTrue(self.s.has_job("a"))
        conn = sqlite3.connect(self.path)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        self.assertTrue({"jobs", "emails", "events", "proposals", "ledger", "tg_messages", "owner_tasks",
                         "llm_calls", "kv"} <= tables)

    def test_insert_once_and_fields_roundtrip(self):
        self.assertTrue(self.add("a", "2026-05-14T10:00:00Z", hourly_max=25.0, skills=["API"]))
        self.assertFalse(self.add("a", "2026-05-14T10:00:00Z"))
        row = self.s.get("a")
        self.assertEqual(row["fields"], {"hourly_max": 25.0, "skills": ["API"]})
        self.assertEqual(row["fit_tags"], ["dashboard"])
        self.assertEqual(self.s.find_fingerprint("fpa"), "a")
        self.assertIsNone(self.s.find_fingerprint(None))
        self.assertEqual(self.s.find_job_by_title("job A")["id"], "a")             # case-insensitive
        self.assertIsNone(self.s.find_job_by_title("x"))

    def test_candidate_order_newest_mail_then_fit(self):
        self.add("old", "2026-05-13T10:00:00Z", fit=5)
        self.add("new_lo", "2026-05-14T10:00:00Z", fit=0)
        self.add("new_hi", "2026-05-14T10:00:00Z", fit=2)
        self.add("skip", "2026-05-14T11:00:00Z", status="skip_rule")
        self.assertEqual([j["id"] for j in self.s.next_candidates(3)], ["new_hi", "new_lo", "old"])
        self.assertEqual([j["id"] for j in self.s.next_candidates(1)], ["new_hi"])

    def test_draft_send_and_failure_cap(self):
        self.add("a", "2026-05-14T10:00:00Z")
        self.s.save_draft("a", {"score": 8.0, "verdict": "apply", "proposal_en": "Hi"}, "ready")
        row = self.s.get("a")
        self.assertEqual((row["status"], row["score"], row["verdict"], row["draft"]["proposal_en"]),
                         ("ready", 8.0, "apply", "Hi"))
        statuses = [self.s.send_failed("a", max_attempts=3) for _ in range(3)]
        self.assertEqual(statuses, ["ready", "ready", "failed"])
        self.add("b", "2026-05-14T10:00:00Z")
        self.s.save_draft("b", None, "ready", reason="claude CLI not found")
        self.s.mark_sent("b", now="2026-05-14T12:00:00Z")
        row = self.s.get("b")
        self.assertEqual((row["status"], row["sent_at"], row["draft"], row["reason"]),
                         ("sent", "2026-05-14T12:00:00Z", None, "claude CLI not found"))
        self.assertEqual(self.s.count_jobs(since="2026-05-14T00:00:00Z", until="2026-05-15T00:00:00Z", by="sent_at"), 1)
        with self.assertRaises(ValueError):
            self.s.count_jobs(by="title")

    def test_expire_and_counts(self):
        self.add("old", "2026-05-01T10:00:00Z")
        self.add("new", "2026-05-14T10:00:00Z")
        self.assertEqual(self.s.expire_candidates("2026-05-11T00:00:00Z"), 1)
        self.assertEqual(self.s.counts(), {"expired": 1, "candidate": 1})

    def test_email_done_respects_parser_version(self):
        self.assertFalse(self.s.email_done("<m@x>", 1))
        self.s.mark_email("<m@x>", kind="job_alert", email_date=None, subject="s", n_jobs=1, parser_version=1)
        self.assertTrue(self.s.email_done("<m@x>", 1))
        self.assertFalse(self.s.email_done("<m@x>", 2))                  # a parser upgrade re-parses
        self.s.mark_email("<m@x>", kind="job_alert", email_date=None, subject="s", n_jobs=1, parser_version=2)
        self.assertTrue(self.s.email_done("<m@x>", 2))


class OtherTablesTest(StoreCase):
    def test_events_once_per_message_and_counts_by_date(self):
        first = self.s.add_event(msgid="<e1>", kind="offer", subject="s", email_date="2026-05-14T10:00:00Z",
                                 status="new", client="Lena W.")
        self.assertIsNone(self.s.add_event(msgid="<e1>", kind="offer", subject="s", email_date=None, status="new"))
        self.s.add_event(msgid="<e2>", kind="payment", subject="s", email_date="2026-05-13T10:00:00Z", status="new")
        self.assertEqual(self.s.get_event(first)["client"], "Lena W.")
        self.s.set_event(first, status="sent", draft={"reply_en": "Hi"})
        self.assertEqual(self.s.get_event(first)["draft"], {"reply_en": "Hi"})
        self.assertEqual([e["msgid"] for e in self.s.events_by_status("new")], ["<e2>"])
        self.assertEqual(self.s.event_counts("2026-05-14T00:00:00Z"), {"offer": 1})
        self.assertEqual(self.s.event_counts(), {"offer": 1, "payment": 1})

    def test_messages_tasks_and_llm_calls(self):
        self.s.add_tg_message(7, 100, "job_card", "a-1")
        self.assertEqual(self.s.tg_message(7, 100)["ref"], "a-1")
        self.assertIsNone(self.s.tg_message(7, 101))
        tid = self.s.add_owner_task(chat_id=7, message_id=5, text="hello", mode="client", context="ctx")
        self.assertEqual(self.s.next_owner_task()["id"], tid)
        self.assertEqual(self.s.count_owner_tasks(), 1)
        self.s.finish_owner_task(tid, "done", result={"reply_en": "Hi"})
        self.assertEqual(self.s.get_owner_task(tid)["result"], {"reply_en": "Hi"})
        self.assertIsNone(self.s.next_owner_task())
        for ts in (100.0, 200.0, 4000.0):
            self.s.record_llm_call(ts, "job")
        self.assertEqual((self.s.llm_calls_between(150.0), self.s.llm_calls_between(0, 300)), (2, 2))
        self.assertEqual(self.s.oldest_llm_call_since(150.0), 200.0)
        self.assertEqual(self.s.prune_llm_calls(1000.0), 2)

    def test_connects_spent_once_per_job(self):
        self.assertIsNone(self.s.balance())
        self.assertEqual(self.s.record_decision(job_id="j", title="T", status="sent", connects=4), (True, None))
        self.assertIsNone(self.s.balance())                              # unknown balance is not invented
        self.s.set_balance(60, note="/balance")
        self.assertEqual(self.s.record_decision(job_id="j1", title="Task one", status="sent", connects=8), (True, 52))
        self.assertEqual(self.s.record_decision(job_id="j1", title="Task one", status="sent", connects=11), (False, 52))
        self.assertEqual(self.s.record_decision(job_id="j2", title="T2", status="skipped"), (True, 52))
        self.assertEqual(self.s.set_balance(50), 50)
        self.assertEqual([(r["kind"], r["delta"], r["balance"]) for r in self.s.ledger_rows()],
                         [("proposal", -4, None), ("set", 60, 60), ("proposal", -8, 52), ("set", -2, 50)])
        self.assertEqual(self.s.proposal_stats(), {"sent": 2, "skipped": 1, "connects": 12})
        self.assertEqual(self.s.find_proposal_by_title("task ONE")["job_id"], "j1")


if __name__ == "__main__":
    unittest.main()
