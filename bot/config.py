"""Config: a .env file (KEY=VALUE) merged with the process environment, settings, secret redaction for logs.

Values set in the process environment win over the .env file. Relative paths are relative to the project
folder, not to the current directory, so a scheduler or a service manager can start the bot from anywhere.
Secret values are never printed; errors name the key or the path only.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"
REQUIRED_KEYS = ("IMAP_USER", "IMAP_PASSWORD", "TELEGRAM_BOT_TOKEN")
SECRET_KEYS = ("IMAP_PASSWORD", "TELEGRAM_BOT_TOKEN")
OPTIONAL_KEYS = ("IMAP_HOST", "TELEGRAM_OWNER_ID", "PROFILE_FILE", "FREELANCER_NAME", "OWNER_LANGUAGE",
                 "NICHE_KEYWORDS", "MIN_HOURLY_USD", "MIN_FIXED_USD", "SUMMARY_TIMEZONE", "SUMMARY_HOUR",
                 "LLM_MAX_PER_HOUR", "CLAUDE_PATH", "BOT_DATA_DIR", "BOT_LOG_DIR", "SAVE_EVENT_SAMPLES")
KNOWN_KEYS = REQUIRED_KEYS + OPTIONAL_KEYS

LOOKBACK_DAYS = 3          # mail window; queued candidates older than this expire
LLM_TIMEOUT_S = 180
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
POLL_TIMEOUT_S = 50        # Telegram getUpdates long poll
MAIL_INTERVAL_S = 180      # mailbox pass every 3 minutes
EVENTS_BACKFILL_H = 24     # at first start, Upwork notifications older than this are recorded but not sent

_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TG_TOKEN_RE = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{20,}")
_LANGUAGE_RE = re.compile(r"[^\W\d_]+(?:[ ()-]+[^\W\d_]+)*\)?")
_HOST_RE = re.compile(r"[A-Za-z0-9.-]{1,253}")
_FLAG_OFF = ("0", "no", "false", "off")
_SUMMARY_OFF = ("off", "none", "no", "false", "-1")


class ConfigError(Exception):
    """Bad or missing config; the text names keys and paths only, never values."""


@dataclass(frozen=True)
class Config:
    imap_user: str
    imap_password: str = field(repr=False)
    telegram_bot_token: str = field(repr=False)
    imap_host: str = "imap.gmail.com"
    owner_chat_id: int | None = None                  # TELEGRAM_OWNER_ID pins the owner chat
    data_dir: Path = BASE_DIR / "data"
    logs_dir: Path = BASE_DIR / "logs"
    profile_path: Path = BASE_DIR / "profile.md"
    freelancer_name: str = ""                         # sign-off of drafts; empty = unsigned
    owner_language: str = "English"                   # language of the model's notes and translations
    niche_keywords: tuple[str, ...] = ()              # empty = rules.DEFAULT_NICHE
    min_hourly_usd: float = 15.0
    min_fixed_usd: float = 30.0
    timezone: str = "UTC"                             # IANA name for the daily summary
    summary_hour: int | None = 21                     # None = no daily summary
    llm_max_per_hour: int = 20
    claude_path: str = ""
    save_event_samples: bool = False                  # keep raw copies of the first mails of each event type

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobs.db"

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "run.lock"

    @property
    def llm_dir(self) -> Path:
        return self.data_dir / "llm_sandbox"

    @property
    def samples_dir(self) -> Path:
        return self.data_dir / "samples"

    def secrets(self) -> list[str]:
        pw = self.imap_password
        spaced = " ".join(pw[i:i + 4] for i in range(0, len(pw), 4))   # the form Google displays
        return [s for s in (pw, spaced, self.telegram_bot_token) if s]


def parse_env_text(text: str) -> dict[str, str]:
    """KEY=VALUE lines; '#' comments and blank lines ignored; optional 'export ' and matching quotes."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _KEY_RE.fullmatch(key):
            continue                                   # malformed: skipped silently, it may hold a secret
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def load_env(path: Path | str = ENV_PATH) -> dict[str, str]:
    """KEY=VALUE pairs of an env file; {} when the file does not exist."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise ConfigError(f"env file unreadable: {path} ({e.__class__.__name__})") from None
    try:
        text = raw.decode("utf-16") if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ConfigError(f"env file is not UTF-8 text: {path}") from None
    return parse_env_text(text)


def _path(value: str, default: Path) -> Path:
    if not value:
        return default
    p = Path(value).expanduser()
    return p if p.is_absolute() else BASE_DIR / p


def _number(values: dict, key: str, default: float, lo: float, hi: float, kind=float):
    raw = values.get(key, "").strip()
    if not raw:
        return default
    try:
        v = kind(raw)
    except ValueError:
        raise ConfigError(f"{key} must be a number") from None
    if not lo <= v <= hi:
        raise ConfigError(f"{key} must be between {lo:g} and {hi:g}")
    return v


def _flag(values: dict, key: str) -> bool:
    raw = values.get(key, "").strip().lower()
    if raw in ("", *_FLAG_OFF):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    raise ConfigError(f"{key} must be 1 or 0")


def _summary_hour(values: dict) -> int | None:
    raw = values.get("SUMMARY_HOUR", "").strip().lower()
    if raw in _SUMMARY_OFF:
        return None
    return int(_number(values, "SUMMARY_HOUR", 21, 0, 23, int))


def _owner_id(values: dict) -> int | None:
    raw = values.get("TELEGRAM_OWNER_ID", "").strip()
    if not raw:
        return None
    if not re.fullmatch(r"\d{1,15}", raw):
        raise ConfigError("TELEGRAM_OWNER_ID must be the numeric id of your private chat with the bot")
    return int(raw)


def _language(values: dict) -> str:
    raw = " ".join(values.get("OWNER_LANGUAGE", "").split())
    if not raw:
        return "English"
    if len(raw) > 40 or not _LANGUAGE_RE.fullmatch(raw):
        raise ConfigError("OWNER_LANGUAGE must be a language name such as English or Spanish")
    return raw


def load_config(env_path: Path | str | None = ENV_PATH, environ: Mapping[str, str] | None = None) -> Config:
    """Settings from the env file plus the process environment (non-empty environment values win)."""
    values = load_env(env_path) if env_path is not None else {}
    env = os.environ if environ is None else environ
    for key in KNOWN_KEYS:
        if str(env.get(key, "")).strip():
            values[key] = str(env[key])
    missing = [k for k in REQUIRED_KEYS if not values.get(k, "").strip()]
    if missing:
        s = "s" if len(missing) > 1 else ""
        where = Path(env_path).name if env_path is not None else ".env"
        them = "them" if s else "it"
        raise ConfigError(f"missing key{s}: {', '.join(missing)}; set {them} in {where} or in the environment")
    host = values.get("IMAP_HOST", "").strip() or "imap.gmail.com"
    if not _HOST_RE.fullmatch(host):
        raise ConfigError("IMAP_HOST must be a host name such as imap.gmail.com")
    data_dir = _path(values.get("BOT_DATA_DIR", "").strip(), BASE_DIR / "data")
    return Config(
        imap_user=values["IMAP_USER"].strip(),
        imap_password="".join(values["IMAP_PASSWORD"].split()),         # Google shows it as 4x4 groups
        telegram_bot_token=values["TELEGRAM_BOT_TOKEN"].strip(),
        imap_host=host,
        owner_chat_id=_owner_id(values),
        data_dir=data_dir,
        logs_dir=_path(values.get("BOT_LOG_DIR", "").strip(), BASE_DIR / "logs"),
        profile_path=_path(values.get("PROFILE_FILE", "").strip(), BASE_DIR / "profile.md"),
        freelancer_name=" ".join(values.get("FREELANCER_NAME", "").split())[:60],
        owner_language=_language(values),
        niche_keywords=tuple(w for w in (" ".join(p.split()) for p in values.get("NICHE_KEYWORDS", "").split(","))
                             if w)[:50],
        min_hourly_usd=_number(values, "MIN_HOURLY_USD", 15.0, 0, 10_000),
        min_fixed_usd=_number(values, "MIN_FIXED_USD", 30.0, 0, 1_000_000),
        timezone=values.get("SUMMARY_TIMEZONE", "").strip() or "UTC",
        summary_hour=_summary_hour(values),
        llm_max_per_hour=int(_number(values, "LLM_MAX_PER_HOUR", 20, 1, 1000, int)),
        claude_path=values.get("CLAUDE_PATH", "").strip(),
        save_event_samples=_flag(values, "SAVE_EVENT_SAMPLES"),
    )


def redact(text: object, secrets: list[str] | tuple[str, ...] = ()) -> str:
    """Replace known secret values and anything shaped like a Telegram bot token."""
    s = str(text)
    for sec in sorted({x for x in secrets if x and len(x) >= 4}, key=len, reverse=True):
        s = s.replace(sec, "***")
    return _TG_TOKEN_RE.sub("<token>", s)


class RedactingFormatter(logging.Formatter):
    """Formats (message + traceback) first, then redacts the whole line."""

    def __init__(self, fmt: str = LOG_FORMAT, secrets: list[str] | tuple[str, ...] = ()):
        super().__init__(fmt)
        self._secrets = list(secrets)

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record), self._secrets)


def setup_logging(logs_dir: Path | None, secrets: list[str] | tuple[str, ...] = (), console: bool = True,
                  level: int = logging.INFO, filename: str = "daemon.log") -> None:
    """Root logger -> logs/<filename> (1 MB x 5 rotation) and stderr when there is one (pythonw has none)."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    root.setLevel(level)
    fmt = RedactingFormatter(LOG_FORMAT, secrets)
    if logs_dir is not None:
        Path(logs_dir).mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(Path(logs_dir) / filename, maxBytes=1_000_000,
                                                  backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    if console and sys.stderr is not None:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        root.addHandler(sh)
