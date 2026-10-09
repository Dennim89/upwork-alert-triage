import unittest

from bot import assist, llm, pipeline, scoring


def fields(**kw):
    base = {"budget_type": "fixed", "fixed_budget": 400.0, "payment_verified": True, "client_rating": 4.8,
            "client_spend": "$32k"}
    base.update(kw)
    return base


class SpentTest(unittest.TestCase):
    def test_spent_usd(self):
        self.assertEqual(scoring.spent_usd("$2k"), 2000)
        self.assertEqual(scoring.spent_usd("$201k"), 201000)
        self.assertEqual(scoring.spent_usd("$1.2M"), 1_200_000)
        self.assertEqual(scoring.spent_usd("$549"), 549)
        self.assertEqual(scoring.spent_usd("$1,500"), 1500)
        self.assertIsNone(scoring.spent_usd(None))
        self.assertIsNone(scoring.spent_usd("n/a"))


class PointsTest(unittest.TestCase):
    def test_client_points(self):
        self.assertEqual(scoring.client_points(fields()), 2.0)                          # verified, 4.8, $32k
        self.assertEqual(scoring.client_points(fields(client_spend="$2k")), 1.5)
        self.assertEqual(scoring.client_points(fields(client_rating=None, client_spend="$40")), 0.75)
        self.assertEqual(scoring.client_points({}), 0.25)                                # unknown new client

    def test_money_points(self):
        self.assertEqual(scoring.money_points(fields(fixed_budget=400.0)), 1.0)
        self.assertEqual(scoring.money_points(fields(fixed_budget=100.0)), 0.5)
        self.assertEqual(scoring.money_points(fields(fixed_budget=20.0)), 0.0)
        self.assertEqual(scoring.money_points({"budget_type": "hourly", "hourly_max": 35.0}), 1.0)
        self.assertEqual(scoring.money_points({"budget_type": "hourly", "hourly_max": 18.0}), 0.5)
        self.assertEqual(scoring.money_points({"budget_type": "hourly", "hourly_max": None}), 0.75)  # "$0-$0"
        self.assertEqual(scoring.money_points({}), 0.5)


class FinalizeTest(unittest.TestCase):
    def test_strong_job_is_hot(self):
        d = scoring.finalize({"fit": 4, "win": 2.5, "proposal_en": "x"}, fields())
        self.assertEqual((d["score"], d["verdict"]), (9.5, "apply"))
        self.assertTrue(pipeline.should_send(d))
        self.assertEqual(scoring.label(d), "9.5/10 🔥 apply · fit 4/4 · chance 2.5/3 · client 2/2 · money 1/1")

    def test_weak_fit_is_skipped(self):
        d = scoring.finalize({"fit": 1, "win": 1}, fields(client_spend="$40", client_rating=None, fixed_budget=100.0))
        self.assertLess(d["score"], scoring.SEND_MIN)
        self.assertEqual(d["verdict"], "skip")
        self.assertFalse(pipeline.should_send(d))
        self.assertTrue(scoring.label(d).startswith("3.2/10 outside your field"))

    def test_strong_client_cannot_lift_a_job_outside_your_field(self):
        d = scoring.finalize({"fit": 2, "win": 1}, fields(budget_type="hourly", hourly_max=60.0, client_spend="$84k"))
        self.assertEqual((d["score"], d["verdict"]), (6.0, "skip"))
        self.assertFalse(pipeline.should_send(d))
        self.assertIn("outside your field", scoring.label(d))

    def test_long_term_job_is_sent_but_not_hot(self):
        d = scoring.finalize({"fit": 4, "win": 1}, fields(budget_type="hourly", hourly_max=35.0))
        self.assertEqual(d["score"], 8.0)
        self.assertTrue(pipeline.should_send(d))
        self.assertIn("🤔 maybe", scoring.label(d))

    def test_red_flag_zeroes_and_never_sends(self):
        d = scoring.finalize({"fit": 4, "win": 3, "red_flag": True, "red_flag_reason": "captcha solving"}, fields())
        self.assertEqual((d["score"], d["verdict"]), (0.0, "skip"))
        self.assertFalse(pipeline.should_send(d))
        self.assertEqual(scoring.label(d), "0/10 ⛔ red flag")

    def test_out_of_range_model_values_are_clamped(self):
        d = scoring.finalize({"fit": 9, "win": -2}, fields())
        self.assertEqual((d["fit"], d["win"]), (4.0, 0.0))

    def test_older_schema_passes_through(self):
        old = {"score": 6.0, "verdict": "apply"}
        self.assertIs(scoring.finalize(old, fields()), old)
        self.assertTrue(pipeline.should_send(old))
        self.assertEqual(scoring.label(old), "6/10")
        self.assertIsNone(scoring.finalize(None, fields()))


class NormalizeTest(unittest.TestCase):
    def test_current_schema_is_accepted(self):
        d, err = llm.draft_from_text('{"fit": 3.5, "win": 2, "red_flag": false, "red_flag_reason": "", '
                                     '"why": "ok", "risks": "", "price_usd": 300, '
                                     '"milestones": [{"name": "MVP", "usd": 300}], "proposal_en": "Hi\\nJordan"}')
        self.assertIsNone(err)
        self.assertEqual((d["fit"], d["win"], d["red_flag"], d["why"]), (3.5, 2.0, False, "ok"))
        self.assertEqual(scoring.finalize(d, fields())["score"], 8.5)

    def test_red_flag_string_true(self):
        d, _ = llm.draft_from_text('{"fit": 2, "win": 1, "red_flag": "true", "proposal_en": ""}')
        self.assertTrue(d["red_flag"])

    def test_human_style_post_processing(self):
        d, _ = llm.draft_from_text('{"fit": 3, "win": 2, "proposal_en": "Hi,\\nThe sync drops orders — usually '
                                   'the payload. I will leverage my seamless approach.\\nJordan"}')
        self.assertNotIn("—", d["proposal_en"])
        self.assertIn("drops orders, usually", d["proposal_en"])
        self.assertIn('sounds AI-written: "leverage", "seamless"', d["warnings"])
        clean, _ = llm.draft_from_text('{"fit": 3, "win": 2, "proposal_en": "Hi,\\nI\'d start with the logs.\\nJordan"}')
        self.assertEqual(clean["warnings"], [])

    def test_every_prompt_carries_the_style(self):
        persona = llm.Persona("Jordan")
        for text in (llm.job_rules(persona), assist.client_rules(persona), assist.note_rules(persona)):
            self.assertIn("STYLE: write like a real person", text)
        self.assertIn("No em dashes", llm.HUMAN_STYLE)

    def test_prompt_has_the_rubric(self):
        rules = llm.job_rules(llm.Persona())
        self.assertIn('"fit": 0-4', rules)
        self.assertIn("job alerts always truncate", rules)
        self.assertIn("CAPABILITY", rules)


if __name__ == "__main__":
    unittest.main()
