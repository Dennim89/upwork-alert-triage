"""bot_daemon.py wiring and the real thread loop, offline: config, IMAP, Telegram transport and the CLI are faked."""
import contextlib
import io
import logging
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import bot_daemon
from bot import config
from bot import state as statemod
from bot.daemon import Daemon
from bot.lock import SingleInstance
from bot.simulate import FakeBotApi, canned_llm, fixture_fetch
from bot.telegram import TelegramClient
from tests.support import CSV, CSV_TITLE, PW, TOKEN, LoggingGuard, make_config


class Base(LoggingGuard):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.cfg = make_config(Path(self.tmp.name), profile_path=config.BASE_DIR / "profile.example.md",
                               claude_path="/opt/fake/claude")


class MainWiringTest(Base):
    def test_real_dependencies_are_wired_and_the_lock_is_held(self):
        seen = {}

        class FakeDaemon:
            def __init__(self, cfg, client, *, fetch, llm_call):
                seen.update(cfg=cfg, token=client.token, fetch=fetch, llm_call=llm_call)

            def run(inner):
                seen["lock_free_during_run"] = SingleInstance(self.cfg.lock_path).acquire()
                seen["fetched"] = seen["fetch"](lambda head: True)
                seen["llm"] = seen["llm_call"]("PROMPT", "SYSTEM")
                logging.getLogger("bot.daemon").info("hello with %s inside", PW)

        fetch_calls, cli_calls = [], []
        with mock.patch.object(bot_daemon.config, "load_config", return_value=self.cfg), \
                mock.patch.object(bot_daemon, "Daemon", FakeDaemon), \
                mock.patch.object(bot_daemon.mail, "fetch_upwork",
                                  lambda cfg, select: fetch_calls.append((cfg, select("h"))) or ["item"]), \
                mock.patch.object(bot_daemon.llm, "call_claude",
                                  lambda prompt, **kw: cli_calls.append((prompt, kw)) or ("text", None)), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(bot_daemon.main([]), 0)
        self.assertEqual(seen["token"], TOKEN)
        self.assertFalse(seen["lock_free_during_run"])
        self.assertEqual((seen["fetched"], fetch_calls[0][0], fetch_calls[0][1]), (["item"], self.cfg, True))
        self.assertEqual(seen["llm"], ("text", None))
        self.assertEqual(cli_calls[0], ("PROMPT", {"system_prompt": "SYSTEM", "cwd": self.cfg.llm_dir,
                                                   "claude_path": "/opt/fake/claude"}))
        probe = SingleInstance(self.cfg.lock_path)
        self.assertTrue(probe.acquire())                                       # released after run()
        probe.release()
        log_text = (self.cfg.logs_dir / "daemon.log").read_text(encoding="utf-8")
        self.assertIn("daemon starting", log_text)
        self.assertIn("hello with *** inside", log_text)                      # secret redacted, line kept
        self.assertNotIn(PW, log_text)

    def test_busy_lock_and_config_error_exit_non_zero(self):
        holder = SingleInstance(self.cfg.lock_path)
        self.assertTrue(holder.acquire())
        self.addCleanup(holder.release)
        with mock.patch.object(bot_daemon.config, "load_config", return_value=self.cfg), \
                mock.patch.object(bot_daemon, "LOCK_WAIT_S", 0), mock.patch.object(bot_daemon.time, "sleep"), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(bot_daemon.main([]), 3)
        with mock.patch.object(bot_daemon.config, "load_config", side_effect=config.ConfigError("missing key: X")), \
                mock.patch.object(bot_daemon.config, "BASE_DIR", Path(self.tmp.name)), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(bot_daemon.main([]), 1)
        self.assertIn("missing key: X", (Path(self.tmp.name) / "logs" / "daemon.log").read_text(encoding="utf-8"))


class ThreadedRunTest(Base):
    def test_threads_deliver_a_card_and_stop_cleanly(self):
        statemod.save(self.cfg.state_path, {"owner_chat_id": 1001, "owner_name": "jordan"})
        api = FakeBotApi()
        lock = threading.Lock()
        original = api.__call__

        def locked(req, timeout=None):
            with lock:
                return original(req, timeout)
        fixture_time = datetime(2026, 5, 14, 18, 0, tzinfo=timezone.utc).timestamp()   # the alert is fresh then
        d = Daemon(self.cfg, TelegramClient(TOKEN, opener=locked, sleep=lambda s: None),
                   fetch=fixture_fetch([CSV]), llm_call=canned_llm(), poll_timeout=0, clock=lambda: fixture_time)
        runner = threading.Thread(target=d.run, daemon=True)
        runner.start()
        deadline = time.time() + 10
        while time.time() < deadline and not any(p.get("reply_markup") for p in api.sent()):
            time.sleep(0.05)
        d.stop()
        runner.join(timeout=20)
        self.assertFalse(runner.is_alive())
        card = next((p for p in api.sent() if p.get("reply_markup")), None)
        self.assertIsNotNone(card, "job card was not delivered by the worker threads")
        self.assertIn(CSV_TITLE, card["text"])
        self.assertTrue(any(m == "getUpdates" for m, _ in api.calls))


if __name__ == "__main__":
    unittest.main()
