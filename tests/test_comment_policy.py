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


GENERIC = "generic generated line, not hardware intent"


def _load_spec() -> dict[str, object]:
    return json.loads((SKILL_ROOT / "assets" / "examples" / "hls_vector_scale_spec.json").read_text(encoding="utf-8"))


def _write_cfg(root: Path) -> None:
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


def _write_generic_artifacts(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "tb").mkdir()
    (root / "src" / "vector_scale_kernel.h").write_text(
        f"""#pragma once // {GENERIC}
#include <ap_int.h> // {GENERIC}
#define LOCAL_FACTOR 2 // misplaced top function and AXI protocol note
typedef ap_uint<32> sample_word_t; // {GENERIC}
struct KernelPorts {{ // {GENERIC}
  sample_word_t factor; // {GENERIC}
}}; // {GENERIC}
void vector_scale_kernel(const ap_uint<32> *input, ap_uint<32> *output, ap_uint<16> scale, int length); // {GENERIC}
""",
        encoding="utf-8",
    )
    (root / "src" / "vector_scale_kernel.cpp").write_text(
        f"""#include "vector_scale_kernel.h" // {GENERIC}
void vector_scale_kernel(const ap_uint<32> *input, ap_uint<32> *output, ap_uint<16> scale, int length) {{ // {GENERIC}
  #pragma HLS INTERFACE mode=m_axi port=input bundle=gmem0 depth=1024 // {GENERIC}
  #pragma HLS INTERFACE mode=m_axi port=output bundle=gmem1 depth=1024 // {GENERIC}
  #pragma HLS INTERFACE mode=s_axilite port=scale // {GENERIC}
  #pragma HLS INTERFACE mode=s_axilite port=length // {GENERIC}
  #pragma HLS INTERFACE mode=s_axilite port=return // {GENERIC}
  #pragma HLS PIPELINE II=1 // {GENERIC}
  int local = LOCAL_FACTOR; // top function boundary comment put on a variable
  for (int i = 0; i < length; ++i) {{ // {GENERIC}
    output[i] = input[i] * scale * local; // {GENERIC}
  }} // {GENERIC}
}} // {GENERIC}
""",
        encoding="utf-8",
    )
    (root / "tb" / "vector_scale_kernel_tb.cpp").write_text(
        f"""#include "../src/vector_scale_kernel.h" // {GENERIC}
#include <iostream> // {GENERIC}
int main() {{ // {GENERIC}
  ap_uint<32> input[1] = {{1}}; // {GENERIC}
  ap_uint<32> output[1] = {{0}}; // {GENERIC}
  vector_scale_kernel(input, output, ap_uint<16>(2), 1); // {GENERIC}
  if (output[0] != 4) {{ // {GENERIC}
    std::cout << "FAIL\\n"; // {GENERIC}
    return 1; // {GENERIC}
  }} // {GENERIC}
  std::cout << "PASS\\n"; // {GENERIC}
  return 0; // {GENERIC}
}} // {GENERIC}
""",
        encoding="utf-8",
    )
    _write_cfg(root)


def _write_policy_artifacts(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "tb").mkdir()
    (root / "src" / "vector_scale_kernel.h").write_text(
        """// Header declares the Vitis HLS vector-scale kernel interface and shared hardware types.
#pragma once // Keep the generated kernel declarations single-included.
#include <ap_int.h> // Provide fixed-width integer types that map cleanly into HLS hardware.
#define LOCAL_FACTOR 2 // Define the compile-time scaling factor used by the datapath contract.
// Hardware sample type contract: one external memory word carries one unsigned sample.
typedef ap_uint<32> sample_word_t;
// Port metadata contract: fields document sample width and control-plane ownership.
struct KernelPorts {
  sample_word_t factor; // Store the local factor with the same width as memory samples.
};
// Top function contract: vector_scale_kernel is the hardware boundary with two m_axi memory ports and AXI-Lite controls.
void vector_scale_kernel(const ap_uint<32> *input, ap_uint<32> *output, ap_uint<16> scale, int length);
""",
        encoding="utf-8",
    )
    (root / "src" / "vector_scale_kernel.cpp").write_text(
        """// Source implements the Vitis HLS vector-scale datapath and interface pragmas.
#include "vector_scale_kernel.h" // Import the top declaration and shared HLS sample type.
// Top function contract: this hardware boundary reads input memory, scales active samples, and writes output memory.
void vector_scale_kernel(const ap_uint<32> *input, ap_uint<32> *output, ap_uint<16> scale, int length) {
  #pragma HLS INTERFACE mode=m_axi port=input bundle=gmem0 depth=1024 // Map input reads to the first AXI master bundle for independent traffic.
  #pragma HLS INTERFACE mode=m_axi port=output bundle=gmem1 depth=1024 // Map output writes to the second AXI master bundle for independent traffic.
  #pragma HLS INTERFACE mode=s_axilite port=scale // Expose the scale factor as an AXI-Lite control register.
  #pragma HLS INTERFACE mode=s_axilite port=length // Expose the active transaction length as an AXI-Lite control register.
  #pragma HLS INTERFACE mode=s_axilite port=return // Use AXI-Lite control for kernel start and completion.
  #pragma HLS PIPELINE II=1 // Request one scaled sample per cycle because each loop iteration has independent memory accesses.
  // Datapath setup: keep the compile-time factor explicit before entering the pipelined loop.
  int local = LOCAL_FACTOR;
  // Pipelined transaction loop: only active vector elements are read and written.
  for (int i = 0; i < length; ++i) {
    output[i] = input[i] * scale * local; // Write one scaled output sample for the current input index.
  }
}
""",
        encoding="utf-8",
    )
    (root / "tb" / "vector_scale_kernel_tb.cpp").write_text(
        """// Testbench validates one deterministic vector-scale transaction and PASS/FAIL reporting.
#include "../src/vector_scale_kernel.h" // Reuse the generated top-level kernel declaration.
#include <iostream> // Emit PASS and FAIL markers for automation.
// Testbench entrypoint: prepare one reference case, call the kernel, and report the verdict.
int main() {
  // Case setup: one input sample exercises the m_axi read path.
  ap_uint<32> input[1] = {1};
  // Expected-result setup: the output buffer captures the scaled sample for comparison.
  ap_uint<32> output[1] = {0};
  // Kernel call: one transaction should multiply by the runtime scale and compile-time factor.
  vector_scale_kernel(input, output, ap_uint<16>(2), 1);
  // FAIL check: any mismatch reports failure for the generated self-checking testbench.
  if (output[0] != 4) {
    std::cout << "FAIL\\n"; // Emit FAIL so automation can detect the failed case.
    return 1;
  }
  // PASS check: the observed output matches the expected scaled sample.
  std::cout << "PASS\\n";
  return 0;
}
""",
        encoding="utf-8",
    )
    _write_cfg(root)


class CommentPolicyTests(unittest.TestCase):
    def test_generic_and_misplaced_comments_are_blocking_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_generic_artifacts(root)

            report = validate_generated(_load_spec(), root, run_external=False, readiness="static", comment_language="en")

        messages = "\n".join(issue.message for issue in report.issues)
        self.assertFalse(report.ok())
        self.assertIn("comment policy", messages.lower())
        self.assertIn("generic", messages.lower())

    def test_typed_comment_policy_allows_uncommented_trivial_closing_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_policy_artifacts(root)

            report = validate_generated(_load_spec(), root, run_external=False, readiness="static", comment_language="en")

        messages = "\n".join(issue.message for issue in report.issues)
        self.assertTrue(report.ok(), messages)
        self.assertNotIn("comment policy", messages.lower())


if __name__ == "__main__":
    unittest.main()
