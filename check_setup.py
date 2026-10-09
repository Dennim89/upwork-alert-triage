"""Manual setup check (run it by hand): mailbox login, Telegram getMe, owner chat, profile, Claude CLI.

Prints status lines only. Secret values never appear: every reason passes through redact().
"""
from __future__ import annotations

import sys

from bot import config, llm, mail, telegram
from bot import state as statemod
from bot.lock import SingleInstance


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        cfg = config.load_config()
    except config.ConfigError as e:
        print(f"Config: FAIL ({e})")
        return 1
    secrets = cfg.secrets()
    ok = True
    try:
        mail.check_login(cfg)
        print(f"Mailbox ({cfg.imap_host}): OK")
    except Exception as e:
        ok = False
        print(f"Mailbox ({cfg.imap_host}): FAIL ({config.redact(e, secrets)})")
    try:
        me = telegram.get_me(cfg.telegram_bot_token)
        print(f"Telegram: OK (bot {me.get('username', '?')})")
    except Exception as e:
        print(f"Telegram: FAIL ({config.redact(e, secrets)})")
        print("Owner chat: unknown (Telegram failed)")
        return 1
    try:
        st = statemod.load(cfg.state_path)                     # read-only: safe while the daemon runs
    except statemod.StateError as e:
        print(f"Owner chat: FAIL ({e})")
        return 1
    chat = st.get("owner_chat_id")
    if cfg.owner_chat_id is not None:
        print("Owner chat: pinned by TELEGRAM_OWNER_ID (press Start in the bot from that account)")
    elif chat is None:
        lock = SingleInstance(cfg.lock_path)                   # binding needs getUpdates: never race the daemon
        if not lock.acquire():
            print("Owner chat: not yet (the daemon is running: press Start in the bot, it binds the chat itself)")
            return 1
        try:
            st = statemod.load(cfg.state_path)
            chat = telegram.ensure_owner(cfg.telegram_bot_token, st)
            statemod.save(cfg.state_path, st)
        except Exception as e:
            print(f"Owner chat: FAIL ({config.redact(e, secrets)})")
            return 1
        finally:
            lock.release()
        if chat is None:
            ok = False
            print("Owner chat: not yet (press Start in the bot, then run this check again)")
        else:
            print(f"Owner chat: found ({st.get('owner_name') or 'no name'})")
    else:
        print(f"Owner chat: found ({st.get('owner_name') or 'no name'})")
    proof = llm.load_proof(cfg.profile_path)
    if proof:
        n = len([ln for ln in proof.proof_bank.splitlines() if ln.strip()])
        print(f"Profile: OK ({cfg.profile_path.name}, {n} proof item{'' if n == 1 else 's'})")
    else:
        print(f"Profile: missing or empty ({cfg.profile_path.name}): cards will come without drafts. "
              "Copy profile.example.md and edit it.")
    found = llm.find_claude(cfg.claude_path)
    print("Claude CLI: " + ("found" if found else "not found (cards will say the draft is unavailable)"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
