import tempfile
import unittest
from pathlib import Path

from bot import events
from tests.support import CSV, item

EXPECTED = {
    "client_message_text.eml": "client_message", "client_message_notext.eml": "client_message",
    "proposal_viewed.eml": "proposal_viewed", "interview_invite.eml": "interview_invite", "offer.eml": "offer",
    "contract_started.eml": "contract_started", "milestone_funded.eml": "milestone_funded", "payment.eml": "payment",
    "proposal_declined.eml": "proposal_declined", "job_closed.eml": "proposal_declined",
    "security_code.eml": "security", "unknown_newsletter.eml": "other",
}
SYNC = "Inventory sync between two web shops"


def ev(name: str) -> tuple:
    it = item(f"events/{name}" if name in EXPECTED else name)
    text, anchors = events.mail_text(it)
    kind = events.classify_event(it.subject, text)
    return it, kind, events.build_event(kind, it.subject, text, anchors)


class ClassifyTest(unittest.TestCase):
    def test_every_fixture_class(self):
        for name, kind in EXPECTED.items():
            self.assertEqual(ev(name)[1], kind, name)
        for name, kind in ((CSV, "job_alert"), ("digest.eml", "digest"), ("other_profile.eml", "other")):
            self.assertEqual(ev(name)[1], kind, name)

    def test_tricky_subjects(self):
        cases = {
            "New job alert: Login page for our app": "job_alert",          # a job alert wins over 'login'
            "Your profile is now visible to clients": "other",
            "Special offer: 20% off Connects": "account",                  # marketing about Connects, not a job offer
            "Payment Received - Thank You!": "account",                    # your own billing, not client money
            "A new card was added to your account": "account",
            "Welcome to Freelancer Plus": "account",
            "A security question has been added or changed to your account": "account",
            "Your profile is approved": "account",
            "Message from Lena: let's connect tomorrow": "client_message",   # singular 'connect' is not Connects
            "Someone viewed your profile": "other",
            "Your contract has ended": "other",
            "You have 2 unread messages": "client_message",
            "Your Upwork verification code": "security",
            "Security alert: new sign-in to your account": "security",
            "Jobs picked for you": "digest",
            "Acme Labs hired you for Data pipeline": "contract_started",
            "Offer: Telegram bot": "offer",
            "Invitation to interview: Python scraper": "interview_invite",
            "Milestone 2 funded": "milestone_funded",
            "You've been paid $100.00": "payment",
            "The job Python scraper was closed": "proposal_declined",
        }
        for subject, kind in cases.items():
            self.assertEqual(events.classify_event(subject, ""), kind, subject)

    def test_body_fallback_rules(self):
        self.assertEqual(events.classify_event("Upwork notification", "Sam sent you a new message"), "client_message")
        self.assertEqual(events.classify_event("Update", "A client viewed your proposal today"), "proposal_viewed")
        self.assertEqual(events.classify_event("Update", "nothing to see"), "other")

    def test_rules_table_is_the_single_source(self):
        kinds = {k for k, _, _ in events.EVENT_RULES}
        self.assertEqual(kinds - set(events.LABELS), {"job_alert"})
        self.assertEqual(set(events.LABELS), set(events.HINTS))


class ExtractTest(unittest.TestCase):
    def test_client_message_with_text(self):
        _, kind, e = ev("client_message_text.eml")
        self.assertEqual((e["client"], e["title"]), ("Sam K.", "Telegram bot that posts daily sales numbers"))
        self.assertTrue(e["body"].startswith("Thanks for the proposal. Two questions"))
        self.assertTrue(e["body"].endswith("instead of a cloud service?"))
        self.assertNotIn("View message", e["body"])
        self.assertNotIn("Job:", e["body"])
        self.assertEqual(e["link"], "https://click.example.com/t/client_message_text/action")

    def test_client_message_without_text(self):
        _, _, e = ev("client_message_notext.eml")
        self.assertIsNone(e["body"])
        self.assertEqual((e["client"], e["title"]), ("Sam K.", "Telegram bot that posts daily sales numbers"))

    def test_known_events(self):
        expected = {"offer.eml": ("Lena W.", SYNC), "payment.eml": ("Lena W.", SYNC),
                    "milestone_funded.eml": ("Lena W.", SYNC), "interview_invite.eml": ("Lena W.", SYNC),
                    "contract_started.eml": ("Lena W.", SYNC), "proposal_viewed.eml": ("Lena W.", SYNC),
                    "proposal_declined.eml": (None, "Weather data scraper for a farm co-op"),
                    "job_closed.eml": (None, "Dashboard for a small solar installer")}
        for name, (client, title) in expected.items():
            _, _, e = ev(name)
            self.assertEqual((e["client"], e["title"]), (client, title), name)
        _, _, offer = ev("offer.eml")
        self.assertIn("$700.00", offer["body"])

    def test_security_and_digest_keep_no_personal_fields(self):
        it, kind, e = ev("security_code.eml")
        self.assertEqual((kind, e["body"], e["client"]), ("security", None, None))
        card = events.event_card(dict(e, kind=kind, subject=it.subject))
        self.assertNotIn("604215", card)
        self.assertIn("The text is not forwarded", card)
        _, kind, e = ev("digest.eml")
        self.assertEqual((kind, e["client"], e["title"]), ("digest", None, None))


class CardTest(unittest.TestCase):
    def test_event_card_and_unclassified(self):
        it, kind, e = ev("offer.eml")
        card = events.event_card(dict(e, kind=kind, subject=it.subject))
        for s in ("<b>Upwork · Offer</b> · Lena W.", f"Job: {SYNC}", "Open in Upwork",
                  "What to do: Before you accept, check: amount", "not a request to leave Upwork", "$700.00"):
            self.assertIn(s, card)
        it, kind, e = ev("unknown_newsletter.eml")
        card = events.event_card(dict(e, kind=kind, subject=it.subject))
        self.assertIn("Subject: Upwork Community: May highlights", card)
        self.assertIn("An Upwork e-mail type without a rule", card)
        self.assertLessEqual(len(e["body"]), events.EXCERPT_CHARS + 1)
        self.assertTrue(e["body"].endswith("…"))
        _, kind, e = ev("milestone_funded.eml")
        self.assertIn("The milestone is funded: you can start the work.", events.event_card(dict(e, kind=kind)))

    def test_client_message_cards(self):
        it, kind, e = ev("client_message_notext.eml")
        card = events.event_card(dict(e, kind=kind, subject=it.subject))
        self.assertIn("Open Upwork &gt; Messages and send the client's text here", card)
        it, kind, e = ev("client_message_text.eml")
        res = {"translation": "", "summary": "short <summary>", "reply_en": "Hi & bye\nJordan"}
        cards = events.client_message_cards(dict(e, subject=it.subject), res)
        self.assertEqual(len(cards), 2)
        self.assertIn("<b>Client wrote:</b>\nThanks for the proposal.", cards[0])
        self.assertIn("<b>Suggested reply:</b> short &lt;summary&gt;", cards[0])
        self.assertEqual(cards[1], "<pre>Hi &amp; bye\nJordan</pre>")
        translated = events.client_message_cards(dict(e, subject=it.subject), dict(res, translation="Gracias <b>"))
        self.assertIn("<b>Translation:</b>\nGracias &lt;b&gt;", translated[0])
        self.assertNotIn("Client wrote:", translated[0])
        fallback = events.client_message_cards(dict(e, subject=it.subject), None, "timeout after 180 s")
        self.assertEqual(len(fallback), 1)
        self.assertIn("No draft: timeout after 180 s.", fallback[0])
        self.assertIn("Thanks for the proposal", fallback[0])

    def test_excerpt(self):
        text = "Find Work Freelancer Plus Log In\nHello\n" + "word " * 300 + "\nUnsubscribe here\n© 2026 Example"
        ex = events.excerpt(text)
        self.assertTrue(ex.startswith("Hello"))
        self.assertTrue(ex.endswith("…"))
        self.assertLessEqual(len(ex), events.EXCERPT_CHARS + 1)
        self.assertEqual(events.excerpt("Hi\n\nUnsubscribe from these emails"), "Hi")


class SampleTest(unittest.TestCase):
    def test_first_mails_of_each_class_only(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            saved = [events.save_sample(f"mail {i}".encode(), "offer", d, f"2026051{i}") for i in range(5)]
            self.assertEqual(sum(p is not None for p in saved), events.SAMPLES_PER_CLASS)
            self.assertIsNone(events.save_sample(b"mail 0", "offer", d, "20260510"))
            self.assertIsNone(events.save_sample(None, "payment", d, "x"))
            others = [events.save_sample(f"o{i}".encode(), "other", d, f"s{i}") for i in range(35)]
            self.assertEqual(sum(p is not None for p in others), events.SAMPLES_OTHER)
            self.assertEqual(len(list((Path(d) / "events").glob("offer_*.eml"))), 3)
            blocker = Path(d) / "file"
            blocker.write_text("x")
            self.assertIsNone(events.save_sample(b"raw", "offer", blocker, "x"))     # unwritable: no crash


if __name__ == "__main__":
    unittest.main()
