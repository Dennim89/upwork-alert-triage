import contextlib
import io
import logging
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import bot_daemon
from bot import assist, config, llm
from bot import state as statemod
from bot.daemon import HELP_TEXT, SEND_TEXT, Daemon
from bot.simulate import FakeBotApi, callback_update, canned_llm, fixture_fetch, message_update
from bot.telegram import TelegramClient
from tests.support import CSV, CSV_TITLE, LOW_RATE, RED_FLAG, SLACK, TOKEN, make_config

OWNER = 1001
T0 = datetime(2026, 5, 14, 18, 0, tzinfo=timezone.utc).timestamp()
PROFILE = config.BASE_DIR / "profile.example.md"


class DaemonCase(unittest.TestCase):
    MAILS: tuple = ()
    OWNER_BOUND = True
    PER_HOUR = 20
    SETTINGS: dict = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.cfg = make_config(Path(self.tmp.name), profile_path=PROFILE, freelancer_name="Jordan",
                               **self.SETTINGS)
        if self.OWNER_BOUND:
            statemod.save(self.cfg.state_path, {"owner_chat_id": OWNER, "owner_name": "jordan"})
        self.api = FakeBotApi()
        self.clock = [T0]
        self.sleeps: list[float] = []
        self.prompts: list[tuple[str, str]] = []
        self.canned = canned_llm()
        self.mails = list(self.MAILS)
        self.d = Daemon(self.cfg, TelegramClient(TOKEN, opener=self.api, sleep=lambda s: None),
                        fetch=lambda select: fixture_fetch(self.mails)(select), llm_call=self.llm,
                        clock=lambda: self.clock[0], sleep=self.sleeps.append, poll_timeout=0,
                        llm_per_hour=self.PER_HOUR)
        self.addCleanup(self.d.close)
        self.d.startup()

    def llm(self, prompt, system_prompt):
        self.prompts.append((prompt, system_prompt))
        return self.canned(prompt, system_prompt)

    def sent(self):
        return self.api.sent()

    def texts(self):
        return [p["text"] for p in self.sent()]

    def calls(self, method):
        return [p for m, p in self.api.calls if m == method]

    def updates(self, *ups):
        self.api.push(*ups)
        return self.d.tg_tick(timeout=0)

    def drain(self):
        n = 0
        while self.d.llm_tick():
            n += 1
        return n


class JobCardTest(DaemonCase):
    MAILS = (CSV, RED_FLAG)

    def card(self):
        self.d.mail_tick(force=True)
        self.assertEqual(self.drain(), 1)                                    # the red-flag job never reaches the LLM
        card = next(p for p in self.sent() if p.get("reply_markup"))
        return card, card["reply_markup"]["inline_keyboard"][0][0]["callback_data"][2:]

    def test_card_prompt_and_proposal(self):
        card, _ = self.card()
        self.assertTrue(card["text"].startswith(f"<b>{CSV_TITLE}</b>"))
        self.assertIn("Score: 9/10 🔥 apply", card["text"])
        self.assertTrue(self.sent()[-1]["text"].startswith("<pre>Hi,\nYou get price lists"))   # proposal after the card
        prompt, system = self.prompts[-1]
        self.assertEqual(system, llm.SYSTEM_PROMPT)
        for s in ("on behalf of Jordan", f"Title: {CSV_TITLE}", "I'm Jordan, a Python developer", "Use the Fit guide"):
            self.assertIn(s, prompt)

    def test_buttons_record_connects_once(self):
        self.updates(message_update(1, "/balance 60", 9))
        card, job_id = self.card()
        self.assertEqual([b["text"] for b in card["reply_markup"]["inline_keyboard"][0]], ["✅ Sent", "⏭ Skip"])
        self.updates(callback_update(2, f"s:{job_id}", card["_id"]))
        picker = self.calls("editMessageReplyMarkup")[-1]["reply_markup"]["inline_keyboard"]
        self.assertEqual([[b["text"] for b in row] for row in picker], [["4", "8", "11"], ["16", "20", "24"], ["↩ Back"]])
        self.updates(callback_update(3, f"c:{job_id}:8", card["_id"]))
        status = self.calls("editMessageReplyMarkup")[-1]["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual((status["text"], status["callback_data"]), ("✅ Sent · 8 Connects · balance 52", "x"))
        self.assertEqual(self.calls("answerCallbackQuery")[-1]["text"], "Recorded: -8 Connects, balance 52")
        self.updates(callback_update(4, f"c:{job_id}:11", card["_id"]), callback_update(5, "x", card["_id"]))
        self.assertEqual([a.get("text") for a in self.calls("answerCallbackQuery")[-2:]],
                         ["Already recorded", "Already recorded"])
        store = self.d.store()
        self.assertEqual(store.balance(), 52)
        p = store.get_proposal(job_id)
        self.assertEqual((p["title"], p["connects"], p["status"]), (CSV_TITLE, 8, "sent"))

    def test_skip_back_and_foreign_presses(self):
        card, job_id = self.card()
        self.updates(callback_update(1, f"s:{job_id}", card["_id"]), callback_update(2, f"b:{job_id}", card["_id"]))
        restored = self.calls("editMessageReplyMarkup")[-1]["reply_markup"]["inline_keyboard"][0]
        self.assertEqual([b["text"] for b in restored], ["✅ Sent", "⏭ Skip"])
        edits = len(self.calls("editMessageReplyMarkup"))
        self.updates(callback_update(3, f"k:{job_id}", card["_id"], user_id=999))      # not the owner
        self.assertEqual(self.calls("answerCallbackQuery")[-1]["text"], "Not available")
        self.assertEqual(len(self.calls("editMessageReplyMarkup")), edits)
        self.updates(callback_update(4, f"k:{job_id}", card["_id"]))
        self.assertEqual(self.calls("editMessageReplyMarkup")[-1]["reply_markup"]["inline_keyboard"][0][0]["text"],
                         "⏭ Skipped")
        self.assertEqual((self.d.store().get_proposal(job_id)["status"], self.d.store().balance()), ("skipped", None))

    def test_note_as_reply_to_a_card_gets_the_job_context(self):
        card, _ = self.card()
        self.updates(message_update(5, "/note thanks, can start monday", message_id=77, reply_to=card["_id"]))
        self.assertEqual(self.calls("sendChatAction")[-1]["action"], "typing")
        self.assertTrue(self.d.llm_tick())
        prompt, system = self.prompts[-1]
        self.assertEqual(system, assist.NOTE_SYSTEM)
        self.assertIn(f"Job: {CSV_TITLE}", prompt)
        self.assertIn("Our proposal draft: Hi,", prompt)
        self.assertIn("thanks, can start monday", prompt)
        out = self.sent()[-1]
        self.assertEqual(out["text"], "<pre>Thanks for the offer, I'll accept it today and can start on Monday. Could "
                                      "you give me read access to both shops' APIs before then? And which products "
                                      "should I map first?</pre>")
        self.assertEqual(out["reply_parameters"]["message_id"], 77)


class CommandsTest(DaemonCase):
    MAILS = (CSV, "events/offer.eml")

    def test_help_balance_pause_resume_stats(self):
        self.updates(message_update(1, "/help", 10))
        self.assertEqual(self.sent()[-1]["text"], HELP_TEXT)
        self.updates(message_update(2, "/balance", 11), message_update(3, "/balance 60", 12),
                     message_update(4, "/balance lots", 13))
        self.assertEqual(self.texts()[-3:], ["Connects balance: not set (/balance N)", "Connects balance: 60",
                                             "Format: /balance 60"])
        self.updates(message_update(5, "/pause", 14))
        self.d.mail_tick(force=True)
        self.drain()
        self.assertTrue(any("<b>Upwork · Offer</b>" in t for t in self.texts()))       # events still delivered
        self.assertFalse(any(p.get("reply_markup") for p in self.sent()))                # no job card while paused
        self.updates(message_update(6, "/resume", 15))
        self.assertEqual(self.sent()[-1]["text"], "Resumed. Jobs in the queue: 1.")
        self.drain()
        self.assertTrue(any(p.get("reply_markup") for p in self.sent()))
        self.updates(message_update(7, "/stats", 16), message_update(8, "/nope", 17))
        self.assertIn("Connects balance: 60", self.texts()[-2])
        self.assertIn("offers 1", self.texts()[-2])
        self.assertEqual(self.texts()[-1], "Unknown command. /help")
        self.assertEqual(self.d.state.get("update_offset"), 9)

    def test_mode_commands_need_text(self):
        self.updates(message_update(1, "/note", 10), message_update(2, "/client   ", 11))
        self.assertTrue(self.texts()[-2].startswith("Write the text after the command, for example: /note "))
        self.assertTrue(self.texts()[-1].startswith("Write the text after the command, for example: /client "))
        self.assertEqual(self.d.store().count_owner_tasks(), 0)

    def test_strangers_are_ignored(self):
        before = len(self.api.calls)
        self.updates(message_update(1, "/stats", 10, chat_id=555), message_update(2, "hello", 11, chat_id=555))
        self.assertEqual([m for m, _ in self.api.calls[before:]], ["getUpdates"])

    def test_old_and_non_text_messages(self):
        old = message_update(1, "Please translate this", 10)
        old["message"]["date"] = int(T0) - 3600                    # sent before the daemon's first start
        new = message_update(2, "/balance", 11)
        new["message"]["date"] = int(T0) + 5
        photo = {"update_id": 3, "message": {"message_id": 12, "chat": {"id": OWNER, "type": "private"},
                                             "photo": [{"file_id": "f", "width": 10, "height": 10}]}}
        self.updates(old, new, photo)
        self.assertEqual(self.d.store().count_owner_tasks(), 0)
        self.assertEqual(self.texts(), ["Connects balance: not set (/balance N)", SEND_TEXT])


class EventsTest(DaemonCase):
    MAILS = ("events/client_message_text.eml", "events/client_message_notext.eml", "events/security_code.eml",
             "events/unknown_newsletter.eml")

    def test_client_message_security_and_unclassified(self):
        st = self.d.mail_tick(force=True)
        self.assertEqual((st["pending_llm"], st["new"], st["event_sent"]), (1, 3, 3))
        texts = self.texts()
        self.assertTrue(any("Open Upwork &gt; Messages and send the client's text here" in t for t in texts))
        sec = next(t for t in texts if "Security e-mail" in t)
        self.assertNotIn("604215", sec)
        other = next(t for t in texts if "E-mail without a rule" in t)
        self.assertIn("Subject: Upwork Community: May highlights", other)
        self.assertEqual(self.drain(), 1)
        prompt, system = self.prompts[-1]
        self.assertEqual(system, assist.CLIENT_SYSTEM)
        self.assertIn("CLIENT TEXT (from Sam K.)", prompt)
        self.assertIn("DRAFT for a human to review", prompt)
        card, reply = self.sent()[-2:]
        self.assertIn("<b>Client wrote:</b>", card["text"])
        self.assertIn("<b>Suggested reply:</b> Yes to both", card["text"])
        self.assertTrue(reply["text"].startswith("<pre>Hi Sam,"))
        self.assertFalse(self.cfg.samples_dir.exists())                                  # samples are opt-in
        self.assertEqual(dict(self.d.mail_tick(force=True)), {})                         # nothing twice

    def test_old_notifications_are_recorded_not_sent(self):
        self.d.store().kv_set("events_since", "2026-05-20T00:00:00Z")                    # the daemon started later
        st = self.d.mail_tick(force=True)
        self.assertEqual(st["baseline"], 4)
        self.assertEqual(self.sent(), [])


class SamplesTest(DaemonCase):
    MAILS = ("events/offer.eml", "events/security_code.eml", CSV)
    SETTINGS = {"save_event_samples": True}

    def test_first_mails_are_kept_when_enabled(self):
        self.d.mail_tick(force=True)
        kinds = sorted(p.name.rsplit("_", 2)[0] for p in (self.cfg.samples_dir / "events").glob("*.eml"))
        self.assertEqual(kinds, ["job_alert", "offer", "security"])


class OwnerTextTest(DaemonCase):
    PER_HOUR = 2

    def test_routing_budget_and_queue(self):
        self.updates(message_update(1, "Can you start tomorrow? We need the bot by Friday.", 20),
                     message_update(2, "Ответь, что начну в понедельник", 21), message_update(3, "12345", 22))
        self.assertIn("No words found", self.texts()[-1])
        self.assertTrue(self.d.llm_tick())
        self.assertEqual(self.prompts[-1][1], assist.CLIENT_SYSTEM)                      # Latin: a client's text
        first, second = self.sent()[-2:]
        self.assertIn("<b>Suggested reply:</b>", first["text"])
        self.assertEqual(first["reply_parameters"]["message_id"], 20)
        self.assertTrue(second["text"].startswith("<pre>"))
        self.assertTrue(self.d.llm_tick())
        self.assertEqual(self.prompts[-1][1], assist.NOTE_SYSTEM)                        # Cyrillic: your own note
        self.updates(message_update(4, "/client One more question about the API", 23))
        self.assertIn("The LLM limit (2 per hour) is used up", self.texts()[-1])
        self.assertFalse(self.d.llm_tick())                                             # queued, no third call
        self.assertEqual(len(self.prompts), 2)
        self.clock[0] += 3601
        self.assertTrue(self.d.llm_tick())
        self.assertEqual(self.prompts[-1][1], assist.CLIENT_SYSTEM)
        self.assertEqual(len(self.prompts), 3)

    def test_broken_llm_call_fails_once_without_looping(self):
        calls = []

        def broken(prompt, system_prompt):
            calls.append(1)
            raise ValueError("boom")
        self.d.llm_call = broken
        self.updates(message_update(1, "/note hello, translate this", 30))
        self.assertTrue(self.d.llm_tick())
        self.assertIn("Could not prepare the text: LLM call error (ValueError)", self.texts()[-1])
        self.assertFalse(self.d.llm_tick())
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.d.budget.used(self.clock[0]), 1)

    def test_missing_cli_is_not_counted(self):
        self.d.llm_call = lambda prompt, system_prompt: (None, llm.NOT_FOUND)
        self.updates(message_update(1, "/note hello", 30))
        self.assertTrue(self.d.llm_tick())
        self.assertIn(f"Could not prepare the text: {llm.NOT_FOUND}", self.texts()[-1])
        self.assertEqual(self.d.budget.used(self.clock[0]), 0)


class PersonaTest(DaemonCase):
    MAILS = (SLACK, LOW_RATE)
    SETTINGS = {"owner_language": "Spanish", "niche_keywords": ("slack bot",), "min_hourly_usd": 60.0}

    def test_settings_reach_rules_and_prompts(self):
        st = self.d.mail_tick(force=True)
        self.assertEqual((st["skip_rule"], st["candidate"]), (2, 0))                     # both below $60/h
        jobs = {j["title"]: j for j in self.d.store().all_jobs()}
        self.assertEqual(jobs["Slack bot that answers HR policy questions with an LLM"]["fit_tags"], ["slack bot"])
        self.updates(message_update(1, "/client Hola, ¿puedes empezar el lunes?", 40))
        self.d.llm_tick()
        prompt = self.prompts[-1][0]
        self.assertIn("translation: faithful Spanish translation", prompt)
        self.assertIn('signed "Jordan"', prompt)


class ResilienceTest(DaemonCase):
    MAILS = (CSV,)

    def test_poll_backoff_and_offset(self):
        self.api.fail["getUpdates"] = [urllib.error.URLError("down"), urllib.error.URLError("down")]
        self.assertEqual(self.d.tg_tick(timeout=0), 0)
        self.assertEqual(self.d.tg_tick(timeout=0), 0)
        self.assertEqual(self.sleeps, [5, 10])
        self.updates({"update_id": 41}, {"update_id": 40})
        self.assertEqual(self.d.state.get("update_offset"), 42)
        self.d.tg_tick(timeout=0)
        self.assertEqual(self.calls("getUpdates")[-1]["offset"], 42)
        self.assertEqual(self.calls("getUpdates")[-1]["allowed_updates"], ["message", "callback_query"])

    def test_telegram_down_means_no_llm_calls_and_ready_card_is_resent(self):
        self.d.mail_tick(force=True)
        self.api.fail["sendMessage"] = [urllib.error.URLError("down")]
        self.assertTrue(self.d.llm_tick())                       # drafted, delivery failed
        self.assertEqual(len(self.prompts), 1)
        self.assertFalse(self.d.llm_tick())                      # backoff: nothing, no new LLM call
        self.clock[0] += 400
        self.d.llm_tick()
        self.assertEqual(len(self.prompts), 1)                   # the stored draft was reused
        self.assertTrue(any(p.get("reply_markup") for p in self.sent()))

    def test_imap_failure_backs_off(self):
        def boom(select):
            raise bot_daemon.mail.MailError("login failed: [AUTHENTICATIONFAILED]")
        self.d.fetch = boom
        self.assertEqual(dict(self.d.mail_tick(force=True)), {})
        self.assertEqual(self.d.state.get("backoff")["imap"]["fails"], 1)
        self.d.fetch = lambda select: (_ for _ in ()).throw(AssertionError("fetched during backoff"))
        self.assertEqual(dict(self.d.mail_tick(force=True)), {})


class ScheduleTickTest(DaemonCase):
    def test_summary_once_a_day(self):
        self.clock[0] = datetime(2026, 5, 14, 20, 59, tzinfo=timezone.utc).timestamp()
        self.assertEqual(self.d.schedule_tick(), [])
        self.clock[0] += 60
        self.assertEqual(self.d.schedule_tick(), ["summary"])
        self.assertIn("<b>Daily summary · 2026-05-14</b>", self.texts()[-1])
        self.assertEqual(self.d.schedule_tick(), [])


class SummaryOffTest(DaemonCase):
    SETTINGS = {"summary_hour": None, "timezone": "Not/AZone"}

    def test_no_summary_and_utc_fallback(self):
        self.assertFalse(self.d.tz_ok)
        self.clock[0] = datetime(2026, 5, 14, 23, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(self.d.schedule_tick(), [])
        self.assertEqual(self.sent(), [])


class BindingTest(DaemonCase):
    OWNER_BOUND = False

    def test_first_private_start_binds(self):
        self.updates(message_update(1, "/start", 10, chat_id=500), message_update(2, "/start", 11, chat_id=600))
        self.assertEqual(self.d.owner(), 500)
        self.assertEqual(statemod.load(self.cfg.state_path)["owner_chat_id"], 500)
        self.assertEqual([p["chat_id"] for p in self.sent()], [500])               # chat 600 got nothing
        self.assertTrue(self.texts()[0].startswith("Ready: job cards"))


class PinnedOwnerTest(DaemonCase):
    OWNER_BOUND = False
    SETTINGS = {"owner_chat_id": 700}

    def test_only_the_pinned_chat_is_served(self):
        self.assertEqual(self.d.owner(), 700)                                          # set at startup
        self.updates(message_update(1, "/start", 10, chat_id=500), message_update(2, "/start", 11, chat_id=700))
        self.assertEqual([p["chat_id"] for p in self.sent()], [700])
        self.assertEqual(self.texts(), [HELP_TEXT])


class SimulateTest(unittest.TestCase):
    def test_simulate_transcript(self):
        root = logging.getLogger()
        saved = (list(root.handlers), root.level)
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(bot_daemon.main(["--simulate"]), 0)
        finally:
            for h in list(root.handlers):
                root.removeHandler(h)
            for h in saved[0]:
                root.addHandler(h)
            root.setLevel(saved[1])
        text = out.getvalue()
        for s in ("buttons: [✅ Sent] [⏭ Skip]", "[4] [8] [11] / [16] [20] [24] / [↩ Back]",
                  "[✅ Sent · 8 Connects · balance 52]", "Upwork · Client message · Sam K.", "Upwork · Offer · Lena W.",
                  "Score: 9/10 🔥 apply", "Score: 7.5/10 🤔 maybe", "Thanks for the offer, I'll accept it today",
                  "Daily summary · 2026-05-14", "Job cards: 2 (filtered by rules 2, by the model 0)",
                  '[rules] skipped "Quick fix for the checkout page of our online store": red flag: contact off-platform',
                  "[end] Connects balance 52"):
            self.assertIn(s, text)
        self.assertNotIn("<b>", text)                                                  # rendered as Telegram shows it


if __name__ == "__main__":
    unittest.main()
