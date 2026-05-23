from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = SKILL_ROOT / "scripts" / "confidence_loop.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("confidence_loop_test_module", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load module from {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _joined(*parts: str) -> str:
    return "".join(parts)


class ConfidenceLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_module()

    def test_local_confidence_stays_local_without_remote_gate(self) -> None:
        status, scope, risks, returncode = self.module._confidence_outcome(
            {
                "smoke": {"status": "passed"},
                "compileall": {"status": "passed"},
                "pytest": {"status": "passed"},
                "verify_agents": {"status": "passed"},
                "manage_docs_verify": {"status": "passed"},
                "manage_dirs_verify": {"status": "passed"},
                "skill_dependencies": {"status": "passed"},
                "copyright_term_scan": {"status": "passed"},
                "example_mock_validation": {"status": "passed"},
                "forward_test": {"status": "passed"},
                "route_contract": {"status": "passed"},
                "board_acceptance_declarations": {"status": "passed"},
                "remote_directory_contract": {"status": "passed"},
            },
            remote_requested=False,
            remote_skipped=True,
        )

        self.assertEqual(status, "local_high_confidence")
        self.assertEqual(scope, "local")
        self.assertEqual(returncode, 0)
        self.assertIn("Final confidence requires remote Vitis acceptance.", risks)

    def test_final_confidence_requires_remote_gate(self) -> None:
        status, scope, risks, returncode = self.module._confidence_outcome(
            {
                "smoke": {"status": "passed"},
                "compileall": {"status": "passed"},
                "pytest": {"status": "passed"},
                "verify_agents": {"status": "passed"},
                "manage_docs_verify": {"status": "passed"},
                "manage_dirs_verify": {"status": "passed"},
                "skill_dependencies": {"status": "passed"},
                "copyright_term_scan": {"status": "passed"},
                "example_mock_validation": {"status": "passed"},
                "forward_test": {"status": "passed"},
                "route_contract": {"status": "passed"},
                "board_acceptance_declarations": {"status": "passed"},
                "remote_directory_contract": {"status": "passed"},
                "remote_vitis_acceptance": {"status": "passed"},
                "remote_board_acceptance": {"status": "passed"},
            },
            remote_requested=True,
            remote_skipped=False,
        )

        self.assertEqual(status, "factual_high_confidence")
        self.assertEqual(scope, "final")
        self.assertEqual(returncode, 0)
        self.assertEqual(risks, [])

    def test_missing_remote_gate_blocks_final_confidence(self) -> None:
        status, scope, risks, returncode = self.module._confidence_outcome(
            {
                "smoke": {"status": "passed"},
                "compileall": {"status": "passed"},
                "pytest": {"status": "passed"},
                "verify_agents": {"status": "passed"},
                "manage_docs_verify": {"status": "passed"},
                "manage_dirs_verify": {"status": "passed"},
                "skill_dependencies": {"status": "passed"},
                "copyright_term_scan": {"status": "passed"},
                "example_mock_validation": {"status": "passed"},
                "forward_test": {"status": "passed"},
                "route_contract": {"status": "passed"},
                "board_acceptance_declarations": {"status": "passed"},
                "remote_directory_contract": {"status": "passed"},
            },
            remote_requested=False,
            remote_skipped=False,
        )

        self.assertEqual(status, "blocked_remote_validation")
        self.assertEqual(scope, "final")
        self.assertEqual(returncode, 1)
        self.assertIn("Remote Vitis acceptance was not executed.", risks)

    def test_governance_gate_failure_blocks_local_high_confidence(self) -> None:
        status, scope, risks, returncode = self.module._confidence_outcome(
            {
                "smoke": {"status": "passed"},
                "compileall": {"status": "passed"},
                "pytest": {"status": "passed"},
                "verify_agents": {"status": "failed"},
                "manage_docs_verify": {"status": "passed"},
                "manage_dirs_verify": {"status": "passed"},
                "skill_dependencies": {"status": "passed"},
                "copyright_term_scan": {"status": "passed"},
                "example_mock_validation": {"status": "passed"},
                "forward_test": {"status": "passed"},
                "route_contract": {"status": "passed"},
                "board_acceptance_declarations": {"status": "passed"},
                "remote_directory_contract": {"status": "passed"},
            },
            remote_requested=False,
            remote_skipped=True,
        )

        self.assertEqual(status, "needs_attention")
        self.assertEqual(scope, "local")
        self.assertEqual(returncode, 1)
        self.assertIn("At least one confidence gate failed", "\n".join(risks))

    def test_remote_directory_contract_gate_checks_archived_paths(self) -> None:
        result = self.module._remote_directory_contract_gate(
            [
                {
                    "run_id": "run-42",
                    "remote_project_root": "erie-hls-generator",
                    "remote_conda_prefix": "erie-hls-generator/.conda/hls-generator",
                    "remote_run_dir": "erie-hls-generator/runs/run-42",
                    "remote_backup_dir": "erie-hls-generator/backups/run-42",
                    "archived_after_verification": True,
                }
            ],
            remote_requested=True,
        )

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["results"][0]["status"], "passed")

    def test_route_contract_failure_blocks_final_confidence(self) -> None:
        status, scope, risks, returncode = self.module._confidence_outcome(
            {
                "smoke": {"status": "passed"},
                "compileall": {"status": "passed"},
                "pytest": {"status": "passed"},
                "verify_agents": {"status": "passed"},
                "manage_docs_verify": {"status": "passed"},
                "manage_dirs_verify": {"status": "passed"},
                "skill_dependencies": {"status": "passed"},
                "copyright_term_scan": {"status": "passed"},
                "example_mock_validation": {"status": "passed"},
                "forward_test": {"status": "passed"},
                "route_contract": {"status": "failed"},
                "board_acceptance_declarations": {"status": "passed"},
                "remote_directory_contract": {"status": "passed"},
                "remote_vitis_acceptance": {"status": "passed"},
                "remote_board_acceptance": {"status": "passed"},
            },
            remote_requested=True,
            remote_skipped=False,
        )

        self.assertEqual(status, "blocked_remote_validation")
        self.assertEqual(scope, "final")
        self.assertEqual(returncode, 1)
        self.assertIn("AGENTS contract", "\n".join(risks))

    def test_blocked_board_acceptance_blocks_final_confidence(self) -> None:
        status, scope, risks, returncode = self.module._confidence_outcome(
            {
                "smoke": {"status": "passed"},
                "compileall": {"status": "passed"},
                "pytest": {"status": "passed"},
                "verify_agents": {"status": "passed"},
                "manage_docs_verify": {"status": "passed"},
                "manage_dirs_verify": {"status": "passed"},
                "skill_dependencies": {"status": "passed"},
                "copyright_term_scan": {"status": "passed"},
                "example_mock_validation": {"status": "passed"},
                "forward_test": {"status": "passed"},
                "route_contract": {"status": "passed"},
                "board_acceptance_declarations": {"status": "passed"},
                "remote_directory_contract": {"status": "passed"},
                "remote_vitis_acceptance": {"status": "passed"},
                "remote_board_acceptance": {"status": "blocked"},
            },
            remote_requested=True,
            remote_skipped=False,
        )

        self.assertEqual(status, "blocked_remote_validation")
        self.assertEqual(scope, "final")
        self.assertEqual(returncode, 1)
        self.assertIn("Board acceptance is blocked", "\n".join(risks))

    def test_remote_board_acceptance_gate_blocks_when_any_board_result_is_blocked(self) -> None:
        with patch.object(self.module, "_run_remote_board", return_value={"status": "blocked_board_validation", "example_spec": "hls_vector_scale_spec.json"}):
            result = self.module._remote_board_acceptance_gate(
                "server_6",
                "cosim",
                vitis_version="2022.2",
                remote_requested=True,
                remote_vitis_gate={"status": "passed"},
                board_partition={
                    "board_specs": [{"spec": "hls_vector_scale_spec.json", "profile": "u55c_m_axi_host"}],
                    "exempt_specs": [],
                    "invalid_specs": [],
                },
                selected_specs=["hls_vector_scale_spec.json"],
            )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["mode"], "remote_board_validation")

    def test_copyright_scan_catches_sensitive_content_and_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "references").mkdir()
            bad_file = root / "references" / "scan_notes.md"
            bad_file.write_text(
                "source " + _joined("off", "icial") + " note\n",
                encoding="utf-8",
            )
            bad_dir = root / _joined("tuto", "rials")
            bad_dir.mkdir()
            result = self.module._copyright_term_scan(root=root)

        self.assertEqual(result["status"], "failed")
        self.assertGreaterEqual(len(result["matches"]), 2)

    def test_release_sensitivity_scan_catches_fixed_remote_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "runtime" / "hls_generator").mkdir(parents=True)
            (root / "runtime" / "hls_generator" / "runtime_config.json").write_text(
                json_text := (
                    "{\n"
                    '  "remote_validation": {\n'
                    '    "erie_settings_path": "${erie_skill_dir}/config/defaults.json",\n'
                    '    "vitis_profiles": {\n'
                    '      "vitis_2022": {\n'
                    '        "settings_script": "/' + "tools" + '/Xilinx/Vitis/2022.2/settings64.sh",\n'
                    '        "expected_tool": "vitis_hls",\n'
                    '        "target_part": "' + "xcu50" + '-fsvh2104-2-e"\n'
                    "      }\n"
                    "    }\n"
                    "  }\n"
                    "}\n"
                ),
                encoding="utf-8",
            )
            result = self.module._release_sensitivity_scan(root=root)

        self.assertEqual(result["status"], "failed")
        self.assertTrue(any(("/" + "tools" + "/Xilinx/") in item for item in result["matches"]))
        self.assertTrue(any("xcu50" in item for item in result["matches"]))

    def test_release_sensitivity_scan_catches_sensitive_zip_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive_path = Path(tmp) / "erie-hls-generator-v0.1.8.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr(
                    "erie-hls-generator-v0.1.8/skills/erie-hls-generator/runtime/hls_generator/runtime_config.json",
                    (
                        "{\n"
                        '  "remote_validation": {\n'
                        '    "settings_script": "/' + "tools" + '/Xilinx/Vitis/2022.2/settings64.sh"\n'
                        "  }\n"
                        "}\n"
                    ),
                )

            result = self.module._release_sensitivity_scan(root=archive_path)

        self.assertEqual(result["status"], "failed")
        self.assertTrue(any(archive_path.name in item for item in result["matches"]))
        self.assertTrue(any(("/" + "tools" + "/Xilinx/") in item for item in result["matches"]))

    def test_release_sensitivity_scan_ignores_validation_only_probe_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script_path = root / "scripts" / "remote_vitis_acceptance.py"
            script_path.parent.mkdir(parents=True)
            script_path.write_text(
                'DEFAULT = "/' + "tools" + '/Xilinx/Vitis/2022.2/settings64.sh"\n'
                'PART = "' + "xcu50" + '-fsvh2104-2-e"\n',
                encoding="utf-8",
            )

            result = self.module._release_sensitivity_scan(root=root)

        self.assertEqual(result["status"], "passed")

    def test_example_spec_names_include_new_shipped_patterns(self) -> None:
        spec_names = self.module._example_spec_names()

        self.assertIn("hls_axi4_burst_vector_scale_spec.json", spec_names)
        self.assertIn("hls_task_graph_axis_spec.json", spec_names)
        self.assertIn("hls_directio_freerun_axis_spec.json", spec_names)

    def test_run_remote_passes_vitis_version_when_requested(self) -> None:
        seen: dict[str, object] = {}

        def fake_run_remote_command(command, *, timeout_s=900):
            seen["command"] = command
            seen["timeout_s"] = timeout_s
            return {"status": "passed"}

        with patch.object(self.module, "_run_remote_command", side_effect=fake_run_remote_command):
            result = self.module._run_remote("server-a", "cosim", "hls_vector_scale_spec.json", vitis_version="2024.2")

        self.assertEqual(result["status"], "passed")
        self.assertIn("--vitis-version", seen["command"])
        self.assertIn("2024.2", seen["command"])
        self.assertEqual(seen["timeout_s"], 5400)

    def test_run_remote_acceptance_stops_when_link_fails(self) -> None:
        commands: list[list[str]] = []

        def fake_run_remote_command(command, *, timeout_s=900):
            commands.append(command)
            timeouts.append(timeout_s)
            if "--mode" in command and command[command.index("--mode") + 1] == "link":
                return {"status": "failed", "error": "link failed"}
            return {"status": "passed"}

        timeouts: list[int] = []
        with patch.object(self.module, "_run_remote_command", side_effect=fake_run_remote_command):
            result = self.module._run_remote_acceptance("server-a", "cosim", ["hls_vector_scale_spec.json"], vitis_version="2024.2")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["results"], [])
        self.assertEqual(len(commands), 1)
        self.assertEqual(timeouts, [900])
        self.assertIn("--timeout", commands[0])
        self.assertEqual(commands[0][commands[0].index("--timeout") + 1], "300")

    def test_run_split_remote_passes_dual_server_arguments(self) -> None:
        seen: dict[str, object] = {}

        def fake_run_remote_command(command):
            seen["command"] = command
            return {"status": "passed"}

        with patch.object(self.module, "_run_remote_command", side_effect=fake_run_remote_command):
            result = self.module._run_split_remote(
                "build-a",
                "validate-b",
                "cosim",
                "hls_vector_scale_spec.json",
                vitis_version="2022.2",
            )

        self.assertEqual(result["status"], "passed")
        self.assertIn("--build-server", seen["command"])
        self.assertIn("build-a", seen["command"])
        self.assertIn("--validate-server", seen["command"])
        self.assertIn("validate-b", seen["command"])

    def test_run_split_remote_acceptance_stops_when_preflight_fails(self) -> None:
        calls: list[list[str]] = []

        def fake_run_remote_command(command):
            calls.append(command)
            return {"status": "failed", "error": "preflight failed"}

        with patch.object(self.module, "_run_remote_command", side_effect=fake_run_remote_command):
            result = self.module._run_split_remote_acceptance(
                "build-a",
                "validate-b",
                "cosim",
                ["hls_vector_scale_spec.json"],
                vitis_version="2022.2",
            )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["results"], [])
        self.assertEqual(len(calls), 1)

    def test_run_remote_board_uses_extended_timeout(self) -> None:
        seen: dict[str, object] = {}

        def fake_run_remote_command(command, *, timeout_s=900):
            seen["command"] = command
            seen["timeout_s"] = timeout_s
            return {"status": "passed"}

        with patch.object(self.module, "_run_remote_command", side_effect=fake_run_remote_command):
            result = self.module._run_remote_board(
                "server_6",
                "cosim",
                "hls_host_kernel_split_spec.json",
                vitis_version="2022.2",
            )

        self.assertEqual(result["status"], "passed")
        self.assertEqual(seen["timeout_s"], 5400)
        self.assertIn("--mode", seen["command"])
        self.assertIn("board", seen["command"])
        self.assertIn("--timeout", seen["command"])
        self.assertEqual(seen["command"][seen["command"].index("--timeout") + 1], "5400")

    def test_run_remote_vitis_uses_extended_timeout(self) -> None:
        seen: dict[str, object] = {}

        def fake_run_remote_command(command, *, timeout_s=900):
            seen["command"] = command
            seen["timeout_s"] = timeout_s
            return {"status": "passed"}

        with patch.object(self.module, "_run_remote_command", side_effect=fake_run_remote_command):
            result = self.module._run_remote(
                "server_6",
                "cosim",
                "hls_host_kernel_split_spec.json",
                vitis_version="2022.2",
            )

        self.assertEqual(result["status"], "passed")
        self.assertEqual(seen["timeout_s"], 5400)
        self.assertNotIn("--timeout", seen["command"])

    def test_run_command_returns_timeout_status_instead_of_hanging(self) -> None:
        result = self.module._run_command(
            [sys.executable, "-c", "import sys,time; print('partial out', flush=True); print('partial err', file=sys.stderr, flush=True); time.sleep(5)"],
            cwd=SKILL_ROOT,
            timeout_s=1,
        )

        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["returncode"], None)
        self.assertIn("partial out", result["stdout_tail"])
        self.assertIn("partial err", result["stderr_tail"])

    def test_run_remote_command_returns_timeout_payload(self) -> None:
        result = self.module._run_remote_command(
            [sys.executable, "-c", "import sys,time; print('{\"status\":', flush=True); print('ssh timeout', file=sys.stderr, flush=True); time.sleep(5)"],
            timeout_s=1,
        )

        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["returncode"], None)
        self.assertIn("ssh timeout", result["stderr_tail"])


if __name__ == "__main__":
    unittest.main()
