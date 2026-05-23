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

from runtime.hls_generator.user_config import get_board_platform_selection, get_vitis_selection, load_user_config, set_board_platform_selection, set_vitis_selection


class UserConfigTests(unittest.TestCase):
    def test_set_and_get_board_platform_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            old = os.environ.get("HLS_GENERATOR_USER_CONFIG")
            os.environ["HLS_GENERATOR_USER_CONFIG"] = str(config_path)
            try:
                set_board_platform_selection(
                    "server_6",
                    {
                        "platform_name": "xilinx_u55c_gen3x16_xdma_3_202210_1",
                        "remote_platform_root": "<REDACTED_LOCAL_PATH>
                        "remote_xpfm": "<REDACTED_LOCAL_PATH>
                        "source": "upload",
                    },
                )
                loaded = load_user_config()
            finally:
                if old is None:
                    os.environ.pop("HLS_GENERATOR_USER_CONFIG", None)
                else:
                    os.environ["HLS_GENERATOR_USER_CONFIG"] = old

        selection = get_board_platform_selection("server_6", loaded)
        assert selection is not None
        self.assertEqual(selection["platform_name"], "xilinx_u55c_gen3x16_xdma_3_202210_1")
        self.assertEqual(selection["source"], "upload")
        self.assertIn("selected_at", selection)

    def test_set_vitis_selection_preserves_optional_remote_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            old = os.environ.get("HLS_GENERATOR_USER_CONFIG")
            os.environ["HLS_GENERATOR_USER_CONFIG"] = str(config_path)
            try:
                set_vitis_selection(
                    "server_6",
                    {
                        "version": "2022.2",
                        "settings_script": "/tools/Xilinx/Vitis/2022.2/settings64.sh",
                        "expected_tool": "vitis_hls",
                        "target_part": "xcu55c-fsvh2892-2L-e",
                        "vpp_path": "/tools/Xilinx/Vitis/2022.2/bin/v++",
                        "xrt_tool_path": "/opt/xilinx/xrt/bin/xrt-smi",
                        "xrt_setup_script": "/opt/xilinx/xrt/setup.sh",
                        "xbmgmt_tool_path": "/opt/xilinx/xrt/bin/xbmgmt",
                    },
                )
                stored = json.loads(config_path.read_text(encoding="utf-8"))
            finally:
                if old is None:
                    os.environ.pop("HLS_GENERATOR_USER_CONFIG", None)
                else:
                    os.environ["HLS_GENERATOR_USER_CONFIG"] = old

        selection = get_vitis_selection("server_6", stored)
        assert selection is not None
        self.assertEqual(selection["vpp_path"], "/tools/Xilinx/Vitis/2022.2/bin/v++")
        self.assertEqual(selection["xrt_tool_path"], "/opt/xilinx/xrt/bin/xrt-smi")
        self.assertEqual(selection["xrt_setup_script"], "/opt/xilinx/xrt/setup.sh")
        self.assertEqual(selection["xbmgmt_tool_path"], "/opt/xilinx/xrt/bin/xbmgmt")


if __name__ == "__main__":
    unittest.main()
