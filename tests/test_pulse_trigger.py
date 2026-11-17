"""Pulse workflow contract: external cron trigger and a pinned recipient."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class PulseWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with (ROOT / ".github/workflows/pulse.yml").open(encoding="utf-8") as fh:
            cls.doc = yaml.safe_load(fh)

    def _triggers(self) -> dict:
        # PyYAML 1.1 reads the YAML key `on` as boolean True.
        return self.doc.get("on", self.doc.get(True, {}))

    def test_external_cron_can_dispatch_pulse(self):
        types = self._triggers().get("repository_dispatch", {}).get("types", [])
        self.assertIn("airwatch-tick", types)

    def test_recipient_is_pinned_from_repo_variable(self):
        env = self.doc["jobs"]["pulse"].get("env", {})
        self.assertEqual(env.get("TELEGRAM_CHAT_ID"), "${{ vars.TELEGRAM_CHAT_ID }}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
