from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))


class RuntimeConfigTests(unittest.TestCase):
    def test_remote_validation_config_allows_empty_vitis_profiles(self) -> None:
        from runtime.hls_generator import config as config_module

        runtime_config_path = SKILL_ROOT / "runtime" / "hls_generator" / "runtime_config.json"
        payload = json.loads(runtime_config_path.read_text(encoding="utf-8"))
        payload["remote_validation"]["vitis_profiles"] = {}

        with tempfile.TemporaryDirectory(dir=SKILL_ROOT) as tmp:
            override_path = Path(tmp) / "runtime_config.empty_profiles.json"
            override_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            old_value = os.environ.get("HLS_GENERATOR_RUNTIME_CONFIG")
            os.environ["HLS_GENERATOR_RUNTIME_CONFIG"] = str(override_path.relative_to(SKILL_ROOT))
            config_module._cached_runtime_config.cache_clear()
            try:
                remote_config = config_module.remote_validation_config()
            finally:
                config_module._cached_runtime_config.cache_clear()
                if old_value is None:
                    os.environ.pop("HLS_GENERATOR_RUNTIME_CONFIG", None)
                else:
                    os.environ["HLS_GENERATOR_RUNTIME_CONFIG"] = old_value

        self.assertEqual(remote_config["vitis_profiles"], {})

    def test_remote_validation_config_exposes_directory_contract(self) -> None:
        from runtime.hls_generator import config as config_module

        remote_config = config_module.remote_validation_config()

        self.assertEqual(remote_config["directory_contract"]["project_root_dirname"], "erie-hls-generator")
        self.assertEqual(remote_config["directory_contract"]["conda_prefix_path"], ".conda/hls-generator")
        self.assertEqual(remote_config["directory_contract"]["active_run_path_template"], "runs/<run-id>")
        self.assertEqual(remote_config["directory_contract"]["backup_run_path_template"], "backups/<run-id>")
        self.assertTrue(remote_config["directory_contract"]["archive_after_verification"])


if __name__ == "__main__":
    unittest.main()
