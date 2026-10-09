# upwork-alert-triage

Reads your Upwork job-alert e-mails, scores each job and sends you a Telegram card with a draft proposal.
You read the card, open the job and send the proposal on Upwork yourself. The bot never opens upwork.com and
never submits anything.

It is for freelancers who already get Upwork saved-search alerts by e-mail and want to sort them on the
phone, with a first draft ready. Python standard library only. Not affiliated with Upwork.

**Status: experimental.** It comes from a tool one freelancer wrote for himself. This public version passes
its offline tests and the demo below, but it has not yet been run against a real mailbox, Telegram bot or
Claude CLI. Main limits: it depends on the layout of Upwork's alert e-mails, it needs the Claude Code CLI for
drafts, and the alert e-mail does not show the number of proposals or the Connects price.
See [Limitations](#limitations).

A job card and its draft, from `python bot_daemon.py --simulate` (synthetic e-mail, canned model answer):

<!-- sample-output:start -->
```text
[bot -> you] message #106
Python script to merge supplier price lists into one CSV
Open the job: https://click.example.com/t/csv/view
Search: "python script" OR "data cleaning"
Terms: Fixed-price: $450 • Intermediate • Less than 1 month
Client: Canada · rating 4.9 · spent $8k · payment verified
Skills: Python, pandas, Data Cleaning
Posted: 7 minutes ago
Check on the job page: proposals < 20, Interviewing < 5, the client's hire rate, the price in Connects (the e-mail does not show them).
Niche: python automation

Score: 9/10 🔥 apply · fit 4/4 · chance 2.5/3 · client 1.5/2 · money 1/1
Why: Merging supplier spreadsheets in Python is the core of the profile, and the scope is clear.
Risks: Supplier files may change their columns; ask for 2-3 real samples before you promise a date.
Price: $450
Milestones: 1) Merge and SKU mapping: $300; 2) Missing-price report and docs: $150
The proposal follows in the next message (tap it to copy).
buttons: [✅ Sent] [⏭ Skip]

[bot -> you] message #107 (a tap-to-copy block)
Hi,
You get price lists from six suppliers, each with its own column names, and you need one CSV in your SKU format with the gaps flagged. The hard part is usually the mapping, not the merge.
I'd start with your sample files and write a small mapping table per supplier, so a renamed column is a one-line change. You get a script that builds the CSV, a report of products without a price, and a short README. A first version can be ready in 4-5 days.
I did a similar job for a hardware shop: nine suppliers merged into one catalog file every morning.
Which supplier changes its file format most often?
Jordan
```
<!-- sample-output:end -->

## Quickstart

Python 3.11 or newer, no packages to install. In a clone of this repository:

```bash
python bot_daemon.py --simulate    # offline demo: fake mailbox, fake Telegram, canned answers
cp .env.example .env               # then fill in your mailbox, bot token and settings
cp profile.example.md profile.md   # then describe your own work: drafts use only this file
python check_setup.py              # checks the mailbox login, Telegram, owner chat, profile, Claude CLI
python bot_daemon.py               # runs until stopped; logs go to logs/daemon.log
```

For a real run you need:

- a mailbox that receives your Upwork alert e-mails over IMAP (Gmail with an app password works);
- a Telegram bot token from @BotFather. Press Start in your bot before anyone else can;
- the Claude Code CLI (`claude`), logged in on the same machine. Without it the bot still works, but cards
  come without a draft.

Tests: `python -m unittest discover` (offline, no keys, a few seconds).

## What it does

- **Job cards.** Every 3 minutes it reads new "New job alert" e-mails, read-only. Rules drop scams and jobs
  under your budget floor before any model call. The model gives a fit score and a draft; the card shows the
  job, the score with its parts, the reasons, a price and milestones. The draft follows as a separate message
  that copies with one tap. Buttons: **✅ Sent** (then pick the Connects it cost) and **⏭ Skip**.
- **Upwork notifications.** Client messages, proposal viewed, interview invitation, offer, contract started,
  milestone funded, payment, proposal declined or job closed: a short card with what to do next. A client
  message that carries text gets a draft reply. Security e-mails (codes, sign-ins) come without their text.
  An e-mail that no rule recognises still comes, as subject plus the first 500 characters.
- **Your texts.** Paste a client's message and get a draft reply (plus a translation when `OWNER_LANGUAGE`
  is not English). Send `/note` with your own words, in any language, and get a clean English message for
  Upwork chat. Reply to a card to give the model its context.
- **Bookkeeping.** `/balance` sets your Connects balance, the Sent button spends it, `/stats` and a daily
  summary count cards, proposals, Connects and notifications.

The model calls go through a queue: your texts first, then client messages, then job drafts, at most
`LLM_MAX_PER_HOUR` calls per rolling hour.

### How a job is scored

| Part | Points | Who decides |
|---|---|---|
| Fit with your profile | 0-4 | the model, with the Fit guide from your profile |
| Chance to be hired | 0-3 | the model |
| Client | 0-2 | code: payment verified, rating, money spent on Upwork |
| Money | 0-1 | code: hourly rate or fixed budget from the alert |

A card is sent when the total is 5 or more and fit is at least 2.5. 🔥 marks a total of 7 or more with a
chance of 2 or more. A red flag from the model sets the score to 0 and the card is not sent. Rejected jobs
stay in the database with the reason.

## Safety and privacy

- **No upwork.com.** The bot never logs in, scrapes, opens a browser or submits anything. Links on the cards
  are the links from the e-mail; you open them yourself. Link previews are off, so Telegram does not fetch
  them either. Upwork's rules change: check the current Terms of Service yourself. You are responsible for
  what you send.
- **Read-only mailbox.** The mailbox is opened with EXAMINE and bodies are fetched with BODY.PEEK, so nothing
  is marked as read or changed. An IMAP app password opens the whole mailbox: if you can, use a separate
  mailbox that gets only Upwork mail through a forwarding rule.
- **Secrets.** Only in `.env` or the environment. They are cut out of logs and error texts and are never
  passed to the `claude` process.
- **One owner.** The bot answers one private chat: the first one that sends /start, or the one you pin with
  `TELEGRAM_OWNER_ID`. Everything else is ignored.
- **The model is boxed in.** `claude` runs with all tools off, in an empty folder, without your own
  CLAUDE.md, hooks or MCP servers. Job texts and client messages go into prompts as untrusted data, and every
  prompt says the output is a draft for a human.
- **Local data.** `data/jobs.db` keeps job texts, client messages and drafts; `data/state.json` keeps the owner
  chat and the Telegram offset. With `SAVE_EVENT_SAMPLES=1` raw notification e-mails are kept in
  `data/samples/` (off by default). `.gitignore` covers all of it.

## Telegram commands

| Command | What it does |
|---|---|
| `/help` | list of commands |
| `/stats` | Connects balance, proposals and cards today and in total, notifications, queue |
| `/balance N` | set your Connects balance (without N: show it) |
| `/pause`, `/resume` | stop or restart job cards; notifications and texts keep working |
| `/note TEXT` | turn your own words into an English message for the client |
| `/client TEXT` | draft a reply to a client's message |
| plain text | treated as a client's message, or as your note when it is mostly in a non-Latin script |

## Configuration

Settings come from `.env` (copy `.env.example`) or from the environment; the environment wins. Relative
paths are relative to the project folder.

| Key | Default | Meaning |
|---|---|---|
| `IMAP_USER`, `IMAP_PASSWORD` | required | mailbox login; for Gmail an app password |
| `TELEGRAM_BOT_TOKEN` | required | token from @BotFather |
| `IMAP_HOST` | `imap.gmail.com` | another IMAP server |
| `TELEGRAM_OWNER_ID` | empty | pin the owner chat to this numeric id |
| `PROFILE_FILE` | `profile.md` | your profile, see below |
| `FREELANCER_NAME` | empty | the name that signs the drafts; empty = unsigned |
| `OWNER_LANGUAGE` | `English` | language of the model's notes for you and of translations |
| `NICHE_KEYWORDS` | built-in list | comma-separated keywords of your niche (queue order, "Niche" line) |
| `MIN_HOURLY_USD`, `MIN_FIXED_USD` | `15`, `30` | budget floors checked before any model call; 0 = off |
| `SUMMARY_TIMEZONE`, `SUMMARY_HOUR` | `UTC`, `21` | daily summary time; `off` turns it off |
| `LLM_MAX_PER_HOUR` | `20` | model calls per rolling hour |
| `CLAUDE_PATH` | empty | full path to the `claude` executable when it is not on PATH |
| `BOT_DATA_DIR`, `BOT_LOG_DIR` | `data`, `logs` | where the database, state and logs go |
| `SAVE_EVENT_SAMPLES` | `0` | `1` keeps the first raw e-mails of each notification type |

## Your profile file

The drafts can only be as true as this file. `profile.example.md` shows the format: the model receives the
Overview, Rate, Fit, the bullets of the Proof bank and the Do-not-claim list, and the prompt tells it to
use nothing else as facts about you. Text after `→` on a Proof bank line that looks like a path or a link is
removed before the model sees it, so you can keep private notes there.

## Running it all the time

- **Windows:** `powershell -ExecutionPolicy Bypass -File deploy\install_daemon.ps1 -StartNow` registers a Task
  Scheduler task that starts at logon without a window and restarts on failure. `deploy\uninstall_daemon.ps1`
  removes it.
- **Linux:** `deploy/upwork-alert-triage.service` is a systemd user unit; the comments in it show the
  commands.
- **macOS:** not tested; any way to keep `python bot_daemon.py` running will do.

Only one copy may run at a time: the bot holds a lock file in the data folder.

## Limitations

- Job alerts are parsed from the layout Upwork's alert e-mails had in 2026. When Upwork changes it, alerts
  stop producing cards and the log says "no jobs parsed". The tests use synthetic look-alikes, not real e-mails.
- The formats of the other notifications (messages, offers, payments) are guesses. The rules sit in one
  table in `bot/events.py`; `SAVE_EVENT_SAMPLES=1` collects real examples to tune them.
- Drafts need the Claude Code CLI logged in on the same machine. The CLI flags were checked against one
  Claude Code 2.x release; another version may reject one of them (the card then shows the error). Other
  models or APIs are not supported; `call_claude()` in `bot/llm.py` is the one place to change that.
- The alert e-mail does not show the number of proposals, interviews, the client's hire rate or the Connects
  price, so the card asks you to check them on the job page.
- The Connects balance is kept by hand (`/balance`, the Sent button); the bot cannot see your real balance.
- One user per bot. No web interface.
- The author's private version also checked screenshots of job pages with Windows OCR; that part is not
  included here.
- Tested on Linux with Python 3.11, 3.12 and 3.13; 3.10 is not tested. The Windows scripts come from the
  original tool and were not re-run for this release. On Windows a `SUMMARY_TIMEZONE` other than UTC needs
  `pip install tzdata`.

## How it works

| File | Role |
|---|---|
| `bot_daemon.py` | entry point: config, lock, logging, starts the daemon or the demo |
| `bot/daemon.py` | Telegram long polling, mailbox pass, LLM queue, daily summary, in three threads |
| `bot/mail.py` | read-only IMAP: headers first, bodies only for new mail |
| `bot/parse.py` | alert e-mails to jobs (HTML and text/plain), digest links, budget and client fields |
| `bot/rules.py` | red-flag patterns, niche keywords, budget floors |
| `bot/llm.py` | the `claude` CLI call, profile parsing, the proposal prompt, answer checks |
| `bot/scoring.py` | the 0-10 score from the model's part and the alert fields |
| `bot/assist.py` | client-reply and note prompts, context of the card you replied to, hourly budget |
| `bot/events.py` | notification classification table, hints and cards |
| `bot/telegram.py` | Bot API client over urllib, HTML-safe message splitting, the job card |
| `bot/store.py` | SQLite schema and queries |
| `bot/ledger.py`, `bot/schedule.py` | Connects, stats and the daily summary in your time zone |
| `bot/simulate.py` | the offline demo |
| `check_setup.py` | one-off setup check |
| `tests/` | offline tests; sockets and subprocesses are blocked; fixtures come from `tests/fixtures/make_fixtures.py` |

Built with AI coding agents (Claude, Codex).

## License

MIT, see [LICENSE](LICENSE). Upwork is a trademark of Upwork Inc.; this project is not affiliated with,
endorsed or sponsored by Upwork.
