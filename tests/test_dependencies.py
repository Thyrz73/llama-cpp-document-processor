"""Keep the development lock on the exact runtime versions that are deployed."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def pins(name):
    text = (ROOT / name).read_text(encoding="utf-8")
    return dict(re.findall(r"^([a-z0-9_.-]+)==(\S+)", text, re.M))


class LockTests(unittest.TestCase):
    def test_development_lock_matches_runtime_lock(self):
        runtime, development = pins("requirements.txt"), pins("requirements-dev.txt")
        self.assertTrue(runtime)
        self.assertEqual(
            {name: development.get(name) for name in runtime},
            runtime,
            "regenerate requirements-dev.txt after requirements.txt",
        )


if __name__ == "__main__":
    unittest.main()
