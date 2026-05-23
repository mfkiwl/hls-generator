from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from runtime.hls_generator.validation import validate_generated


def _load_spec() -> dict[str, object]:
    return json.loads((SKILL_ROOT / "assets" / "examples" / "hls_vector_scale_spec.json").read_text(encoding="utf-8"))


def _write_artifacts(root: Path, *, assignment_comment: str) -> None:
    (root / "src").mkdir(parents=True)
    (root / "tb").mkdir()
    (root / "src" / "vector_scale_kernel.h").write_text(
        """// Header declares the vector-scale HLS top function.
#pragma once // Keep declarations single-included for the HLS compile.
#include <ap_int.h> // Provide fixed-width HLS integer types.
// Top function declaration contract: vector_scale_kernel is the hardware boundary.
void vector_scale_kernel(const ap_uint<32> *input, ap_uint<32> *output, ap_uint<16> scale, int length);
""",
        encoding="utf-8",
    )
    (root / "src" / "vector_scale_kernel.cpp").write_text(
        f"""// Source implements the vector-scale HLS datapath.
#include "vector_scale_kernel.h" // Include the top declaration and HLS integer types.
// Top function contract: define the hardware boundary for the vector-scale kernel.
void vector_scale_kernel(const ap_uint<32> *input, ap_uint<32> *output, ap_uint<16> scale, int length) {{
  #pragma HLS INTERFACE mode=m_axi port=input bundle=gmem0 // Map input to the first AXI memory bundle.
  #pragma HLS INTERFACE mode=m_axi port=output bundle=gmem1 // Map output to the second AXI memory bundle.
  #pragma HLS INTERFACE mode=s_axilite port=scale // Expose the scalar multiplier through AXI-Lite.
  #pragma HLS INTERFACE mode=s_axilite port=length // Expose the transaction length through AXI-Lite.
  #pragma HLS INTERFACE mode=s_axilite port=return // Use AXI-Lite for kernel control.
  #pragma HLS PIPELINE II=1 // Request one vector element per cycle.
  for (int i = 0; i < length; ++i) {{ // Loop across only the active vector transaction range.
{assignment_comment}    output[i] = input[i] * scale;
  }} // Close the pipelined vector loop.
}} // Close the HLS top function.
""",
        encoding="utf-8",
    )
    (root / "tb" / "vector_scale_kernel_tb.cpp").write_text(
        """// Testbench validates the vector-scale kernel PASS/FAIL path.
#include "../src/vector_scale_kernel.h" // Reuse the generated kernel declaration.
#include <iostream> // Report the self-check result.
// Testbench entrypoint: run one deterministic C simulation case.
int main() {
  ap_uint<32> input[1] = {1}; // Provide one input sample.
  ap_uint<32> output[1] = {0}; // Capture the kernel output sample.
  vector_scale_kernel(input, output, ap_uint<16>(2), 1); // Exercise one kernel transaction.
  std::cout << "PASS\\n"; // Emit a PASS marker for automation.
  return output[0] == 2 ? 0 : 1; // Fail if the scaled value is wrong.
} // Close the testbench entrypoint.
""",
        encoding="utf-8",
    )
    (root / "hls_config.cfg").write_text(
        """[hls]
syn.top=vector_scale_kernel
syn.file=src/vector_scale_kernel.h
syn.file=src/vector_scale_kernel.cpp
tb.file=tb/vector_scale_kernel_tb.cpp
clock=10.0
""",
        encoding="utf-8",
    )


class CommentCoverageTests(unittest.TestCase):
    def test_uncommented_hls_code_line_is_a_blocking_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_artifacts(root, assignment_comment="")

            report = validate_generated(_load_spec(), root, run_external=False, readiness="static", comment_language="en")

        messages = "\n".join(issue.message for issue in report.issues)
        self.assertFalse(report.ok())
        self.assertIn("comment policy", messages.lower())
        self.assertIn("datapath assignment", messages.lower())

    def test_adjacent_preceding_comment_covers_next_hls_code_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_artifacts(root, assignment_comment="    // Write the scaled sample into output memory for this transaction.\n")

            report = validate_generated(_load_spec(), root, run_external=False, readiness="static", comment_language="en")

        messages = "\n".join(issue.message for issue in report.issues)
        self.assertTrue(report.ok(), messages)
        self.assertNotIn("comment policy", messages.lower())


if __name__ == "__main__":
    unittest.main()
