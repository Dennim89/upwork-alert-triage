import io
import json
import re
import unittest
import urllib.error

from bot import telegram as tg
from tests.support import TOKEN, FakeTelegram

JOB = {"id": "a-1", "title": "Build a <dashboard> & API", "url": "https://click.example.com/t/x/view?a=1&b=2",
       "fields": {"query": '"api integration"', "terms": "Hourly: $30-$45 • 1 to 3 months", "budget_type": "hourly",
                  "hourly_min": 30.0, "hourly_max": 45.0, "client_country": "Canada", "client_rating": 4.8,
                  "client_spend": "$3k", "payment_verified": True, "skills": ["API", "API Integration"],
                  "posted": "recently"},
       "fit_tags": ["api integration", "dashboard"]}
DRAFT = {"score": 6.0, "verdict": "apply", "why": "Matches the <profile>", "risks": "Low rate",
         "price_usd": 40.0, "milestones": [], "proposal_en": "Line 1 <b>not bold</b> & co\n- bullet\nJordan",
         "warnings": []}


def http_error(code: int, body: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(f"https://api.telegram.org/bot{TOKEN}/sendMessage", code, "err", {},
                                  io.BytesIO(json.dumps(body).encode()))


def balanced(chunk: str) -> bool:
    stack = []
    for m in re.finditer(r"<(/?)(\w+)[^>]*>", chunk):
        if m.group(1):
            if not stack or stack.pop() != m.group(2):
                return False
        else:
            stack.append(m.group(2))
    return not stack


class CardTest(unittest.TestCase):
    def test_full_card(self):
        card = tg.format_card(JOB, DRAFT)
        self.assertTrue(card.startswith("<b>Build a &lt;dashboard&gt; &amp; API</b>\n"))
        self.assertIn('<a href="https://click.example.com/t/x/view?a=1&amp;b=2">Open the job</a>', card)
        for s in ('Search: "api integration"', "Terms: Hourly: $30-$45 • 1 to 3 months",
                  "Client: Canada · rating 4.8 · spent $3k · payment verified", "Skills: API, API Integration",
                  "Posted: recently", tg.esc(tg.CHECK_ON_PAGE), "Niche: api integration, dashboard",
                  "Score: 6/10 · apply", "Why: Matches the &lt;profile&gt;", "Risks: Low rate", "Rate: $40/h",
                  "The proposal follows in the next message"):
            self.assertIn(s, card)
        self.assertTrue(balanced(card))
        self.assertNotIn("not bold", card)                                    # the proposal is a separate message

    def test_card_without_draft_and_fixed_price(self):
        job = dict(JOB, fields={"budget_type": "fixed", "fixed_budget": 600.0, "proposals": "5 to 10"}, url="")
        card = tg.format_card(job, None, "claude CLI not found")
        self.assertIn("Budget: fixed $600", card)
        self.assertIn("Proposals: 5 to 10", card)
        self.assertIn("No link in the e-mail", card)
        self.assertTrue(card.endswith("No draft: claude CLI not found."))
        fixed = tg.format_card(job, dict(DRAFT, price_usd=900.0, milestones=[{"name": "Bot", "usd": 500.0},
                                                                              {"name": "AI", "usd": 400.0}]))
        self.assertIn("Price: $900", fixed)
        self.assertIn("Milestones: 1) Bot: $500; 2) AI: $400", fixed)

    def test_scored_draft_red_flag_and_warnings(self):
        d = {"fit": 4.0, "win": 3.0, "client_pts": 2.0, "money_pts": 1.0, "score": 0.0, "red_flag": True,
             "red_flag_reason": "asks to pay outside Upwork", "warnings": ['profit/PnL claim: "ROI"'], "proposal_en": ""}
        card = tg.format_card(JOB, d)
        self.assertIn("Score: 0/10 ⛔ red flag", card)
        self.assertIn("⛔ asks to pay outside Upwork", card)
        self.assertIn('Check the text: profit/PnL claim: "ROI"', card)
        self.assertTrue(card.endswith("The proposal text is empty."))
        blank = dict(JOB, fields={"terms": "Hourly: $0-$0 • 1 to 3 months", "budget_type": "hourly"})
        self.assertIn("Budget: hourly (rate not given)", tg.format_card(blank, None))

    def test_proposal_message(self):
        self.assertEqual(tg.format_proposal(DRAFT), "<pre>Line 1 &lt;b&gt;not bold&lt;/b&gt; &amp; co\n- bullet\nJordan</pre>")


class SplitTest(unittest.TestCase):
    def test_short_text_untouched(self):
        self.assertEqual(tg.split_html("<b>x</b>"), ["<b>x</b>"])

    def test_long_pre_is_split_with_balanced_tags(self):
        body = "\n".join(f"line {i} " + "word & " * 20 for i in range(300))
        text = tg.format_proposal({"proposal_en": body})
        chunks = tg.split_html(text)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(tg._u16(c), tg.MAX_LEN)
            self.assertTrue(c.startswith("<pre>") and c.endswith("</pre>"))
            self.assertTrue(balanced(c))
        joined = "\n".join(c[len("<pre>"):-len("</pre>")] for c in chunks)
        self.assertEqual(joined, text[len("<pre>"):-len("</pre>")])

    def test_overlong_line_and_emoji_count(self):
        line = "<b>" + "&amp;" * 3000 + "</b>"
        for c in tg.split_html(line):
            self.assertLessEqual(tg._u16(c), tg.MAX_LEN)
            self.assertTrue(balanced(c))
            self.assertNotRegex(c, r"&[a-z]*$")                     # never cut inside an entity
        self.assertEqual(tg._u16("😀"), 2)
        self.assertGreater(len(tg.split_html("😀" * 3000)), 1)

    def test_html_to_plain(self):
        self.assertEqual(tg.html_to_plain('<b>T &amp; x</b>\n<a href="https://e.x/?a=1&amp;b=2">Open</a>'),
                         "T & x\nOpen: https://e.x/?a=1&b=2")


class ApiTest(unittest.TestCase):
    def test_errors_never_contain_the_token(self):
        op = FakeTelegram([http_error(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})])
        with self.assertRaises(tg.TelegramError) as cm:
            tg.get_me(TOKEN, opener=op)
        self.assertEqual(cm.exception.code, 401)
        self.assertNotIn(TOKEN, str(cm.exception))
        op = FakeTelegram([urllib.error.URLError(f"cannot reach https://api.telegram.org/bot{TOKEN}/getMe")])
        with self.assertRaises(tg.TelegramError) as cm:
            tg.get_me(TOKEN, opener=op)
        self.assertNotIn(TOKEN, str(cm.exception))
        self.assertNotIn(TOKEN.split(":")[1], str(cm.exception))
        op = FakeTelegram([urllib.error.HTTPError("u", 502, "Bad Gateway", {}, io.BytesIO(b"<html>"))])
        with self.assertRaises(tg.TelegramError) as cm:
            tg.get_me(TOKEN, opener=op)
        self.assertEqual(cm.exception.code, 502)

    def test_send_message_params_and_split(self):
        op = FakeTelegram()
        ids = tg.send_message(TOKEN, 42, "<b>hi</b>\n" + "x" * 5000, opener=op)
        self.assertEqual(len(ids), 2)
        self.assertTrue(all(tg._u16(p["text"]) <= tg.MAX_LEN for _, p, _ in op.requests))
        self.assertEqual("".join(p["text"] for _, p, _ in op.requests).count("x"), 5000)
        method, params, url = op.requests[0]
        self.assertEqual((method, params["chat_id"], params["parse_mode"]), ("sendMessage", 42, "HTML"))
        self.assertEqual(params["link_preview_options"], {"is_disabled": True})
        self.assertTrue(url.startswith("https://api.telegram.org/bot"))

    def test_keyboard_reply_and_callback_limits(self):
        op = FakeTelegram()
        kb = tg.inline_keyboard([[("Yes", "s:1"), ("No", "k:1")]])
        tg.send_message(TOKEN, 1, "<b>hi</b>\n" + "y" * 5000, opener=op, reply_markup=kb, reply_to=9)
        first, last = op.requests[0][1], op.requests[-1][1]
        self.assertEqual(first["reply_parameters"]["message_id"], 9)           # thread on the first chunk
        self.assertNotIn("reply_markup", first)
        self.assertEqual(last["reply_markup"], kb)                             # buttons on the last chunk
        with self.assertRaises(ValueError):
            tg.inline_keyboard([[("x", "c:" + "9" * 70)]])

    def test_429_waits_once_and_400_falls_back_to_plain(self):
        slept = []
        op = FakeTelegram([http_error(429, {"ok": False, "error_code": 429, "description": "Too Many Requests",
                                            "parameters": {"retry_after": 3}}), {"ok": True, "result": {"message_id": 9}}])
        self.assertEqual(tg.send_message(TOKEN, 1, "hi", opener=op, sleep=slept.append), [9])
        self.assertEqual(slept, [3])
        op = FakeTelegram([http_error(400, {"ok": False, "error_code": 400,
                                            "description": "Bad Request: can't parse entities"}),
                           {"ok": True, "result": {"message_id": 5}}])
        self.assertEqual(tg.send_message(TOKEN, 1, '<b>x</b> <a href="https://e.x">L</a>', opener=op), [5])
        self.assertNotIn("parse_mode", op.requests[1][1])
        self.assertEqual(op.requests[1][1]["text"], "x L: https://e.x")
        op = FakeTelegram([http_error(400, {"ok": False, "error_code": 400, "description": "Bad Request: wrong HTTP URL"}),
                           {"ok": True, "result": {"message_id": 6}}])
        self.assertEqual(tg.send_message(TOKEN, 1, tg.format_card(JOB, None), opener=op), [6])
        self.assertIn("Open the job: https://click.example.com/t/x/view?a=1&b=2", op.requests[1][1]["text"])
        op = FakeTelegram([http_error(400, {"ok": False, "error_code": 400, "description": "chat not found"})] * 2)
        with self.assertRaises(tg.TelegramError):
            tg.send_message(TOKEN, 1, "<b>x</b>", opener=op)                     # plain retry fails too: raise
        self.assertEqual(len(op.requests), 2)
        op = FakeTelegram([http_error(429, {"ok": False, "error_code": 429, "parameters": {"retry_after": 600}})])
        with self.assertRaises(tg.TelegramError):
            tg.send_message(TOKEN, 1, "hi", opener=op, sleep=slept.append)       # long waits are not slept

    def test_edit_markup_not_modified_is_fine(self):
        op = FakeTelegram([http_error(400, {"ok": False, "error_code": 400,
                                            "description": "Bad Request: message is not modified"})])
        self.assertIsNone(tg.edit_reply_markup(TOKEN, 1, 2, None, opener=op))
        self.assertEqual(op.requests[0][1]["reply_markup"], {"inline_keyboard": []})


def upd(uid, chat_id, ctype="private", text="/start", **chat):
    return {"update_id": uid, "message": {"message_id": uid, "text": text,
                                          "chat": {"id": chat_id, "type": ctype, **chat}}}


class OwnerTest(unittest.TestCase):
    def test_first_private_start_wins(self):
        ups = [upd(1, -100, "group"), upd(2, 7, text="hello"), upd(3, 11, first_name="Jordan", username="jordan_dev"),
               upd(4, 12, first_name="Stranger")]
        self.assertEqual(tg.find_owner(ups), {"chat_id": 11, "name": "jordan_dev"})
        self.assertEqual(tg.find_owner([upd(1, 5, text="/start@MyJobsHelper")])["chat_id"], 5)
        self.assertIsNone(tg.find_owner([upd(1, 5, text="/started")]))
        self.assertEqual(tg.find_owner(ups, pinned=12), {"chat_id": 12, "name": "Stranger"})   # only the pinned chat
        self.assertIsNone(tg.find_owner(ups, pinned=99))

    def test_ensure_owner_binds_once_and_tracks_offset(self):
        st = {}
        op = FakeTelegram([{"ok": True, "result": []}])
        self.assertIsNone(tg.ensure_owner(TOKEN, st, opener=op))
        op = FakeTelegram([{"ok": True, "result": [upd(20, 12, first_name="Stranger2"), upd(19, 11, username="jo")]}])
        self.assertEqual(tg.ensure_owner(TOKEN, st, opener=op), 11)        # sorted by update_id: 19 first
        self.assertEqual((st["owner_chat_id"], st["owner_name"], st["update_offset"]), (11, "jo", 21))
        op2 = FakeTelegram()
        self.assertEqual(tg.ensure_owner(TOKEN, st, opener=op2), 11)
        self.assertEqual(op2.requests, [])                                  # no API call once bound
        self.assertTrue(tg.is_owner(upd(30, 11, text="hi"), st))
        self.assertFalse(tg.is_owner(upd(31, 12, text="hi"), st))
        cb = {"update_id": 32, "callback_query": {"id": "q", "message": {"chat": {"id": 11}}}}
        self.assertTrue(tg.is_owner(cb, st))

    def test_poll_offset(self):
        st = {"update_offset": 5}
        op = FakeTelegram([{"ok": True, "result": [{"update_id": 6}, {"update_id": 8}]}])
        self.assertEqual([u["update_id"] for u in tg.poll(TOKEN, st, opener=op)], [6, 8])
        self.assertEqual(op.requests[0][1]["offset"], 5)
        self.assertEqual(st["update_offset"], 9)


if __name__ == "__main__":
    unittest.main()
