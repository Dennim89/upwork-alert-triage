"""Shared fakes for tests: fixtures loader, fake IMAP server, fake urlopen for Telegram, fake CLI runner."""
from __future__ import annotations

import imaplib
import io
import json
import logging
import subprocess
import unittest
from pathlib import Path

from bot import config, mail

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TOKEN = "123456:fake-token-for-tests-only-000"   # token-shaped, so the redaction rules apply to it
PW = "abcdefghijklmnop"                           # app-password-shaped
CSV, SLACK, RED_FLAG, LOW_RATE, BLANK_RATE = ("alert_csv.eml", "alert_slack.eml", "alert_red_flag.eml",
                                              "alert_low_rate.eml", "alert_blank_rate.eml")
CSV_TITLE = "Python script to merge supplier price lists into one CSV"
SLACK_TITLE = "Slack bot that answers HR policy questions with an LLM"


def raw(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def item(name: str, uid: str = "1") -> mail.MailItem:
    return mail.decode_message(raw(name), uid=uid)


def make_config(base: Path, **kw) -> config.Config:
    return config.Config("me@example.com", PW, TOKEN, data_dir=base / "data", logs_dir=base / "logs", **kw)


def headers_of(raw_msg: bytes) -> bytes:
    for sep in (b"\r\n\r\n", b"\n\n"):
        i = raw_msg.find(sep)
        if i >= 0:
            return raw_msg[:i + len(sep)]
    return raw_msg


class FakeIMAP:
    """Serves canned UID SEARCH / FETCH answers for {uid: raw message}; records every call."""

    ALL_MAIL = [b'(\\HasNoChildren) "/" "INBOX"', b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"']

    def __init__(self, messages: dict[str, bytes], list_lines=None, fail_login: bytes | None = None):
        self.messages = messages
        self.list_lines = self.ALL_MAIL if list_lines is None else list_lines
        self.fail_login = fail_login
        self.calls: list[tuple] = []

    def login(self, user, password):
        self.calls.append(("login", user))
        if self.fail_login is not None:
            raise imaplib.IMAP4.error(self.fail_login)
        return "OK", [b"logged in"]

    def list(self):
        self.calls.append(("list",))
        return "OK", list(self.list_lines)

    def select(self, mailbox, readonly=False):
        self.calls.append(("select", mailbox, readonly))
        return "OK", [str(len(self.messages)).encode()]

    def uid(self, command, *args):
        self.calls.append(("uid", command) + args)
        if command == "SEARCH":
            return "OK", [" ".join(self.messages).encode()]
        if command == "FETCH":
            uids, items = args
            data = []
            for n, u in enumerate(uids.split(","), 1):
                body = headers_of(self.messages[u]) if "HEADER.FIELDS" in items else self.messages[u]
                data += [(f"{n} (UID {u} BODY[] {{{len(body)}}}".encode(), body), b")"]
            return "OK", data
        raise AssertionError(f"unexpected IMAP command {command}")

    def logout(self):
        self.calls.append(("logout",))
        return "BYE", [b"bye"]


class FakeTelegram:
    """urlopen stand-in: records (method, params, url); answers from a queue (dict -> JSON body, or raises)."""

    def __init__(self, responses=None):
        self.requests: list[tuple[str, dict, str]] = []
        self.responses = list(responses or [])

    def __call__(self, req, timeout=None):
        method = req.full_url.rsplit("/", 1)[-1]
        self.requests.append((method, json.loads(req.data.decode("utf-8")), req.full_url))
        resp = self.responses.pop(0) if self.responses else {"ok": True, "result": {"message_id": len(self.requests)}}
        if isinstance(resp, BaseException):
            raise resp
        return io.BytesIO(json.dumps(resp).encode("utf-8"))


class FakeRunner:
    """subprocess.run stand-in for the claude CLI."""

    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0, exc: BaseException | None = None):
        self.stdout, self.stderr, self.returncode, self.exc = stdout, stderr, returncode, exc
        self.args = None
        self.kw: dict = {}

    def __call__(self, args, **kw):
        self.args, self.kw = args, kw
        if self.exc is not None:
            raise self.exc
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, self.stderr)


def canned_stdout(name: str = "llm_job_csv.json") -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class LoggingGuard(unittest.TestCase):
    """For tests that call setup_logging(): restores the root logger and the 'bot' level afterwards."""

    def setUp(self):
        root, botlog = logging.getLogger(), logging.getLogger("bot")
        saved = (list(root.handlers), root.level, botlog.level)
        botlog.setLevel(logging.NOTSET)                       # these tests read the real log file

        def restore():
            for h in list(root.handlers):
                root.removeHandler(h)
                if h not in saved[0]:
                    h.close()
            for h in saved[0]:
                root.addHandler(h)
            root.setLevel(saved[1])
            botlog.setLevel(saved[2])
        self.addCleanup(restore)
