import unittest

from bot import rules


class RedFlagTest(unittest.TestCase):
    def test_each_flag(self):
        cases = [
            ("Need a volume bot for our new token", "volume bot"),
            ("volume booster for our token", "volume bot"),
            ("wash-trading script for an exchange", "wash trading"),
            ("bundler script for token launches", "bundler"),
            ("we will do a rug pull later", "rug pull"),
            ("Pump and dump group automation", "pump and dump"),
            ("pump & dump signals bot", "pump and dump"),
            ("build a wallet drainer", "drainer"),
            ("Phishing page that looks like a bank login", "phishing"),
            ("Contact me on Telegram for details", "contact off-platform"),
            ("please message us via WhatsApp", "contact off-platform"),
            ("add me on skype to discuss", "contact off-platform"),
            ("My telegram: @someone", "contact off-platform"),
            ("we pay outside Upwork", "outside upwork"),
            ("payment off-platform only", "outside upwork"),
            ("you pay for training first", "pay for training"),
            ("there is a small training fee", "pay for training"),
            ("A refundable deposit of $20 is required", "deposit"),
            ("You must make a small deposit to get started", "deposit"),
            ("Applicants need to deposit $30 for the test kit", "deposit"),
            ("first an unpaid test task", "unpaid test"),
            ("Clone our repo and run it locally to start", "clone our repo and run"),
            ("clone my GitHub repo, install deps and run the tests", "clone our repo and run"),
            ("download and run our app to test", "download and run"),
            ("I will connect via AnyDesk", "anydesk"),
            ("install TeamViewer", "teamviewer"),
            ("write fake reviews on Google", "fake reviews"),
            ("Upwork account rental, $200/month", "account rental"),
            ("rent your account to us", "account rental"),
            ("MEV sandwich attack bot on Ethereum", "sandwich attack"),
        ]
        for text, label in cases:
            res = rules.check("Job", text, {})
            self.assertTrue(res.skip, text)
            self.assertEqual(res.reason, f"red flag: {label}", text)

    def test_ordinary_jobs_are_not_flagged(self):
        for text in ("Build a Telegram bot that reminds clients about bookings",
                     "Users deposit USDC into the vault and the bot rebalances",
                     "Build deposit and withdraw flows for our exchange dashboard",
                     "You will integrate the deposit API of the payment provider",
                     "Clone the competitor's pricing page layout",
                     "Scraper that should download and store PDFs",
                     "The bot should notify users on Telegram"):
            self.assertIsNone(rules.red_flag(text), text)


class NicheTest(unittest.TestCase):
    def test_default_niche_tags(self):
        tags = rules.fit_tags("Python script to scrape listings, Telegram bot alerts, OpenAI summary, "
                              "ETL into a dashboard, API integration with an AI agent")
        for t in ("python automation", "scraping", "telegram bot", "openai", "data pipeline", "dashboard",
                  "api integration", "ai agent"):
            self.assertIn(t, tags)
        self.assertEqual(rules.fit_tags("Logo design for a bakery"), ())
        res = rules.check("Build a dashboard with API integration", "", {})
        self.assertEqual((res.skip, res.fit, res.tags), (False, 2, ("api integration", "dashboard")))

    def test_custom_keywords_replace_the_default_list(self):
        cfg = rules.settings(["Slack bot", "C++", "data  cleaning"])
        self.assertEqual([label for label, _ in cfg.niche], ["slack bot", "c++", "data cleaning"])
        self.assertEqual(rules.fit_tags("SlackBots for HR and a C++ port, plus data-cleaning", cfg),
                         ("slack bot", "c++", "data cleaning"))
        self.assertEqual(rules.fit_tags("Telegram bot", cfg), ())
        self.assertEqual(rules.fit_tags("CPP or C+ only", cfg), ())
        self.assertEqual(rules.settings([]).niche, rules.DEFAULT.niche)

    def test_keyword_pattern_word_boundaries(self):
        rx = rules.keyword_pattern("api")
        self.assertTrue(rx.search("REST APIs"))
        self.assertFalse(rx.search("rapid prototyping"))
        with self.assertRaises(ValueError):
            rules.keyword_pattern("   ")


class BudgetTest(unittest.TestCase):
    def test_default_thresholds(self):
        self.assertIn("low hourly", rules.check("x", "", {"budget_type": "hourly", "hourly_max": 12.0}).reason)
        self.assertFalse(rules.check("x", "", {"budget_type": "hourly", "hourly_max": 15.0}).skip)
        self.assertIn("low budget", rules.check("x", "", {"budget_type": "fixed", "fixed_budget": 29.0}).reason)
        self.assertFalse(rules.check("x", "", {"budget_type": "fixed", "fixed_budget": 30.0}).skip)
        self.assertFalse(rules.check("x", "", {"budget_type": "hourly"}).skip)          # unparsed: no skip
        self.assertFalse(rules.check("x", "", {}).skip)
        # "Hourly: $0-$0" = rate left blank by the client, not a $0 job
        self.assertFalse(rules.check("x", "", {"budget_type": "hourly", "hourly_min": 0.0, "hourly_max": 0.0}).skip)
        self.assertFalse(rules.check("x", "", {"budget_type": "fixed", "fixed_budget": 0.0}).skip)

    def test_configured_thresholds(self):
        cfg = rules.settings(min_hourly=40, min_fixed=500)
        self.assertEqual(rules.check("x", "", {"budget_type": "hourly", "hourly_max": 35.0}, cfg).reason,
                         "low hourly: max $35/h < $40")
        self.assertEqual(rules.check("x", "", {"budget_type": "fixed", "fixed_budget": 450.0}, cfg).reason,
                         "low budget: $450 < $500")
        self.assertFalse(rules.check("x", "", {"budget_type": "hourly", "hourly_max": 12.0},
                                     rules.settings(min_hourly=0)).skip)

    def test_red_flag_wins_over_budget(self):
        res = rules.check("Volume bot", "", {"budget_type": "fixed", "fixed_budget": 5.0})
        self.assertEqual(res.reason, "red flag: volume bot")


if __name__ == "__main__":
    unittest.main()
