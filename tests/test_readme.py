"""The README cannot drift away from the code: the sample output is real and every setting is documented."""
import contextlib
import io
import re
import unittest

from bot import config, simulate

README = (config.BASE_DIR / "README.md").read_text(encoding="utf-8")
ENV_EXAMPLE = (config.BASE_DIR / ".env.example").read_text(encoding="utf-8")


class ReadmeTest(unittest.TestCase):
    def test_sample_output_is_an_excerpt_of_the_demo(self):
        m = re.search(r"<!-- sample-output:start -->\n```text\n(.*?)```\n<!-- sample-output:end -->", README, re.S)
        self.assertIsNotNone(m)
        out = io.StringIO()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(simulate.run(out), 0)
        self.assertIn(m.group(1), out.getvalue())

    def test_quickstart_has_at_most_five_commands(self):
        block = re.search(r"## Quickstart\n.*?```bash\n(.*?)```", README, re.S).group(1)
        commands = [ln for ln in block.splitlines() if ln.strip()]
        self.assertLessEqual(len(commands), 5)
        self.assertTrue(commands[0].startswith("python bot_daemon.py --simulate"))

    def test_every_setting_is_documented(self):
        for key in config.KNOWN_KEYS:
            self.assertIn(f"`{key}`", README, key)
            self.assertRegex(ENV_EXAMPLE, rf"(?m)^{key}=", key)
        self.assertIn("Built with AI coding agents (Claude, Codex).", README)


if __name__ == "__main__":
    unittest.main()
