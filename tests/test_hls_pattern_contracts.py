from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from runtime.hls_generator.hls_profile import validate_hls_profile
from runtime.hls_generator.model_provider import _hls_pragmas, _mock_hls_header_text, _mock_hls_source_text, _mock_hls_testbench_text, _mock_vectors
from runtime.hls_generator.prompt import render_prompt
from runtime.hls_generator.requirements import build_codegen_plan


def _spec_with_pattern(pattern: str, metadata: dict[str, object] | None = None) -> dict[str, object]:
    base = json.loads((SKILL_ROOT / "assets" / "examples" / "hls_vector_scale_spec.json").read_text(encoding="utf-8"))
    base["name"] = f"{pattern}_kernel"
    base["interfaces"]["top_function"] = f"{pattern}_kernel"
    base["design_requirements"]["confirmed_by_user"] = True
    base["design_requirements"]["confirmation_notes"] = f"Confirmed pattern {pattern}."
    base["hls_profile"] = {
        "example_pattern": pattern,
        "required_metadata_fields": [],
        "metadata": metadata or {},
    }
    return base


class HLSPatternContractTests(unittest.TestCase):
    def test_skill_entry_uses_new_project_structure_reference_name(self) -> None:
        skill_text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("references/hls-project-structure-patterns.md", skill_text)
        self.assertNotIn("references/hls-demo-imported-patterns.md", skill_text)

    def test_project_structure_reference_avoids_source_tied_phrasing(self) -> None:
        text = (SKILL_ROOT / "references" / "hls-project-structure-patterns.md").read_text(encoding="utf-8")
        lowered = text.lower()

        self.assertNotIn("ref/hls_demo", lowered)
        self.assertNotIn("imported", lowered)
        self.assertNotIn("demo set", lowered)
        self.assertNotIn("u50 demo", lowered)
        self.assertNotIn("distilled from", lowered)

    def test_codegen_plan_adds_new_pattern_open_questions(self) -> None:
        cases = {
            "array_partition": {
                "target_buffer": "tile_buf",
                "partition_dim": 1,
                "partition_type": "cyclic",
                "partition_factor": 8,
                "contention_reason": "outer pipeline causes concurrent reads on tile_buf",
            },
            "array_reshape": {
                "target_buffer": "wide_buf",
                "reshape_dim": 1,
                "reshape_type": "complete",
                "adjacent_access_reason": "adjacent elements are consumed in the same loop body",
                "bandwidth_bottleneck": "schedule viewer shows a load bottleneck on wide_buf",
            },
            "dataflow": {
                "stage_boundaries": "read_block -> row_pass -> col_pass -> write_block",
                "channel_kind": "fifo",
                "channel_depth": 16,
                "cosim_required": True,
            },
            "multi_m_axi": {
                "bundle_map": {"input_a": "gmem_a", "input_b": "gmem_b", "output": "gmem_out"},
                "traffic_independence": "input and output channels must not share arbitration",
                "read_write_concurrency": "concurrent read/read/write traffic is required",
            },
            "fixed_point": {
                "numeric_range": "input is normalized to [-1, 1)",
                "integer_bits": 4,
                "quantization_mode": "AP_RND",
                "overflow_mode": "AP_SAT",
                "error_budget": "maximum absolute error <= 1 LSB",
            },
        }
        expected_terms = {
            "array_partition": ["target buffer", "partition dim", "partition type", "partition factor", "contention"],
            "array_reshape": ["target buffer", "reshape dim", "reshape type", "adjacent access", "bandwidth bottleneck"],
            "dataflow": ["stage boundaries", "channel kind", "channel depth", "co-simulation"],
            "multi_m_axi": ["bundle map", "traffic independence", "read/write concurrency"],
            "fixed_point": ["numeric range", "integer bits", "quantization", "overflow", "error budget"],
        }
        for pattern, metadata in cases.items():
            spec = _spec_with_pattern(pattern)
            spec["hls_profile"] = {
                "example_pattern": pattern,
                "required_metadata_fields": list(metadata.keys()),
                "metadata": {},
            }

            plan = build_codegen_plan(spec)
            open_questions = "\n".join(plan["open_questions"]).lower()

            for term in expected_terms[pattern]:
                self.assertIn(term, open_questions, (pattern, open_questions))

    def test_codegen_plan_adds_pattern_specific_open_questions(self) -> None:
        spec = _spec_with_pattern("task_graph")
        spec["hls_profile"] = {
            "example_pattern": "task_graph",
            "required_metadata_fields": [
                "restart_semantics",
                "channel_depth",
                "channel_ownership",
            ],
            "metadata": {},
        }

        plan = build_codegen_plan(spec)
        open_questions = "\n".join(plan["open_questions"])

        self.assertIn("restart semantics", open_questions.lower())
        self.assertIn("channel depth", open_questions.lower())
        self.assertIn("channel ownership", open_questions.lower())

    def test_validate_hls_profile_enforces_extended_profile_fields(self) -> None:
        profile = {
            "example_pattern": "line_buffer_stencil",
            "allowed_libraries": ["hls_task.h", "ap_int.h"],
            "required_headers": ["hls_task.h"],
            "required_pragmas": ["#pragma HLS DATAFLOW"],
            "required_metadata_fields": ["restart_semantics"],
            "metadata": {},
            "forbidden_combinations": [
                {
                    "all_of": [
                        "#pragma HLS ARRAY_PARTITION variable=line_buf complete dim=1",
                        "#pragma HLS ARRAY_RESHAPE variable=line_buf complete dim=1",
                    ],
                    "message": "Do not partition and reshape the same stencil line buffer.",
                }
            ],
            "required_cfg_entries": ["clock=8", "syn.file=src/stencil_kernel.cpp"],
        }
        spec = _spec_with_pattern("line_buffer_stencil")
        spec["hls_profile"] = profile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "stencil_kernel.cpp").write_text(
                '#include <hls_task.h>\n'
                '#include <ap_int.h>\n'
                'void stencil_kernel(const ap_uint<32>* input, ap_uint<32>* output, int length) {\n'
                '  #pragma HLS ARRAY_PARTITION variable=line_buf complete dim=1\n'
                '  #pragma HLS ARRAY_RESHAPE variable=line_buf complete dim=1\n'
                '}\n',
                encoding="utf-8",
            )
            (root / "hls_config.cfg").write_text("syn.file=src/stencil_kernel.cpp\n", encoding="utf-8")
            issues = validate_hls_profile(profile, root, spec)

        messages = "\n".join(item["message"] for item in issues)
        self.assertIn("missing metadata", messages.lower())
        self.assertIn("dataflow", messages.lower())
        self.assertIn("line buffer", messages.lower())
        self.assertIn("clock=8", messages.lower())

    def test_render_prompt_injects_pattern_specific_rules(self) -> None:
        vector_spec = _spec_with_pattern(
            "vector_lane",
            metadata={"lane_width": 4, "pack_intent": "pack adjacent samples into a lane vector"},
        )
        vector_spec["hls_profile"].update(
            {
                "required_metadata_fields": ["lane_width", "pack_intent"],
                "required_headers": ["hls_vector.h"],
            }
        )
        vector_prompt = render_prompt(vector_spec, comment_language="en")

        directio_spec = _spec_with_pattern(
            "directio_freerun",
            metadata={"free_running": True, "control_protocol": "ap_ctrl_none"},
        )
        directio_spec["hls_profile"].update(
            {
                "required_metadata_fields": ["free_running", "control_protocol"],
                "required_headers": [],
            }
        )
        directio_prompt = render_prompt(directio_spec, comment_language="en")

        self.assertIn("lane width", vector_prompt.lower())
        self.assertIn("hls_vector.h", vector_prompt)
        self.assertIn("free-running", directio_prompt.lower())
        self.assertIn("ap_ctrl_none", directio_prompt)

    def test_render_prompt_injects_tutorial_derived_rules_for_new_patterns(self) -> None:
        array_reshape_spec = _spec_with_pattern(
            "array_reshape",
            metadata={
                "target_buffer": "wide_buf",
                "reshape_dim": 1,
                "reshape_type": "complete",
                "adjacent_access_reason": "adjacent elements are consumed together",
                "bandwidth_bottleneck": "schedule viewer shows a load bottleneck on wide_buf",
            },
        )
        array_reshape_spec["hls_profile"].update(
            {
                "required_metadata_fields": [
                    "target_buffer",
                    "reshape_dim",
                    "reshape_type",
                    "adjacent_access_reason",
                    "bandwidth_bottleneck",
                ],
            }
        )
        array_reshape_prompt = render_prompt(array_reshape_spec, comment_language="en")

        dataflow_spec = _spec_with_pattern(
            "dataflow",
            metadata={
                "stage_boundaries": "read -> compute -> write",
                "channel_kind": "fifo",
                "channel_depth": 16,
                "cosim_required": True,
            },
        )
        dataflow_spec["interfaces"]["arguments"] = [
            {"name": "input", "type": "const ap_uint<32> *", "direction": "input", "interface": "m_axi", "bundle": "gmem0", "depth": 1024},
            {"name": "output", "type": "ap_uint<32> *", "direction": "output", "interface": "m_axi", "bundle": "gmem1", "depth": 1024},
            {"name": "rows", "type": "int", "direction": "input", "interface": "s_axilite"},
            {"name": "cols", "type": "int", "direction": "input", "interface": "s_axilite"},
        ]
        dataflow_spec["hls_profile"].update(
            {
                "required_metadata_fields": [
                    "stage_boundaries",
                    "channel_kind",
                    "channel_depth",
                    "cosim_required",
                ],
            }
        )
        dataflow_prompt = render_prompt(dataflow_spec, comment_language="en")

        multi_m_axi_spec = _spec_with_pattern(
            "multi_m_axi",
            metadata={
                "bundle_map": {"input_a": "gmem_a", "input_b": "gmem_b", "output": "gmem_out"},
                "traffic_independence": "three independent memory channels",
                "read_write_concurrency": "read/read/write overlap is required",
            },
        )
        multi_m_axi_spec["hls_profile"].update(
            {
                "required_metadata_fields": ["bundle_map", "traffic_independence", "read_write_concurrency"],
            }
        )
        multi_m_axi_prompt = render_prompt(multi_m_axi_spec, comment_language="en")

        fixed_point_spec = _spec_with_pattern(
            "fixed_point",
            metadata={
                "numeric_range": "[-1, 1)",
                "integer_bits": 4,
                "quantization_mode": "AP_RND",
                "overflow_mode": "AP_SAT",
                "error_budget": "1 LSB",
            },
        )
        fixed_point_spec["hls_profile"].update(
            {
                "required_metadata_fields": [
                    "numeric_range",
                    "integer_bits",
                    "quantization_mode",
                    "overflow_mode",
                    "error_budget",
                ],
            }
        )
        fixed_point_prompt = render_prompt(fixed_point_spec, comment_language="en")

        self.assertIn("outer loop", array_reshape_prompt.lower())
        self.assertIn("schedule-viewer", array_reshape_prompt.lower())
        self.assertIn("read/compute/write", dataflow_prompt.lower())
        self.assertIn("co-simulation", dataflow_prompt.lower())
        self.assertIn("bundle map", multi_m_axi_prompt.lower())
        self.assertIn("read/write concurrency", multi_m_axi_prompt.lower())
        self.assertIn("quantization", fixed_point_prompt.lower())
        self.assertIn("error budget", fixed_point_prompt.lower())

    def test_render_prompt_injects_project_structure_rules_for_new_patterns(self) -> None:
        minimal_spec = json.loads((SKILL_ROOT / "assets" / "examples" / "hls_minimal_vitis_pipeline_spec.json").read_text(encoding="utf-8"))
        minimal_prompt = render_prompt(minimal_spec, comment_language="en").lower()

        split_spec = json.loads((SKILL_ROOT / "assets" / "examples" / "hls_host_kernel_split_spec.json").read_text(encoding="utf-8"))
        split_prompt = render_prompt(split_spec, comment_language="en").lower()

        self.assertIn("compile/link", minimal_prompt)
        self.assertIn("do not mix package or host orchestration", minimal_prompt)
        self.assertIn("stable bundle", minimal_prompt)
        self.assertIn("hotspot", split_prompt)
        self.assertIn("helper header", split_prompt)
        self.assertNotIn("generate host code", split_prompt)

    def test_new_project_structure_examples_keep_hls_only_outputs(self) -> None:
        for name in ("hls_minimal_vitis_pipeline_spec.json", "hls_host_kernel_split_spec.json"):
            spec = json.loads((SKILL_ROOT / "assets" / "examples" / name).read_text(encoding="utf-8"))
            outputs = [entry["path"] for entry in spec["outputs"]]
            self.assertTrue(any(path.endswith(".h") for path in outputs), outputs)
            self.assertTrue(any(path.endswith(".cpp") for path in outputs), outputs)
            self.assertTrue(any(path.endswith(".cfg") for path in outputs), outputs)
            self.assertFalse(any("/host/" in path.replace("\\", "/").lower() for path in outputs), outputs)
            self.assertFalse(any("/package/" in path.replace("\\", "/").lower() for path in outputs), outputs)

    def test_examples_declare_board_acceptance_explicitly(self) -> None:
        board_runnable = json.loads((SKILL_ROOT / "assets" / "examples" / "hls_vector_scale_spec.json").read_text(encoding="utf-8"))
        board_exempt = json.loads((SKILL_ROOT / "assets" / "examples" / "hls_task_graph_axis_spec.json").read_text(encoding="utf-8"))

        self.assertEqual(board_runnable["workflow"]["board_acceptance"]["profile"], "u55c_m_axi_host")
        self.assertEqual(board_runnable["workflow"]["board_acceptance"]["host_template"], "vector_scale_host")
        self.assertEqual(board_exempt["workflow"]["board_acceptance"]["profile"], "not_board_runnable")
        self.assertTrue(board_exempt["workflow"]["board_acceptance"]["reason"])

    def test_host_kernel_split_mock_uses_increment_semantics(self) -> None:
        spec = json.loads((SKILL_ROOT / "assets" / "examples" / "hls_host_kernel_split_spec.json").read_text(encoding="utf-8"))

        vectors = _mock_vectors(spec)
        source_text = _mock_hls_source_text(spec, "host_kernel_split_kernel.h", "en")

        self.assertEqual(vectors[0]["expected_outputs"]["output"], [2, 3, 4])
        self.assertIn("output[i] = input[i] + 1;", source_text)

    def test_task_graph_mock_header_puts_hls_task_before_hls_stream(self) -> None:
        spec = _spec_with_pattern(
            "task_graph",
            metadata={
                "restart_semantics": "per_transaction_restart",
                "channel_depth": 16,
                "channel_ownership": "reader -> compute -> writer",
            },
        )
        spec["hls_profile"].update({"required_headers": ["hls_task.h"]})

        header_text = _mock_hls_header_text(spec, "en")

        self.assertLess(header_text.index("#include <hls_task.h>"), header_text.index("#include <hls_stream.h>"))

    def test_task_graph_top_level_pragmas_do_not_mix_pipeline_with_dataflow(self) -> None:
        spec = _spec_with_pattern(
            "task_graph",
            metadata={
                "restart_semantics": "per_transaction_restart",
                "channel_depth": 16,
                "channel_ownership": "reader -> compute -> writer",
            },
        )

        pragma_text = _hls_pragmas(spec)

        self.assertIn("#pragma HLS DATAFLOW", pragma_text)
        self.assertNotIn("#pragma HLS PIPELINE II=1", pragma_text)

    def test_task_graph_mock_source_uses_hls_task_actor(self) -> None:
        spec = json.loads((SKILL_ROOT / "assets" / "examples" / "hls_task_graph_axis_spec.json").read_text(encoding="utf-8"))

        source_text = _mock_hls_source_text(spec, "task_graph_kernel.h", "en")

        self.assertIn("hls::task compute_stage", source_text)
        self.assertIn("load_task_graph_memory_increment", source_text)
        self.assertIn("compute_stage", source_text)
        self.assertIn("store_task_graph_memory_increment", source_text)
        self.assertIn("#pragma HLS PIPELINE II=1 style=flp", source_text)
        self.assertNotIn("hls_thread_local hls::task", source_text)
        self.assertNotIn("compute_task_graph_memory_increment(task_stream, task_result_stream, task_count_stream);", source_text)

    def test_task_graph_profile_requires_hls_task_usage(self) -> None:
        spec = _spec_with_pattern(
            "task_graph",
            metadata={
                "restart_semantics": "per_transaction_restart",
                "channel_depth": 16,
                "channel_ownership": "reader -> compute -> writer",
            },
        )
        profile = {
            "example_pattern": "task_graph",
            "required_headers": ["hls_task.h"],
            "required_metadata_fields": ["restart_semantics", "channel_depth", "channel_ownership"],
            "metadata": spec["hls_profile"]["metadata"],
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "task_graph_kernel.cpp").write_text(
                '#include <hls_task.h>\n'
                '#include <hls_stream.h>\n'
                'void task_graph_kernel(hls::stream<int>& in_stream, hls::stream<int>& out_stream, int length) {\n'
                '  #pragma HLS DATAFLOW\n'
                '  (void)length;\n'
                '  out_stream.write(in_stream.read());\n'
                '}\n',
                encoding="utf-8",
            )
            (root / "hls_config.cfg").write_text("syn.file=src/task_graph_kernel.cpp\n", encoding="utf-8")
            issues = validate_hls_profile(profile, root, spec)

        messages = "\n".join(item["message"] for item in issues)
        self.assertIn("instantiate hls::task explicitly", messages)
        self.assertIn("flushing or free-running pipeline style", messages)

    def test_task_graph_testbench_uses_standard_memory_cases(self) -> None:
        spec = json.loads((SKILL_ROOT / "assets" / "examples" / "hls_task_graph_axis_spec.json").read_text(encoding="utf-8"))
        vectors = [
            {
                "id": "case_nominal",
                "inputs": {"input": [1, 2, 3], "length": 3},
                "expected_outputs": {"output": [2, 3, 4]},
            },
            {
                "id": "case_boundary",
                "inputs": {"input": [0, 15], "length": 2},
                "expected_outputs": {"output": [1, 16]},
            },
        ]

        tb_text = _mock_hls_testbench_text(spec, vectors, "hash", "en")

        self.assertIn("task_graph_memory_increment_kernel(input, output, 3);", tb_text)
        self.assertIn("task_graph_memory_increment_kernel(input, output, 2);", tb_text)
        self.assertNotIn("one combined kernel transaction", tb_text)

    def test_dataflow_2d_block_transform_mock_source_uses_explicit_stage_helpers(self) -> None:
        spec = json.loads((SKILL_ROOT / "assets" / "examples" / "hls_2d_block_transform_spec.json").read_text(encoding="utf-8"))

        source_text = _mock_hls_source_text(spec, "block_transform_kernel.h", "en")

        self.assertIn("#pragma HLS DATAFLOW", source_text)
        self.assertIn("read_block", source_text)
        self.assertIn("row_pass", source_text)
        self.assertIn("transpose_or_reorder", source_text)
        self.assertIn("col_pass", source_text)
        self.assertIn("write_block", source_text)
        self.assertIn("rows", source_text)
        self.assertIn("cols", source_text)


if __name__ == "__main__":
    unittest.main()
