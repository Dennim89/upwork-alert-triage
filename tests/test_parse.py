import base64
import unittest

from bot import parse, rules
from tests.support import BLANK_RATE, CSV, CSV_TITLE, RED_FLAG, SLACK, SLACK_TITLE, item

INVISIBLE = ("͏", "­", "\xa0")


def one(name: str) -> parse.Job:
    it = item(name)
    jobs, diag = parse.parse_email(it.subject, it.html, it.text, it.msgid)
    assert (diag["kind"], diag["parser"], len(jobs)) == ("job_alert", "alert", 1), diag
    return jobs[0]


class ClassifyTest(unittest.TestCase):
    def test_subjects(self):
        self.assertEqual(parse.classify("New job alert: Build a dashboard"), "job_alert")
        self.assertEqual(parse.classify("  new JOB alert  "), "job_alert")
        self.assertEqual(parse.classify("Jobs picked for you"), "digest")
        for s in ("Your profile is now visible to clients", "Verify your email", "Freelancer Plus membership", "", None):
            self.assertEqual(parse.classify(s), "other")


class AlertTest(unittest.TestCase):
    def test_fields_of_a_fixed_price_alert(self):
        j = one(CSV)
        self.assertEqual(j.title, CSV_TITLE)
        self.assertEqual(j.query, '"python script" OR "data cleaning"')
        self.assertEqual(j.terms, "Fixed-price: $450 • Intermediate • Less than 1 month")
        self.assertEqual((j.budget_type, j.fixed_budget, j.hourly_max, j.experience), ("fixed", 450.0, None, "Intermediate"))
        self.assertEqual(j.client, "Payment verified • 4.9 • $8k spent • Canada")
        self.assertEqual((j.payment_verified, j.client_rating, j.client_spend, j.client_country),
                         (True, 4.9, "$8k", "Canada"))
        self.assertEqual(j.skills, ["Python", "pandas", "Data Cleaning"])
        self.assertEqual(j.posted, "7 minutes ago")
        self.assertTrue(j.snippet.startswith("We get price lists from six suppliers"))
        self.assertTrue(j.snippet.endswith("Sample files will be shared after hiring."))
        self.assertIsNone(j.proposals)

    def test_link_is_the_view_job_details_href_and_id_is_msgid_plus_title(self):
        it = item(CSV)
        j = parse.parse_job_alert(it.subject, it.html, None, it.msgid)[0]
        flat, anchors = parse.html_to_text(it.html)
        view = next(a["href"] for a in anchors if flat[a["start"]:a["end"]] == "View job details")
        title_href = next(a["href"] for a in anchors if flat[a["start"]:a["end"]] == CSV_TITLE)
        self.assertEqual(j.url, view)
        self.assertNotEqual(view, title_href)
        self.assertEqual(j.id, parse.alert_job_id("<alert-csv@mail.example.com>", CSV_TITLE))

    def test_text_only_path_agrees_with_html(self):
        for name in (CSV, SLACK, RED_FLAG):
            it = item(name)
            a = parse.parse_job_alert(it.subject, it.html, None, it.msgid)[0]
            b = parse.parse_job_alert(it.subject, None, it.text, it.msgid)[0]
            for k in ("id", "url", "title", "query", "terms", "hourly_max", "fixed_budget", "client", "skills",
                      "posted", "snippet"):
                self.assertEqual(getattr(a, k), getattr(b, k), f"{name}: {k}")
            self.assertEqual(a.fingerprint(), b.fingerprint())

    def test_invisible_padding_and_hidden_preheader_are_gone(self):
        it = item(CSV)
        flat, _ = parse.html_to_text(it.html)
        self.assertFalse(any(ch in flat for ch in INVISIBLE))
        self.assertNotIn('"python script" OR "da...', flat)                  # hidden, truncated preheader
        j = parse.parse_job_alert(it.subject, it.html, it.text, it.msgid)[0]
        for v in (j.title, j.snippet, j.query, j.terms, j.client, *j.skills):
            self.assertFalse(any(ch in v for ch in INVISIBLE), v)

    def test_subject_without_title_falls_back_to_layout(self):
        it = item(CSV)
        j = parse.parse_job_alert("New job alert", it.html, None, it.msgid)[0]
        self.assertEqual(j.title, CSV_TITLE)
        self.assertEqual(j.terms, "Fixed-price: $450 • Intermediate • Less than 1 month")

    def test_meta_on_one_line(self):
        j = one(SLACK)
        self.assertEqual(j.title, SLACK_TITLE)
        self.assertEqual(j.terms, "Hourly: $35-$55 • 1 to 3 months • Less than 30 hrs/week")
        self.assertEqual((j.budget_type, j.hourly_min, j.hourly_max), ("hourly", 35.0, 55.0))
        self.assertEqual((j.client_country, j.client_spend, j.client_rating), ("United Kingdom", "$26k", 4.7))
        self.assertEqual(j.skills, ["Python", "Slack API", "LLM", "RAG"])

    def test_plain_text_only_alert_with_a_blank_rate(self):
        j = one(BLANK_RATE)
        self.assertEqual(j.terms, "Hourly: $0-$0 • 1 to 3 months • < 30 hrs/week")       # entity decoded
        self.assertEqual((j.budget_type, j.hourly_min, j.hourly_max), ("hourly", None, None))  # $0-$0 = not given
        self.assertEqual((j.client_country, j.client_rating, j.posted), ("Ireland", 5.0, "recently"))
        self.assertEqual(j.url, "https://click.example.com/t/library/view")
        self.assertNotIn("http", j.snippet)
        self.assertFalse(rules.check(j.title, j.text(), j.fields()).skip)

    def test_unverified_payment_and_red_flag_text(self):
        j = one(RED_FLAG)
        self.assertEqual((j.payment_verified, j.client_spend, j.client_country), (False, "$0", "Australia"))
        self.assertEqual(j.experience, "Entry")
        self.assertIn("Contact me on WhatsApp", j.snippet)

    def test_same_job_from_two_alerts_has_same_fingerprint_but_new_id(self):
        it = item(CSV)
        a = parse.parse_job_alert(it.subject, it.html, it.text, it.msgid)[0]
        b = parse.parse_job_alert(it.subject, it.html, it.text, "<other-alert@x>")[0]
        self.assertNotEqual(a.id, b.id)
        self.assertEqual(a.fingerprint(), b.fingerprint())

    def test_other_mail_yields_nothing(self):
        it = item("other_profile.eml")
        self.assertEqual(parse.parse_email(it.subject, it.html, it.text, it.msgid)[0], [])


class LinksParserTest(unittest.TestCase):
    def test_digest_jobs_from_direct_and_wrapped_links(self):
        it = item("digest.eml")
        jobs, diag = parse.parse_email(it.subject, it.html, it.text, it.msgid)
        self.assertEqual(diag["kind"], "digest")
        self.assertEqual([j.id for j in jobs], ["01b2000000000000b2", "01b3000000000000b3", "01b4000000000000b4",
                                                "01b5000000000000b5"])
        self.assertTrue(all(j.url == f"https://www.upwork.com/jobs/~{j.id}" for j in jobs))
        self.assertEqual(jobs[0].title, "Inventory sync between two web shops")
        self.assertEqual((jobs[0].budget_type, jobs[0].fixed_budget), ("fixed", 700.0))
        self.assertEqual((jobs[1].hourly_min, jobs[1].hourly_max, jobs[1].experience), (25.0, 40.0, "Intermediate"))
        self.assertEqual(jobs[2].title, "Telegram bot that posts daily sales numbers")        # base64 redirect
        for j in jobs:
            self.assertNotIn("Unsubscribe", j.snippet)
            self.assertNotIn("©", j.snippet)
            self.assertNotIn("View job", j.snippet)
            self.assertLessEqual(len(j.snippet), parse.SNIPPET_MAX + 1)
        self.assertFalse(any("Rust developer" in j.title for j in jobs))        # opaque link: no id, no job

    def test_find_job_variants(self):
        b64 = base64.urlsafe_b64encode(b"https://www.upwork.com/jobs/~0123abcdef?x=1").decode()
        cases = {
            "https://www.upwork.com/jobs/~0123abcdef": "0123abcdef",
            "https://www.upwork.com/jobs/Some-Title_~0123abcdef/?ref=x": "0123abcdef",
            "https://upwork.com/freelance-jobs/apply/Python-Dev_~0198765432/": "0198765432",
            "https://www.upwork.com/ab/proposals/job/~01aa22bb33/apply/": "01aa22bb33",
            "https://www.upwork.com/jobs/%7E0123abcdef": "0123abcdef",
            "https://t.example.com/c?u=https%3A%2F%2Fwww.upwork.com%2Fjobs%2F~0123abcdef%3Fs%3D1": "0123abcdef",
            "https://t.example.com/c?u=https%253A%252F%252Fwww.upwork.com%252Fjobs%252F~0123abcdef": "0123abcdef",
            "https://r.example.org/L0/https:%2F%2Fwww.upwork.com%2Fjobs%2F~0123abcdef/1/abc=": "0123abcdef",
            f"https://t.example.com/r?d={b64}&s=1": "0123abcdef",
            "https://www.upwork.com/ab/account-security/login?redir=%2Fjobs%2F~0123abcdef": "0123abcdef",
            "https://www.upwork.com/jobs/~0123abcdef&amp;x=1": "0123abcdef",
        }
        for href, jid in cases.items():
            self.assertEqual((parse.find_job(href) or ("",))[0], jid, href)
        for href in ("https://click.example.com/t/csv/view", "https://www.upwork.com/freelancers/~0123abcdef",
                     "https://evilupwork.com/jobs/~0123abcdef", "https://www.upwork.com/nx/find-work/",
                     "https://example.com/jobs/~0123abcdef", "", None):
            self.assertIsNone(parse.find_job(href), href)

    def test_plain_text_links_and_titles(self):
        text = ("Hi,\n\nPython scraper for a used-car site\nhttps://www.upwork.com/jobs/~0111aaaa22\n"
                "Hourly: $20.00-$40.00 | Intermediate | Posted 2 hours ago\nScrape listings daily.\n\n"
                "Telegram bot for a bakery: https://www.upwork.com/jobs/~0222bbbb33?ref=alert\n"
                "Fixed-price | Est. budget: $250 | Posted yesterday\nOrders and payments in Telegram.\n\n"
                "Manage notifications: https://www.upwork.com/ab/notification-settings/\n")
        jobs = parse.parse_links(None, text)
        self.assertEqual([(j.id, j.title) for j in jobs], [("0111aaaa22", "Python scraper for a used-car site"),
                                                           ("0222bbbb33", "Telegram bot for a bakery")])
        self.assertEqual((jobs[0].hourly_max, jobs[1].fixed_budget, jobs[1].posted), (40.0, 250.0, "yesterday"))
        self.assertNotIn("http", jobs[0].snippet)
        self.assertNotIn("Manage", jobs[1].snippet)


class FieldsTest(unittest.TestCase):
    def test_budget_variants(self):
        cases = {
            "Hourly: $8-$25 • 1 to 3 months": ("hourly", 8.0, 25.0, None),
            "Hourly ($5-$10)": ("hourly", 5.0, 10.0, None),
            "Hourly: $25.00 - $45.00": ("hourly", 25.0, 45.0, None),
            "$20-$40/hr, long term": ("hourly", 20.0, 40.0, None),
            "Hourly: $30": ("hourly", 30.0, 30.0, None),
            "Fixed-price: $1.5K • Expert": ("fixed", None, None, 1500.0),
            "Fixed price • Budget: $1,200": ("fixed", None, None, 1200.0),
            "Est. budget: $400": ("fixed", None, None, 400.0),
            "Hourly • 1 to 3 months": ("hourly", None, None, None),
            "Fixed-price • Less than 1 month": ("fixed", None, None, None),
        }
        for text, (bt, lo, hi, fixed) in cases.items():
            f = parse.budget_fields(text)
            self.assertEqual((f.get("budget_type"), f.get("hourly_min"), f.get("hourly_max"), f.get("fixed_budget")),
                             (bt, lo, hi, fixed), text)
        self.assertEqual(parse.budget_fields("We need a dashboard, 400 kilograms of data"), {})

    def test_zero_amounts_mean_unknown(self):
        self.assertEqual(parse.budget_fields("Hourly: $0-$0 • 1 to 3 months"), {"budget_type": "hourly"})
        self.assertEqual(parse.budget_fields("Hourly: $0"), {"budget_type": "hourly"})
        self.assertEqual(parse.budget_fields("Fixed-price: $0 • Expert"), {"budget_type": "fixed"})
        self.assertEqual(parse.budget_fields("Hourly: $0-$25")["hourly_max"], 25.0)

    def test_client_line_variants(self):
        self.assertEqual(parse.client_fields("Payment verified • 4.8 • $3k spent • Canada"),
                         {"payment_verified": True, "client_spend": "$3k", "client_rating": 4.8,
                          "client_country": "Canada"})
        self.assertEqual(parse.client_fields("Payment unverified • $0 spent • Kenya")["payment_verified"], False)
        f = parse.client_fields("Payment verified | ★★★★★ 4.9 | $12K+ spent | United States")
        self.assertEqual((f["client_rating"], f["client_spend"], f["client_country"]), (4.9, "$12K+", "United States"))
        self.assertEqual(parse.client_fields("Client location: Germany")["client_country"], "Germany")


class RobustnessTest(unittest.TestCase):
    def test_garbage_never_raises(self):
        for html, text in ((None, None), ("", ""), ("<<<>>><a href=", "((( )))"), ("<html><body>" * 500, "x" * 5000),
                           ("<table><tr><td><a href='https://www.upwork.com/jobs/~0123abcdef'>", None)):
            for subject in ("New job alert: X", "Jobs picked for you", "whatever"):
                jobs, _ = parse.parse_email(subject, html, text, "<m@x>")
                self.assertIsInstance(jobs, list)

    def test_long_description_is_capped(self):
        html = ("<p>New job alert</p><p>Take a look and submit a proposal when you're ready.</p>"
                "<a href='https://click.example.com/t/long/title'>Long job</a>"
                f"<p>{'word ' * 900}</p><p>Hourly: $30-$50</p><p>Payment verified • $1k spent • Canada</p>"
                "<p>Posted recently.</p><a href='https://click.example.com/t/long/view'>View job details</a>")
        j = parse.parse_job_alert("New job alert: Long job", html, None, "<m@x>")[0]
        self.assertLessEqual(len(j.snippet), parse.SNIPPET_MAX + 1)
        self.assertTrue(j.snippet.endswith("…"))
        self.assertEqual((j.hourly_max, j.client_country, j.url), (50.0, "Canada", "https://click.example.com/t/long/view"))


if __name__ == "__main__":
    unittest.main()
