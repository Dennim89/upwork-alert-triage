"""upwork-alert-triage: Upwork job-alert e-mails -> scored Telegram cards with a drafted proposal.

Reads your mailbox over IMAP (read-only), drafts with the local Claude Code CLI and sends cards to your own
Telegram chat. It never touches upwork.com: you read the card and submit the proposal yourself.
Python standard library only.
"""
__version__ = "0.1.0"
