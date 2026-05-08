# HLS C/C++ Comment Style

Load this reference before changing prompt rules, mock HLS output, or validation
policy for generated C/C++ comments.

## Language Choice

- `en`: use English comments only.
- `zh`: use Chinese comments by default; keep identifiers, tool names, protocol
  names, and AMD/Xilinx terms in their canonical English spelling.
- When the caller uses `auto`, resolve the language from
  `~/.hls-generator/config.json`. If no value is configured, ask the user to
  choose `en` or `zh` before generation.

## Required Coverage

Generated HLS C/C++ should comment hardware intent at these points:

- Top function role and hardware boundary.
- Every external argument's protocol, direction, and bundle/control role.
- Each `#pragma HLS` directive and why it is safe for the access pattern.
- Key loops, including pipeline/dataflow intent and boundary behavior.
- Local buffers, especially partitioned, streamed, or memory-mapped buffers.
- Testbench case setup, expected checks, and PASS/FAIL reporting.

## Style Rules

- Prefer short `//` comments near the code they explain.
- Use `/* ... */` only for a file header or a multi-line hardware constraint.
- Explain why the hardware structure exists; do not restate the next C token.
- Keep comments consistent with generated code and the confirmed HLS spec.
- Do not add commented-out old code, TODO/FIXME placeholders, or line-by-line
  noise comments.
