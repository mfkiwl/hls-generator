from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = SKILL_ROOT / "scripts" / "evaluate_skill.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("evaluate_skill_test_module", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load module from {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EvaluateSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_module()

    def test_evaluate_payload_reports_pass_rate_delta(self) -> None:
        payload = {
            "version": 1,
            "title": "demo",
            "cases": [
                {
                    "id": "case_a",
                    "title": "case a",
                    "with_skill_expected_pass": True,
                    "without_skill_expected_pass": False,
                    "required_files": [],
                    "required_terms": [],
                }
            ],
        }

        report = self.module.evaluate_payload(payload)

        self.assertEqual(report["with_skill"]["passed"], 1)
        self.assertEqual(report["without_skill"]["passed"], 0)
        self.assertGreater(report["pass_rate_delta"], 0)

    def test_evaluate_case_checks_required_terms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "demo.txt").write_text("hello factual confidence\n", encoding="utf-8")
            payload = {
                "version": 1,
                "title": "demo",
                "cases": [
                    {
                        "id": "term_case",
                        "title": "term",
                        "with_skill_expected_pass": True,
                        "without_skill_expected_pass": False,
                        "required_files": ["demo.txt"],
                        "required_terms": [{"file": "demo.txt", "terms": ["factual confidence"]}],
                    }
                ],
            }

            original = self.module.SKILL_ROOT
            self.module.SKILL_ROOT = root
            try:
                report = self.module.evaluate_payload(payload)
            finally:
                self.module.SKILL_ROOT = original

        self.assertEqual(report["with_skill"]["failed"], 0)
        self.assertEqual(report["with_skill"]["passed"], 1)


if __name__ == "__main__":
    unittest.main()
