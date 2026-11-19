"""Anti-AI-slop and V2 Warm Pocket Notebook Mobile UI contract verification for fieldnotes-mobile."""

from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestFieldNotesUiV2Contracts(unittest.TestCase):
    def test_pocket_mobile_identity_and_safe_dom(self) -> None:
        html = (REPO_ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertIn("FieldNotes Mobile", html)
        self.assertIn("Bitter", html)
        self.assertIn("Work Sans", html)
        self.assertIn("#EFECE6", html.upper())
        self.assertIn("#FFFBEB", html.upper())
        self.assertIn("#9A3412", html.upper())
        self.assertNotIn("#F7F6F3", html.upper())
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval("):
            self.assertNotIn(sink, html)
        self.assertTrue((REPO_ROOT / "PRODUCT.md").is_file())
        self.assertTrue((REPO_ROOT / "DESIGN.md").is_file())


if __name__ == "__main__":
    unittest.main()
