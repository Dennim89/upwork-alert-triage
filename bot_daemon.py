"""Entry point: one long-lived process - Telegram long polling + Upwork mail every 3 min + LLM queue.

Holds data/run.lock for its whole life, so a second copy (or check_setup.py binding the owner chat) cannot
read Telegram updates at the same time.

  python bot_daemon.py              run (reads .env and the environment; logs to logs/daemon.log)
  python bot_daemon.py --simulate   offline demo: fake Telegram / mailbox / LLM, prints the transcript
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from bot import config, llm, mail, simulate
from bot import state as statemod
from bot.daemon import Daemon
from bot.lock import SingleInstance
from bot.telegram import TelegramClient

LOCK_WAIT_S = 60           # a previous copy may still be shutting down; then exit non-zero (the scheduler retries)
log = logging.getLogger("bot_daemon")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Upwork job-alert e-mails -> Telegram cards with draft proposals.")
    ap.add_argument("--simulate", action="store_true", help="offline transcript with fake Telegram/mailbox/LLM")
    ap.add_argument("--env", default=str(config.ENV_PATH), help="path to the .env file")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    if args.simulate:
        config.setup_logging(None, console=True, level=logging.WARNING)
        return simulate.run()
    try:
        cfg = config.load_config(args.env)
    except config.ConfigError as e:
        config.setup_logging(config.BASE_DIR / "logs")
        log.error("config: %s", e)
        return 1
    config.setup_logging(cfg.logs_dir, cfg.secrets(), level=logging.DEBUG if args.verbose else logging.INFO)
    lock = SingleInstance(cfg.lock_path)
    deadline = time.time() + LOCK_WAIT_S
    while not lock.acquire():
        if time.time() > deadline:
            log.error("lock %s busy for %d s (another copy is running); exit", cfg.lock_path, LOCK_WAIT_S)
            return 3
        time.sleep(5)
    log.info("daemon starting (lock held)")
    try:
        daemon = Daemon(cfg, TelegramClient(cfg.telegram_bot_token),
                        fetch=lambda select: mail.fetch_upwork(cfg, select=select),
                        llm_call=lambda prompt, system_prompt: llm.call_claude(
                            prompt, system_prompt=system_prompt, cwd=cfg.llm_dir, claude_path=cfg.claude_path or None))
        daemon.run()
        return 0
    except statemod.StateError as e:
        log.error("state: %s", e)
        return 1
    except Exception:
        log.exception("daemon crashed")
        return 2
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
