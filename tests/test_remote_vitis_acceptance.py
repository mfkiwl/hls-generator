from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = SKILL_ROOT / "scripts" / "remote_vitis_acceptance.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("remote_vitis_acceptance_test_module", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load module from {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RemoteVitisAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_module()

    def test_planned_steps_keep_remote_artifacts_by_default(self) -> None:
        steps = self.module._planned_steps(
            "vitis",
            "server-a",
            "profile-a",
            "cosim",
            cleanup_remote=False,
            example_spec="hls_vector_scale_mock_spec.json",
        )

        self.assertIn("archive verified remote run into backups/<run-id>", steps)
        self.assertNotIn("erie request delete cleanup", steps)

    def test_planned_steps_include_split_validation_phases(self) -> None:
        steps = self.module._planned_steps(
            "vitis",
            "build-server",
            "profile-a",
            "cosim",
            cleanup_remote=False,
            example_spec="hls_vector_scale_mock_spec.json",
            validate_server="validate-server",
            topology="split_build_validate",
        )

        self.assertIn("erie check build-server", steps)
        self.assertIn("erie workspace-check validate-server", steps)
        self.assertIn("erie request command validation Vitis cosim", steps)

    def test_planned_steps_include_board_validation_phases(self) -> None:
        steps = self.module._planned_steps(
            "board",
            "board-server",
            "profile-a",
            "cosim",
            cleanup_remote=False,
            example_spec="hls_vector_scale_spec.json",
        )

        self.assertIn("erie exec hardware fingerprint probe for 9950X/U55C evidence", steps)
        self.assertIn("render validation-only board host scaffold", steps)
        self.assertIn("erie exec detached board compile/link/host-run sequence", steps)

    def test_resolve_topology_accepts_split_server_inputs(self) -> None:
        args = argparse.Namespace(server=None, build_server="build-a", validate_server="validate-b")

        topology = self.module._resolve_topology(args)

        self.assertEqual(topology["topology"], "split_build_validate")
        self.assertEqual(topology["build_server"], "build-a")
        self.assertEqual(topology["validate_server"], "validate-b")

    def test_select_split_version_prefers_lowest_shared_version(self) -> None:
        build_candidates = [
            {"version": "2022.2", "settings_script": "/build/2022.2/settings64.sh", "expected_tool": "vitis_hls"},
            {"version": "2023.2", "settings_script": "/build/2023.2/settings64.sh", "expected_tool": "vitis_hls"},
        ]
        validate_candidates = [
            {"version": "2022.2", "settings_script": "/validate/2022.2/settings64.sh", "expected_tool": "vitis_hls"},
            {"version": "2023.2", "settings_script": "/validate/2023.2/settings64.sh", "expected_tool": "vitis_hls"},
        ]
        args = argparse.Namespace(vitis_version=None)

        version = self.module._select_shared_vitis_version(args, build_candidates, validate_candidates)

        self.assertEqual(version, "2022.2")

    def test_select_vitis_profile_blocks_when_multiple_versions_need_choice(self) -> None:
        args = argparse.Namespace(
            server="server-a",
            profile="configured_profile",
            readiness="cosim",
            example_spec="hls_vector_scale_mock_spec.json",
            vitis_version=None,
        )
        candidates = [
            {"version": "2022.1", "settings_script": "/user/configured/vitis-2022.1/settings64.sh", "expected_tool": "vitis_hls"},
            {"version": "2022.2", "settings_script": "/user/configured/vitis-2022.2/settings64.sh", "expected_tool": "vitis_hls"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            with patch.object(self.module, "get_vitis_selection", return_value=None):
                result = self.module._select_vitis_profile(args, run_dir, candidates, {"settings_script": "/user/configured/fallback/settings64.sh", "expected_tool": "vitis_hls"})
            self.assertTrue(Path(result["remote_vitis_version_request"]).exists())

        self.assertEqual(result["status"], self.module.BLOCKED_VERSION_STATUS)
        self.assertEqual(len(result["candidate_versions"]), 2)

    def test_resolve_profile_config_blocks_when_user_must_provide_settings(self) -> None:
        args = argparse.Namespace(
            mode="vitis",
            server="server-a",
            profile=None,
            readiness="cosim",
            example_spec="hls_vector_scale_mock_spec.json",
            vitis_version=None,
        )
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            with patch.object(self.module, "get_vitis_selection", return_value=None):
                result = self.module._resolve_profile_config(
                    args,
                    run_dir,
                    candidates=[],
                    configured_profiles={},
                    required_fields=("settings_script", "expected_tool", "target_part"),
                )

        self.assertEqual(result["status"], self.module.BLOCKED_PROFILE_STATUS)
        self.assertEqual(result["missing_fields"], ["settings_script", "expected_tool", "target_part"])

    def test_blocked_profile_config_uses_board_mode_when_requested(self) -> None:
        args = argparse.Namespace(
            mode="board",
            server="server-a",
            profile=None,
            readiness="cosim",
            example_spec="hls_vector_scale_spec.json",
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = self.module._blocked_profile_config(args, Path(tmp), missing_fields=["settings_script"], configured_profiles={})

        self.assertEqual(result["mode"], "board")

    def test_probe_platform_name_prefers_matching_u55c_platform(self) -> None:
        helper = Mock()
        helper.exec.return_value = "\n".join(
            [
                "/tools/Xilinx/Vitis/2022.2/base_platforms/xilinx_u55c_gen3x16_xdma_3_202210_1/xilinx_u55c_gen3x16_xdma_3_202210_1.xpfm",
                "/tools/Xilinx/Vitis/2022.2/base_platforms/xilinx_vck190_base_202220_1/xilinx_vck190_base_202220_1.xpfm",
            ]
        )

        result = self.module._probe_platform_name(
            "server_6",
            Path("settings.json"),
            helper,
            {"target_part": "xcu55c-fsvh2892-2L-e"},
        )

        self.assertEqual(result["status"], self.module.PASS_STATUS)
        self.assertEqual(result["selected_platform"], "xilinx_u55c_gen3x16_xdma_3_202210_1")

    def test_probe_platform_name_blocks_when_no_matching_u55c_platform_exists(self) -> None:
        helper = Mock()
        helper.exec.side_effect = [
            "\n".join(
                [
                    "/tools/Xilinx/Vitis/2022.2/base_platforms/xilinx_vck190_base_202220_1/xilinx_vck190_base_202220_1.xpfm",
                    "/tools/Xilinx/Vitis/2022.2/base_platforms/xilinx_zcu104_base_202220_1/xilinx_zcu104_base_202220_1.xpfm",
                ]
            ),
            "\n".join(
                [
                    "Device(s) Present",
                    "|BDF             |Shell                            |Logic UUID                            |Device ID       |Device Ready*  |",
                    "|[0000:02:00.0]  |xilinx_u55c_gen3x16_xdma_base_3  |9708-uuid                              |mgmt(inst=512)  |Yes            |",
                ]
            ),
        ]

        result = self.module._probe_platform_name(
            "server_6",
            Path("settings.json"),
            helper,
            {"target_part": "xcu55c-fsvh2892-2L-e"},
        )

        self.assertEqual(result["status"], self.module.BLOCKED_BOARD_STATUS)
        self.assertEqual(result["reason"], "no_matching_platform_shell_detected")
        self.assertEqual(result["shell_name"], "xilinx_u55c_gen3x16_xdma_base_3")
        self.assertEqual(result["suggested_platform_name"], "xilinx_u55c_gen3x16_xdma_3_202210_1")

    def test_probe_hardware_fingerprint_accepts_xbmgmt_u55c_shell(self) -> None:
        helper = Mock()
        helper.exec.return_value = "\n".join(
            [
                "cpu_model=AMD Ryzen 9 9950X3D 16-Core Processor",
                "lspci=02:00.0 Processing accelerators: Xilinx Corporation Device 505c",
                "firmware_scan=/opt/xilinx/firmware/u55c",
                "board_scan=",
                "mgmt_scan=Device(s) Present",
                "|BDF             |Shell                            |Logic UUID                            |Device ID       |Device Ready*  |",
                "|[0000:02:00.0]  |xilinx_u55c_gen3x16_xdma_base_3  |9708-uuid                              |mgmt(inst=512)  |Yes            |",
            ]
        )

        result = self.module._probe_hardware_fingerprint(
            "server_6",
            Path("settings.json"),
            helper,
            {"settings_script": "/tools/Xilinx/Vitis/2022.2/settings64.sh", "xbmgmt_tool_path": "/opt/xilinx/xrt/bin/xbmgmt"},
        )

        self.assertEqual(result["status"], self.module.PASS_STATUS)
        self.assertTrue(result["firmware_hint"])

    def test_select_vitis_profile_persists_explicit_version(self) -> None:
        args = argparse.Namespace(
            server="server-a",
            profile="configured_profile",
            readiness="cosim",
            example_spec="hls_vector_scale_mock_spec.json",
            vitis_version="2022.2",
        )
        candidate = {
            "version": "2022.2",
            "settings_script": "/user/configured/settings64.sh",
            "expected_tool": "vitis_hls",
            "target_part": "user-configured-part",
        }
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            with patch.object(self.module, "set_vitis_selection") as set_selection:
                result = self.module._select_vitis_profile(args, run_dir, [candidate], {"settings_script": "/user/configured/fallback/settings64.sh", "expected_tool": "vitis_hls"})

        self.assertEqual(result["version"], "2022.2")
        set_selection.assert_called_once()

    def test_select_vitis_profile_enriches_saved_selection_from_candidate(self) -> None:
        args = argparse.Namespace(
            server="server-a",
            profile=None,
            readiness="cosim",
            example_spec="hls_vector_scale_mock_spec.json",
            vitis_version=None,
        )
        saved = {
            "version": "2022.2",
            "settings_script": "/user/configured/settings64.sh",
            "expected_tool": "vitis_hls",
        }
        candidate = {
            "version": "2022.2",
            "settings_script": "/tools/Xilinx/Vitis/2022.2/settings64.sh",
            "expected_tool": "vitis_hls",
            "vpp_path": "/tools/Xilinx/Vitis/2022.2/bin/v++",
            "xrt_tool_path": "/opt/xilinx/xrt/bin/xrt-smi",
            "xrt_setup_script": "/opt/xilinx/xrt/setup.sh",
        }
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            with patch.object(self.module, "get_vitis_selection", return_value=saved):
                with patch.object(self.module, "set_vitis_selection") as set_selection:
                    result = self.module._select_vitis_profile(args, run_dir, [candidate], {"settings_script": "/fallback/settings64.sh", "expected_tool": "vitis_hls"})

        self.assertEqual(result["version"], "2022.2")
        self.assertEqual(result["vpp_path"], "/tools/Xilinx/Vitis/2022.2/bin/v++")
        self.assertEqual(result["xrt_setup_script"], "/opt/xilinx/xrt/setup.sh")
        set_selection.assert_called_once()

    def test_resolve_board_platform_selection_prefers_cli_and_persists(self) -> None:
        args = argparse.Namespace(
            platform_name="xilinx_u55c_gen3x16_xdma_3_202210_1",
            remote_platform_root="erie-hls-generator/platforms/alveo/xilinx_u55c_gen3x16_xdma_3_202210_1",
            remote_xpfm="erie-hls-generator/platforms/alveo/xilinx_u55c_gen3x16_xdma_3_202210_1/xilinx_u55c_gen3x16_xdma_3_202210_1.xpfm",
        )
        with patch.object(self.module, "set_board_platform_selection") as set_selection:
            result = self.module._resolve_board_platform_selection(
                args,
                "server_6",
                "<REDACTED_LOCAL_PATH>
                {"target_part": "xcu55c-fsvh2892-2L-e"},
                {"project_root_dirname": "erie-hls-generator", "platform_root_path_template": "platforms/alveo/<platform-name>"},
            )

        self.assertEqual(result["platform_name"], "xilinx_u55c_gen3x16_xdma_3_202210_1")
        self.assertEqual(result["remote_platform_root"], "<REDACTED_LOCAL_PATH>
        self.assertEqual(result["remote_xpfm"], "<REDACTED_LOCAL_PATH>
        set_selection.assert_called_once()

    def test_resolve_board_platform_selection_uses_saved_entry_when_cli_missing(self) -> None:
        args = argparse.Namespace(platform_name="", remote_platform_root="", remote_xpfm="")
        with patch.object(
            self.module,
            "get_board_platform_selection",
            return_value={
                "platform_name": "xilinx_u55c_gen3x16_xdma_3_202210_1",
                "remote_platform_root": "erie-hls-generator/platforms/alveo/xilinx_u55c_gen3x16_xdma_3_202210_1",
                "remote_xpfm": "erie-hls-generator/platforms/alveo/xilinx_u55c_gen3x16_xdma_3_202210_1/xilinx_u55c_gen3x16_xdma_3_202210_1.xpfm",
                "source": "upload",
            },
        ):
            result = self.module._resolve_board_platform_selection(
                args,
                "server_6",
                "<REDACTED_LOCAL_PATH>
                {"target_part": "xcu55c-fsvh2892-2L-e"},
                {"project_root_dirname": "erie-hls-generator", "platform_root_path_template": "platforms/alveo/<platform-name>"},
            )

        self.assertEqual(result["remote_xpfm"], "<REDACTED_LOCAL_PATH>

    def test_probe_platform_name_accepts_uploaded_remote_xpfm(self) -> None:
        helper = Mock()
        helper.exec.return_value = "selected_xpfm=<REDACTED_LOCAL_PATH>

        result = self.module._probe_platform_name(
            "server_6",
            Path("settings.json"),
            helper,
            {
                "platform_name": "xilinx_u55c_gen3x16_xdma_3_202210_1",
                "remote_xpfm": "<REDACTED_LOCAL_PATH>
            },
        )

        self.assertEqual(result["status"], self.module.PASS_STATUS)
        self.assertEqual(result["selected_xpfm"], "<REDACTED_LOCAL_PATH>

    def test_wait_for_job_accepts_failed_status_with_nonzero_returncode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp)
            script = skill_dir / "scripts" / "remote_ssh.py"
            settings = skill_dir / "config" / "defaults.json"
            script.parent.mkdir(parents=True)
            settings.parent.mkdir(parents=True)
            script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            settings.write_text("{}\n", encoding="utf-8")
            helper = self.module.ErieHelper(
                {
                    "erie_skill_dir": str(skill_dir),
                    "erie_settings_path": str(settings),
                    "python_env": {},
                },
                timeout=20,
            )
            status_result = subprocess.CompletedProcess(
                ["python", "remote_ssh.py", "status"],
                7,
                "status: failed\nexit_code: 7\n",
                "",
            )

            with patch.object(self.module.subprocess, "run", return_value=status_result):
                result = helper.wait_for_job("server-a", "job-1", poll_s=0, max_wait_s=1)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["returncode"], 7)
        self.assertIn("exit_code: 7", result["output"])

    def test_wait_for_job_retries_transient_status_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp)
            script = skill_dir / "scripts" / "remote_ssh.py"
            settings = skill_dir / "config" / "defaults.json"
            script.parent.mkdir(parents=True)
            settings.parent.mkdir(parents=True)
            script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            settings.write_text("{}\n", encoding="utf-8")
            helper = self.module.ErieHelper(
                {
                    "erie_skill_dir": str(skill_dir),
                    "erie_settings_path": str(settings),
                    "python_env": {},
                },
                timeout=5400,
            )
            timeout_result = subprocess.CompletedProcess(
                ["python", "remote_ssh.py", "status"],
                1,
                "",
                "error: SSH command timed out after 180 seconds.\n",
            )
            success_result = subprocess.CompletedProcess(
                ["python", "remote_ssh.py", "status"],
                0,
                "status: succeeded\nexit_code: 0\n",
                "",
            )

            with patch.object(self.module.subprocess, "run", side_effect=[timeout_result, success_result]) as run_mock:
                with patch.object(self.module.time, "sleep", return_value=None):
                    result = helper.wait_for_job("server-a", "job-1", poll_s=0, max_wait_s=300)

        self.assertEqual(result["status"], "succeeded")
        first_command = run_mock.call_args_list[0].args[0]
        self.assertIn("--timeout", first_command)
        self.assertEqual(first_command[first_command.index("--timeout") + 1], "180")

    def test_request_and_run_uses_capped_control_timeout_for_command_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp)
            script = skill_dir / "scripts" / "remote_ssh.py"
            settings = skill_dir / "config" / "defaults.json"
            script.parent.mkdir(parents=True)
            settings.parent.mkdir(parents=True)
            script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            settings.write_text("{}\n", encoding="utf-8")
            helper = self.module.ErieHelper(
                {
                    "erie_skill_dir": str(skill_dir),
                    "erie_settings_path": str(settings),
                    "python_env": {},
                },
                timeout=5400,
            )

            with patch.object(helper, "_run", return_value="request: command-request.json\n"):
                with patch.object(helper, "_run_with_returncode", return_value=("executed\n", 0)) as execute_mock:
                    request_path = helper.request_and_run(settings, "server-a", "command", ": > payload.b64", "append remote package payload chunk")

        self.assertEqual(request_path, "command-request.json")
        execute_command = execute_mock.call_args.args[0]
        self.assertEqual(execute_command[0], "run-request")
        self.assertEqual(execute_command[execute_command.index("--timeout") + 1], "180")
        self.assertEqual(execute_mock.call_args.kwargs["timeout_s"], 180)

    def test_request_and_run_retries_only_idempotent_timeouts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp)
            script = skill_dir / "scripts" / "remote_ssh.py"
            settings = skill_dir / "config" / "defaults.json"
            script.parent.mkdir(parents=True)
            settings.parent.mkdir(parents=True)
            script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            settings.write_text("{}\n", encoding="utf-8")
            helper = self.module.ErieHelper(
                {
                    "erie_skill_dir": str(skill_dir),
                    "erie_settings_path": str(settings),
                    "python_env": {},
                },
                timeout=5400,
            )
            timeout_result = ("error: SSH command timed out after 180 seconds.\n", 1)
            success_result = ("executed\n", 0)

            with patch.object(helper, "_run", return_value="request: init-request.json\n"):
                with patch.object(helper, "_run_with_returncode", side_effect=[timeout_result, success_result]) as execute_mock:
                    request_path = helper.request_and_run(settings, "server-a", "command", ": > payload.b64", "initialize remote package payload")

        self.assertEqual(request_path, "init-request.json")
        self.assertEqual(execute_mock.call_count, 2)

    def test_request_and_run_does_not_retry_non_idempotent_payload_append(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp)
            script = skill_dir / "scripts" / "remote_ssh.py"
            settings = skill_dir / "config" / "defaults.json"
            script.parent.mkdir(parents=True)
            settings.parent.mkdir(parents=True)
            script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            settings.write_text("{}\n", encoding="utf-8")
            helper = self.module.ErieHelper(
                {
                    "erie_skill_dir": str(skill_dir),
                    "erie_settings_path": str(settings),
                    "python_env": {},
                },
                timeout=5400,
            )

            with patch.object(helper, "_run", return_value="request: append-request.json\n"):
                with patch.object(helper, "_run_with_returncode", return_value=("error: SSH command timed out after 180 seconds.\n", 1)) as execute_mock:
                    with self.assertRaises(self.module.RemoteAcceptanceError):
                        helper.request_and_run(settings, "server-a", "command", "printf %s chunk >> payload.b64", "append remote package payload chunk")

        self.assertEqual(execute_mock.call_count, 1)

    def test_run_server_vitis_phase_reports_status_output_and_tail_log(self) -> None:
        helper = Mock()
        helper.timeout = 120
        helper.request_and_run.return_value = "mkdir-request"
        helper.exec_detached.return_value = {"job_id": "job-1", "manifest": "manifest-1"}
        helper.wait_for_job.return_value = {
            "status": "failed",
            "output": "status: failed\nexit_code: 7\n",
            "returncode": 7,
        }
        helper.tail_log.return_value = "tail line 1\ntail line 2"

        with tempfile.TemporaryDirectory() as tmp:
            package_path = Path(tmp) / "artifacts.tar.gz"
            package_path.write_bytes(b"demo")
            with patch.object(self.module, "_transfer_package_by_request_commands", return_value=["upload-request"]):
                with patch.object(self.module, "_remote_vitis_command", return_value="run-command"):
                    with self.assertRaises(self.module.RemoteAcceptanceError) as exc:
                        self.module._run_server_vitis_phase(
                            helper,
                            Path(tmp),
                            "server-a",
                            {"version": "2022.2", "target_part": "part-a", "settings_script": "/opt/vitis/settings64.sh", "expected_tool": "vitis_hls"},
                            "cosim",
                            package_path,
                            {"remote_tmp_dir": ".remote"},
                            Path(tmp) / "run-dir",
                            phase_label="build",
                            cleanup_remote=False,
                            remote_workdir="remote-workspace-root",
                        )

        message = str(exc.exception)
        self.assertIn("status: failed", message)
        self.assertIn("exit_code: 7", message)
        self.assertIn("tail line 1", message)

    def test_probe_remote_workdir_uses_last_nonempty_line(self) -> None:
        helper = Mock()
        helper.exec.return_value = "\nremote-workspace-root\n"

        workdir = self.module._probe_remote_workdir("server-a", Path("settings.json"), helper)

        self.assertEqual(workdir, "remote-workspace-root")

    def test_run_server_vitis_phase_reports_governed_remote_paths(self) -> None:
        helper = Mock()
        helper.timeout = 120
        helper.request_and_run.return_value = "request-1"
        helper.exec_detached.return_value = {"job_id": "job-1", "manifest": "manifest-1"}
        helper.wait_for_job.return_value = {"status": "succeeded", "output": "status: succeeded\n", "returncode": 0}

        with tempfile.TemporaryDirectory() as tmp:
            package_path = Path(tmp) / "artifacts.tar.gz"
            package_path.write_bytes(b"demo")
            with patch.object(self.module, "_transfer_package_by_request_commands", return_value=["upload-request"]):
                with patch.object(self.module, "_remote_vitis_command", return_value="run-command"):
                    result = self.module._run_server_vitis_phase(
                        helper,
                        Path(tmp),
                        "server-a",
                        {"version": "2022.2", "target_part": "part-a", "settings_script": "/opt/vitis/settings64.sh", "expected_tool": "vitis_hls"},
                        "cosim",
                        package_path,
                        {"directory_contract": {"archive_after_verification": True, "archive_trigger": "after required verification passes"}},
                        Path(tmp) / "run-dir",
                        phase_label="build",
                        cleanup_remote=False,
                        remote_workdir="remote-workspace-root",
                    )

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["remote_project_root"], "erie-hls-generator")
        self.assertEqual(result["remote_conda_prefix"], "erie-hls-generator/.conda/hls-generator")
        self.assertTrue(result["remote_run_dir"].startswith("erie-hls-generator/runs/"))
        self.assertTrue(result["remote_backup_dir"].startswith("erie-hls-generator/backups/"))
        self.assertTrue(result["archived_after_verification"])

    def test_remote_board_command_uses_platform_and_top_function(self) -> None:
        command = self.module._remote_board_command(
            "erie-hls-generator/runs/run-42",
            {
                "settings_script": "/tools/Xilinx/Vitis/2022.2/settings64.sh",
                "platform_name": "xilinx_u55c_gen3x16_xdma_3_202210_1",
                "target_part": "xcu55c-fsvh2892-2L-e",
                "xrt_setup_script": "/opt/xilinx/xrt/setup.sh",
            },
            {
                "top_function": "vector_scale_kernel",
            },
        )

        self.assertIn("HLS_PLATFORM_NAME=", command)
        self.assertIn("vector_scale_kernel", command)
        self.assertIn("run_board_validation.sh", command)

    def test_board_runner_script_emits_board_status_marker(self) -> None:
        text = self.module._board_runner_script("vector_scale_kernel")

        self.assertIn("HLS_BOARD_STATUS", text)
        self.assertIn('"$HLS_VPP_TOOL" -c -t hw', text)
        self.assertIn('export LD_LIBRARY_PATH="$XRT_LIB_DIR${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"', text)
        self.assertIn('-Wl,-rpath,"$XRT_LIB_DIR"', text)
        self.assertIn('sudo -n env LD_LIBRARY_PATH="$LD_LIBRARY_PATH"', text)
        self.assertIn("./host.exe kernel.xclbin 2>&1 | tee board_run.log", text)

    def test_request_upload_and_run_uses_remote_upload_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp) / "erie"
            script = skill_dir / "scripts" / "remote_ssh.py"
            settings = skill_dir / "config" / "defaults.json"
            script.parent.mkdir(parents=True)
            settings.parent.mkdir(parents=True)
            script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            settings.write_text("{}\n", encoding="utf-8")
            helper = self.module.ErieHelper(
                {
                    "erie_skill_dir": str(skill_dir),
                    "erie_settings_path": str(settings),
                    "python_env": {},
                },
                timeout=20,
            )

            calls: list[list[str]] = []

            def fake_run(args):
                calls.append(args)
                if args[0] == "request-upload":
                    return "request: upload-request.json\n"
                if args[0] == "run-request":
                    return "executed\n"
                raise AssertionError(args)

            with patch.object(helper, "_run", side_effect=fake_run):
                request_path = helper.request_upload_and_run(
                    settings,
                    "server_6",
                    Path(tmp) / "payload.tar.gz",
                    "erie-hls-generator/platforms/alveo/payload.tar.gz",
                    "upload U55C platform payload",
                )

        self.assertEqual(request_path, "upload-request.json")
        self.assertEqual(calls[0][0], "request-upload")
        self.assertIn("--local", calls[0])
        self.assertIn("--remote", calls[0])
        self.assertEqual(calls[1][0], "run-request")

    def test_local_platform_upload_prepares_tarball_and_extract_command(self) -> None:
        platform_name = "xilinx_u55c_gen3x16_xdma_3_202210_1"
        with tempfile.TemporaryDirectory() as tmp:
            local_root = Path(tmp) / "u55c"
            local_root.mkdir()
            (local_root / ".dependency_source.json").write_text(
                json.dumps({"board_id": "u55c", "platform_name": platform_name}) + "\n",
                encoding="utf-8",
            )
            (local_root / f"{platform_name}.xpfm").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<sdx:platform xmlns:sdx="http://www.xilinx.com/sdx">
  <sdx:hardwarePlatforms><sdx:reconfigurablePartition sdx:id="0"><sdx:hardwarePlatform sdx:path="hw" sdx:name="hw.xsa"/><sdx:hwEmuPlatform sdx:path="hw_emu" sdx:name="hw_emu.xsa"/></sdx:reconfigurablePartition></sdx:hardwarePlatforms>
  <sdx:softwarePlatforms><sdx:softwarePlatform sdx:path="sw" sdx:name="sw.spfm"/></sdx:softwarePlatforms>
</sdx:platform>
""",
                encoding="utf-8",
            )
            for rel_path in ("hw/hw.xsa", "hw_emu/hw_emu.xsa", "sw/sw.spfm", "license/LICENSE"):
                path = local_root / rel_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"payload")
            run_dir = Path(tmp) / "run"
            helper = Mock()
            helper.request_upload_and_run.return_value = "upload-request"
            helper.request_and_run.return_value = "extract-request"
            selection = {
                "platform_name": platform_name,
                "remote_platform_root": f"<REDACTED_LOCAL_PATH>
                "remote_xpfm": f"<REDACTED_LOCAL_PATH>
            }

            with patch.object(self.module, "set_board_platform_selection") as set_selection:
                result = self.module._upload_local_board_platform_payload(
                    helper,
                    Path(tmp) / "settings.json",
                    "server_6",
                    run_dir,
                    "<REDACTED_LOCAL_PATH>
                    selection,
                    local_root=local_root,
                )

            with tarfile.open(result["archive_path"], "r:gz") as archive:
                names = set(archive.getnames())

        self.assertEqual(result["status"], self.module.PASS_STATUS)
        self.assertIn(f"{platform_name}/{platform_name}.xpfm", names)
        helper.request_upload_and_run.assert_called_once()
        helper.request_and_run.assert_called_once()
        extract_command = helper.request_and_run.call_args.args[3]
        self.assertIn("tar -xzf", extract_command)
        self.assertIn(f"{platform_name}.xpfm", extract_command)
        set_selection.assert_called_once()


if __name__ == "__main__":
    unittest.main()
