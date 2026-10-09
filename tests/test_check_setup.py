import contextlib
import dataclasses
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import check_setup
from bot import config, mail
from bot import state as statemod
from bot.lock import SingleInstance
from tests.support import PW, TOKEN, FakeTelegram, make_config

ME = {"ok": True, "result": {"id": 1, "is_bot": True, "username": "my_test_helper"}}
START = {"ok": True, "result": [{"update_id": 3, "message": {"message_id": 1, "text": "/start",
                                                             "chat": {"id": 55, "type": "private", "username": "jo"}}}]}
EMPTY = {"ok": True, "result": []}


class CheckSetupTest(unittest.TestCase):
    """Offline: config, IMAP login and urlopen are faked; only the printed lines are checked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.cfg = make_config(Path(self.tmp.name), profile_path=config.BASE_DIR / "profile.example.md")

    def run_check(self, tg_responses, login_exc=None, cfg_exc=None, claude="/opt/fake/claude", cfg=None):
        out = io.StringIO()
        load = mock.patch.object(check_setup.config, "load_config", side_effect=cfg_exc, return_value=cfg or self.cfg)
        tg = FakeTelegram(tg_responses)
        with load, mock.patch.object(check_setup.mail, "check_login", side_effect=login_exc), \
                mock.patch("urllib.request.urlopen", tg), \
                mock.patch.object(check_setup.llm, "find_claude", return_value=claude), contextlib.redirect_stdout(out):
            code = check_setup.main()
        self.requests = [m for m, _, _ in tg.requests]
        return code, out.getvalue().splitlines()

    def test_all_ok(self):
        code, lines = self.run_check([ME, START])
        self.assertEqual(lines, ["Mailbox (imap.gmail.com): OK", "Telegram: OK (bot my_test_helper)",
                                 "Owner chat: found (jo)", "Profile: OK (profile.example.md, 4 proof items)",
                                 "Claude CLI: found"])
        self.assertEqual(code, 0)
        self.assertEqual(statemod.load(self.cfg.state_path)["owner_chat_id"], 55)

    def test_while_the_daemon_holds_the_lock(self):
        daemon_lock = SingleInstance(self.cfg.lock_path)
        self.assertTrue(daemon_lock.acquire())
        self.addCleanup(daemon_lock.release)
        code, lines = self.run_check([ME, START])                    # unbound: no getUpdates race with the daemon
        self.assertEqual(lines[2], "Owner chat: not yet (the daemon is running: press Start in the bot, it binds "
                                   "the chat itself)")
        self.assertEqual(self.requests, ["getMe"])
        statemod.save(self.cfg.state_path, {"owner_chat_id": 55, "owner_name": "jo"})
        code, lines = self.run_check([ME])                           # bound: read-only, no getUpdates
        self.assertEqual((code, lines[2], self.requests), (0, "Owner chat: found (jo)", ["getMe"]))

    def test_pinned_owner_and_missing_profile(self):
        cfg = dataclasses.replace(self.cfg, owner_chat_id=55, profile_path=Path(self.tmp.name) / "profile.md")
        code, lines = self.run_check([ME], cfg=cfg)
        self.assertEqual(lines[2], "Owner chat: pinned by TELEGRAM_OWNER_ID (press Start in the bot from that account)")
        self.assertTrue(lines[3].startswith("Profile: missing or empty (profile.md)"))
        self.assertEqual((code, self.requests), (0, ["getMe"]))

    def test_failures_are_reported_without_secrets(self):
        code, lines = self.run_check([ME, EMPTY], login_exc=mail.MailError(f"login failed: bad {PW}"), claude=None)
        self.assertEqual(lines[0], "Mailbox (imap.gmail.com): FAIL (login failed: bad ***)")
        self.assertEqual(lines[2], "Owner chat: not yet (press Start in the bot, then run this check again)")
        self.assertEqual(lines[4], "Claude CLI: not found (cards will say the draft is unavailable)")
        self.assertEqual(code, 1)
        code, lines = self.run_check([{"ok": False, "error_code": 401, "description": "Unauthorized"}])
        self.assertEqual(lines[1:], ["Telegram: FAIL (getMe: 401 Unauthorized)", "Owner chat: unknown (Telegram failed)"])
        self.assertTrue(all(TOKEN not in ln and PW not in ln for ln in lines))
        code, lines = self.run_check([], cfg_exc=config.ConfigError("missing key: TELEGRAM_BOT_TOKEN"))
        self.assertEqual(lines, ["Config: FAIL (missing key: TELEGRAM_BOT_TOKEN)"])


if __name__ == "__main__":
    unittest.main()
