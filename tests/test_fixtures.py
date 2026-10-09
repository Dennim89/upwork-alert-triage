"""The fixtures are generated, synthetic and reproducible."""
import re
import unittest

from bot import mail
from tests.fixtures import make_fixtures
from tests.support import FIXTURES

_EMAIL = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")
_URL_HOST = re.compile(r"https?://([^/\s\"'<>?#()]+)")
ALLOWED_HOSTS = ("example.com", "example.net", "example.org", "upwork.com")


def readable(rel: str, data: bytes) -> str:
    """Decoded text of a fixture: headers and MIME parts of a mail (quoted-printable undone), JSON as is."""
    if not rel.endswith(".eml"):
        return data.decode("utf-8")
    it = mail.decode_message(data)
    return "\n".join([it.sender, str(it.message.get("To")), it.msgid, it.subject, it.html or "", it.text or ""])


class FixturesTest(unittest.TestCase):
    def test_generator_reproduces_the_committed_files(self):
        built = make_fixtures.build()
        on_disk = {p.relative_to(FIXTURES).as_posix() for p in FIXTURES.rglob("*")
                   if p.is_file() and p.suffix in (".eml", ".json")}
        self.assertEqual(set(built), on_disk)
        for rel, data in built.items():
            self.assertEqual((FIXTURES / rel).read_bytes(), data, f"{rel}: run python tests/fixtures/make_fixtures.py")

    def test_only_placeholder_addresses_and_links(self):
        for rel, data in make_fixtures.build().items():
            text = readable(rel, data)
            if rel.endswith(".eml"):
                self.assertIn("@example.com", text, rel)                       # decoding worked: sender and recipient
            for m in _EMAIL.finditer(text):
                self.assertTrue(m.group(1).endswith(("example.com", "example.net", "example.org")), f"{rel}: {m.group(0)}")
            for m in _URL_HOST.finditer(text):
                self.assertTrue(m.group(1).lower().endswith(ALLOWED_HOSTS), f"{rel}: {m.group(0)}")


if __name__ == "__main__":
    unittest.main()
