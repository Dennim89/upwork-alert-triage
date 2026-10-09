import imaplib
import unittest
from datetime import date, datetime, timezone
from unittest import mock

from bot import config, mail, parse
from tests.support import BLANK_RATE, CSV, CSV_TITLE, PW, SLACK, TOKEN, FakeIMAP, raw

CFG = config.Config("me@example.com", PW, TOKEN)
NOW = datetime(2026, 5, 14, 18, 0, tzinfo=timezone.utc)


class DecodeTest(unittest.TestCase):
    def test_alert(self):
        it = mail.decode_message(raw(CSV), uid="7")
        self.assertEqual(it.msgid, "<alert-csv@mail.example.com>")
        self.assertEqual(it.subject, f"New job alert: {CSV_TITLE}")
        self.assertEqual(it.date, datetime(2026, 5, 14, 17, 52, 10, tzinfo=timezone.utc))
        self.assertIn("upwork", it.sender.lower())
        self.assertIn("View job details", it.html)
        self.assertIn("View job details", it.text)
        self.assertIsNotNone(it.message)
        self.assertEqual(it.raw, raw(CSV))

    def test_html_only_plain_only_and_missing_message_id(self):
        self.assertIsNone(mail.decode_message(raw("digest.eml")).text)
        self.assertIsNone(mail.decode_message(raw(BLANK_RATE)).html)
        bare = b"From: Upwork <alerts@example.com>\r\nSubject: New job alert: X\r\n\r\nhello\r\n"
        a, b = mail.decode_message(bare), mail.decode_message(bare)
        self.assertTrue(a.msgid.endswith("@synthetic.local>"))
        self.assertEqual(a.msgid, b.msgid)
        self.assertEqual(mail.decode_message(bare, fallback_msgid="<h@x>").msgid, "<h@x>")

    def test_imap_date_is_locale_free(self):
        self.assertEqual(mail.imap_date(date(2026, 9, 29)), "29-Sep-2026")
        self.assertEqual(mail.imap_date(datetime(2026, 1, 5, 23, 59)), "05-Jan-2026")


class FetchTest(unittest.TestCase):
    def setUp(self):
        self.msgs = {"101": raw("other_profile.eml"), "102": raw(SLACK), "103": raw(CSV)}
        self.imap = FakeIMAP(self.msgs)
        self.known = set()

    def select(self, head):
        return parse.classify(head["subject"]) == "job_alert" and head["msgid"] not in self.known

    def fetch(self, **kw):
        return mail.fetch_upwork(CFG, select=self.select, connect=lambda: self.imap, now=NOW, **kw)

    def test_read_only_and_selective_download(self):
        self.known.add("<alert-slack@mail.example.com>")
        items = self.fetch()
        self.assertEqual([i.msgid for i in items], ["<alert-csv@mail.example.com>"])  # other and known mail skipped
        calls = self.imap.calls
        self.assertIn(("select", '"[Gmail]/All Mail"', True), calls)      # EXAMINE, All Mail
        search = next(c for c in calls if c[:2] == ("uid", "SEARCH"))
        self.assertEqual(search[2:], ("FROM", '"upwork"', "SINCE", "11-May-2026"))
        fetches = [c for c in calls if c[:2] == ("uid", "FETCH")]
        self.assertTrue(all("PEEK" in c[3] for c in fetches))             # never sets \Seen
        self.assertEqual([c[2] for c in fetches if c[3] == "(BODY.PEEK[])"], ["103"])
        self.assertFalse(any(c[0] == "uid" and c[1] in ("STORE", "COPY", "MOVE", "EXPUNGE") for c in calls))
        self.assertEqual(calls[-1], ("logout",))

    def test_newest_first_and_cap(self):
        items = self.fetch(max_bodies=1)
        self.assertEqual([i.uid for i in items], ["103"])

    def test_inbox_fallback_without_all_mail(self):
        self.imap.list_lines = [b'(\\HasNoChildren) "/" "INBOX"']
        self.fetch()
        self.assertIn(("select", "INBOX", True), self.imap.calls)

    def test_configured_host_is_used(self):
        seen = []

        def fake_ssl(host, port, timeout=None):
            seen.append((host, port))
            return self.imap
        cfg = config.Config("me@example.com", PW, TOKEN, imap_host="imap.example.com")
        with mock.patch.object(mail.imaplib, "IMAP4_SSL", fake_ssl):
            mail.fetch_upwork(cfg, select=self.select, now=NOW)
            mail.check_login(cfg)
        self.assertEqual(seen, [("imap.example.com", 993)] * 2)

    def test_login_failure_is_redacted(self):
        self.imap.fail_login = f"[AUTHENTICATIONFAILED] bad password {PW}".encode()
        with self.assertRaises(mail.MailError) as cm:
            self.fetch()
        self.assertIn("AUTHENTICATIONFAILED", str(cm.exception))
        self.assertNotIn(PW, str(cm.exception))
        self.assertEqual(self.imap.calls[-1], ("logout",))

    def test_connect_failure(self):
        def boom():
            raise OSError("getaddrinfo failed")
        with self.assertRaises(mail.MailError):
            mail.fetch_upwork(CFG, connect=boom, now=NOW)
        with self.assertRaises(mail.MailError):
            mail.check_login(CFG, connect=boom)

    def test_uid_after_literal(self):
        data = [(b"1 (BODY[HEADER.FIELDS (MESSAGE-ID)] {20}", b"Message-ID: <a@b>\r\n\r\n"), b" UID 55)",
                (b"2 (UID 56 BODY[HEADER.FIELDS (MESSAGE-ID)] {20}", b"Message-ID: <c@d>\r\n\r\n"), b")"]
        self.assertEqual([u for u, _ in mail._uid_payloads(data)], ["55", "56"])

    def test_imap_protocol_error_mid_session(self):
        class Broken(FakeIMAP):
            def uid(self, command, *args):
                raise imaplib.IMAP4.abort("socket error: EOF")
        self.imap = Broken(self.msgs)
        with self.assertRaises(mail.MailError):
            self.fetch()
        self.assertEqual(self.imap.calls[-1], ("logout",))


if __name__ == "__main__":
    unittest.main()
