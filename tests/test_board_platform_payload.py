from __future__ import annotations

import importlib
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
import sys


SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

PLATFORM_NAME = "xilinx_u55c_gen3x16_xdma_3_202210_1"


def _load_module():
    return importlib.import_module("runtime.hls_generator.board_platform_payload")


def _write_payload(root: Path, *, omit: str | None = None, platform_name: str = PLATFORM_NAME) -> None:
    root.mkdir(parents=True)
    (root / ".dependency_source.json").write_text(
        json.dumps(
            {
                "board_id": "u55c",
                "platform_name": platform_name,
                "target_profile": "u55c-server",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (root / f"{platform_name}.xpfm").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<sdx:platform sdx:vendor="xilinx" sdx:library="u55c" sdx:name="gen3x16_xdma_3" sdx:version="202210.1" xmlns:sdx="http://www.xilinx.com/sdx">
  <sdx:hardwarePlatforms>
    <sdx:reconfigurablePartition sdx:id="0">
      <sdx:hardwarePlatform sdx:path="hw" sdx:name="hw.xsa"/>
      <sdx:hwEmuPlatform sdx:path="hw_emu" sdx:name="hw_emu.xsa"/>
    </sdx:reconfigurablePartition>
  </sdx:hardwarePlatforms>
  <sdx:softwarePlatforms>
    <sdx:softwarePlatform sdx:path="sw" sdx:name="sw.spfm"/>
  </sdx:softwarePlatforms>
</sdx:platform>
""",
        encoding="utf-8",
    )
    for rel_path in ("hw/hw.xsa", "hw_emu/hw_emu.xsa", "sw/sw.spfm", "license/LICENSE"):
        path = root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if rel_path != omit:
            path.write_bytes(b"payload")


class BoardPlatformPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_module()

    def test_validate_local_u55c_payload_requires_dependency_source_and_xpfm_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "u55c"
            _write_payload(root)

            result = self.module.validate_local_board_platform_payload(root, expected_platform_name=PLATFORM_NAME)

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["platform_name"], PLATFORM_NAME)
        self.assertIn("hw/hw.xsa", result["required_relative_paths"])
        self.assertIn("hw_emu/hw_emu.xsa", result["required_relative_paths"])
        self.assertIn("sw/sw.spfm", result["required_relative_paths"])
        self.assertEqual(result["missing_relative_paths"], [])

    def test_validate_local_u55c_payload_blocks_missing_xpfm_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "u55c"
            _write_payload(root, omit="hw_emu/hw_emu.xsa")

            result = self.module.validate_local_board_platform_payload(root, expected_platform_name=PLATFORM_NAME)

        self.assertEqual(result["status"], "failed")
        self.assertIn("hw_emu/hw_emu.xsa", result["missing_relative_paths"])

    def test_create_platform_archive_uses_platform_name_as_top_level_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "u55c"
            out_dir = Path(tmp) / "reports" / "platform-upload"
            _write_payload(root)
            payload = self.module.validate_local_board_platform_payload(root, expected_platform_name=PLATFORM_NAME)

            archive_path = self.module.create_platform_archive(payload, out_dir)
            with tarfile.open(archive_path, "r:gz") as archive:
                names = set(archive.getnames())

        self.assertIn(f"{PLATFORM_NAME}/{PLATFORM_NAME}.xpfm", names)
        self.assertIn(f"{PLATFORM_NAME}/hw/hw.xsa", names)
        self.assertNotIn("u55c/hw/hw.xsa", names)

    def test_default_u55c_payload_root_uses_workspace_skills_sibling(self) -> None:
        root = self.module.default_local_u55c_payload_root()

        self.assertTrue(
            str(root).endswith(r"VitisDeveloper\skills\.dependencies\board\xilinx\u55c")
            or str(root).endswith("VitisDeveloper/skills/.dependencies/board/xilinx/u55c")
            or str(root).endswith(r".dependencies\board\xilinx\u55c")
            or str(root).endswith(".dependencies/board/xilinx/u55c")
        )
        self.assertNotIn(str(SKILL_ROOT.parent / "VitisDeveloper"), str(root))


if __name__ == "__main__":
    unittest.main()
