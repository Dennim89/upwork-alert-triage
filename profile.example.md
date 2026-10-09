# Profile (example)

This is a made-up freelancer. Copy this file to `profile.md` (or point `PROFILE_FILE` at your own file) and
write your own. With every draft the bot sends the model these sections and nothing else from the file:
Overview, Rate, Fit, the bullets of the Proof bank and the Do-not-claim list.

Write only what is true. The model is told to use PROFILE as its only source of facts about you.

## Overview

I'm Jordan, a Python developer. I build small automation tools for online shops and small teams: scripts that
clean and merge spreadsheets, Slack and Telegram bots, and simple dashboards. I hand over code with a short
README so it runs without me.

## Rate

$40/hour. Fixed price for well-defined tasks.

## Fit

How the model should score fit from 0 to 4:

- 4: Python scripts for cleaning and merging data, Slack and Telegram bots, integrations with documented REST APIs.
- 3: small Flask or FastAPI web apps, scrapers of public pages, LLM features on top of hosted APIs.
- 2: mobile apps, heavy frontend work, machine-learning research.
- 0-1: design, video, marketing, data entry.

## Proof bank

One line per project. Text after → that looks like a path or a link is removed before the model sees it, so
you can keep a private note there about where the evidence lives.

- Merged price lists from nine suppliers into one catalog CSV for a hardware shop; the script runs every morning. → notes/hardware-shop.md (client asked not to be named)
- Slack bot that answers questions from a team wiki with an LLM and links the source page.
- Telegram bot that posts a shop's daily sales numbers from its API.
- Small Flask dashboard of weekly orders, with CSV export.

## Do not claim

- Years of experience.
- Work for large companies, or a team or agency behind me.
- Revenue or growth numbers for any client.
