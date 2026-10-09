import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot import llm, parse, pipeline, rules, telegram
from bot.store import Store
from tests.support import BLANK_RATE, CSV, CSV_TITLE, LOW_RATE, RED_FLAG, SLACK, SLACK_TITLE, canned_stdout, item

ALERTS = [CSV, SLACK, RED_FLAG, LOW_RATE, BLANK_RATE]
OTHERS = ["digest.eml", "other_profile.eml"]
LIBRARY = "Scraper for public library event listings"


class RecordingSender:
    def __init__(self, owner=1, fail_times=0):
        self.owner, self.fail_times = owner, fail_times
        self.cards: list[tuple] = []

    def owner_chat(self):
        return self.owner

    def send_card(self, job, draft, note):
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("telegram down")
        self.cards.append((job["id"], job["title"], draft, note, telegram.format_card(job, draft, note)))


class Drafter:
    def __init__(self, by_title=None, default=None):
        self.by_title = by_title or {}
        self.default = default if default is not None else llm.draft_from_output(canned_stdout())
        self.calls: list[str] = []

    def __call__(self, job):
        self.calls.append(job["title"])
        return self.by_title.get(job["title"], self.default)


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "jobs.db")
        self.addCleanup(self.store.close)

    def items(self, names=ALERTS):
        """Job-alert mails this parser version has not seen (what the daemon's select() lets through)."""
        out = []
        for i, n in enumerate(names):
            it = item(n, uid=str(i))
            if parse.classify(it.subject) == "job_alert" and not self.store.email_done(it.msgid, parse.PARSER_VERSION):
                out.append(it)
        return out

    def test_end_to_end_once_per_job(self):
        sender, drafter = RecordingSender(), Drafter()
        st = pipeline.run_pass(self.store, self.items(ALERTS + OTHERS), sender, drafter, max_llm=3)
        self.assertEqual((st["emails"], st["candidate"], st["skip_rule"], st["llm"], st["sent"]), (5, 3, 2, 3, 3))
        self.assertEqual([c[1] for c in sender.cards], [CSV_TITLE, SLACK_TITLE, LIBRARY])      # newest mail first
        reasons = {j["title"]: j["reason"] for j in self.store.all_jobs() if j["status"] == "skip_rule"}
        self.assertEqual(reasons, {"Quick fix for the checkout page of our online store": "red flag: contact off-platform",
                                   "Copy product names from PDF catalogs into Excel": "low hourly: max $8/h < $15"})
        again = pipeline.run_pass(self.store, self.items(), sender, drafter, max_llm=3)   # same mail again
        self.assertEqual(dict(again), {})                       # already parsed: not even selected
        forced = [item(n) for n in ALERTS]                       # even if handed over again: no new cards
        st3 = pipeline.run_pass(self.store, forced, sender, drafter, max_llm=3)
        self.assertEqual((st3["seen"], st3["sent"], len(sender.cards)), (5, 0, 3))
        self.assertEqual(len(drafter.calls), 3)
        card = sender.cards[0][4]
        self.assertIn("Score: 9/10 🔥 apply", card)              # canned draft + alert fields
        self.assertIn("Niche: python automation", card)

    def test_max_llm_per_pass_and_queue(self):
        sender = RecordingSender()
        pipeline.run_pass(self.store, self.items(), sender, Drafter(), max_llm=1)
        self.assertEqual(len(sender.cards), 1)
        pipeline.run_pass(self.store, [], sender, Drafter(), max_llm=1)
        pipeline.run_pass(self.store, [], sender, Drafter(), max_llm=1)
        self.assertEqual(len(sender.cards), 3)
        self.assertEqual(self.store.counts(), {"sent": 3, "skip_rule": 2})

    def test_skip_verdicts_and_missing_draft(self):
        weak = ({"fit": 1, "win": 1, "why": "not your field", "proposal_en": "x"}, None)
        drafter = Drafter({CSV_TITLE: weak, LIBRARY: (None, "claude CLI not found")})
        sender = RecordingSender()
        pipeline.run_pass(self.store, self.items(), sender, drafter, max_llm=3)
        self.assertEqual([c[1] for c in sender.cards], [SLACK_TITLE, LIBRARY])
        self.assertIn("No draft: claude CLI not found.", sender.cards[1][4])
        statuses = {j["title"]: j["status"] for j in self.store.all_jobs()}
        self.assertEqual(statuses[CSV_TITLE], "skip_llm")
        self.assertEqual(self.store.get(sender.cards[0][0])["status"], "sent")

    def test_older_answer_schema(self):
        skip5 = ({"score": 5.0, "verdict": "skip", "why": "not a fit", "proposal_en": "x"}, None)
        skip7 = ({"score": 7.0, "verdict": "skip", "why": "borderline", "proposal_en": "y"}, None)
        sender = RecordingSender()
        pipeline.run_pass(self.store, self.items(), sender, Drafter({CSV_TITLE: skip5, SLACK_TITLE: skip7}), max_llm=3)
        self.assertEqual([c[1] for c in sender.cards], [SLACK_TITLE, LIBRARY])

    def test_send_failure_keeps_draft_and_retries(self):
        sender, drafter = RecordingSender(fail_times=1), Drafter()
        st = pipeline.run_pass(self.store, self.items(), sender, drafter, max_llm=3)
        self.assertEqual((st["send_error"], st["sent"], st["llm"]), (1, 0, 1))      # the pass stops at the failure
        self.assertEqual(len(self.store.by_status("ready")), 1)
        st = pipeline.run_pass(self.store, [], sender, drafter, max_llm=3)
        self.assertEqual(st["sent"], 3)
        self.assertEqual(len(drafter.calls), 3)                   # the stored draft was reused, not redrafted
        dead = RecordingSender(fail_times=99)
        self.store.save_draft(self.store.all_jobs()[0]["id"], None, "ready")
        for _ in range(5):
            pipeline.process(self.store, dead, drafter)
        self.assertEqual(len(self.store.by_status("failed")), 1)

    def test_no_owner_means_no_llm_calls(self):
        drafter = Drafter()
        st = pipeline.run_pass(self.store, self.items(), RecordingSender(owner=None), drafter)
        self.assertEqual((st["no_owner"], st["candidate"], drafter.calls), (1, 3, []))
        self.assertEqual(len(self.store.next_candidates(10)), 3)

    def test_bad_email_does_not_stop_the_run(self):
        real_parse = parse.parse_email

        def flaky(subject, html, text, msgid=""):
            if "checkout" in subject:
                raise ValueError("boom")
            return real_parse(subject, html, text, msgid)

        with mock.patch.object(parse, "parse_email", flaky):
            st = pipeline.run_pass(self.store, self.items(), RecordingSender(), Drafter(), max_llm=3)
        self.assertEqual((st["email_error"], st["emails"], st["sent"]), (1, 4, 3))

    def test_same_job_in_two_alerts_is_a_dup(self):
        a = item(CSV)
        b = item(CSV)
        b.msgid = "<second-saved-search@x>"
        st = pipeline.run_pass(self.store, [a, b], RecordingSender(), Drafter())
        self.assertEqual((st["candidate"], st["dup"], st["sent"]), (1, 1, 1))

    def test_expiry_and_clock(self):
        pipeline.run_pass(self.store, self.items(), RecordingSender(owner=None), Drafter(), now="2026-05-14T18:00:00Z")
        self.assertEqual(self.store.get(self.store.all_jobs()[0]["id"])["first_seen"], "2026-05-14T18:00:00Z")
        st = pipeline.process(self.store, RecordingSender(), Drafter(), expire_before="2026-05-15T00:00:00Z")
        self.assertEqual((st["expired"], st["llm"]), (3, 0))

    def test_rule_settings_from_config(self):
        strict = rules.settings(["slack bot"], min_hourly=60, min_fixed=30)
        st = pipeline.run_pass(self.store, self.items(), RecordingSender(), Drafter(), max_llm=3, cfg=strict)
        self.assertEqual((st["candidate"], st["skip_rule"]), (2, 3))
        jobs = {j["title"]: j for j in self.store.all_jobs()}
        self.assertEqual(jobs[SLACK_TITLE]["reason"], "low hourly: max $55/h < $60")
        self.assertEqual(jobs[CSV_TITLE]["fit_tags"], [])


if __name__ == "__main__":
    unittest.main()
