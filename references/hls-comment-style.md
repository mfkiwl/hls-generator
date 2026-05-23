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

## Required Typed Placement

Generated HLS C/C++ must satisfy typed comment placement. The validator checks
the structure being commented, not just the presence of `//`. Generic,
misplaced, or type-mismatched comments are blocking errors.

- File header: every `.h`, `.hpp`, `.cpp`, `.cc`, and `.cxx` file starts with a
  short comment describing the file role. This does not replace local comments.
- Functions and methods: place the contract comment on the immediately
  preceding comment-only line. Explain top role, hardware boundary, interface
  summary, or testbench entrypoint.
- Includes, macros, and `#pragma HLS`: use same-line intent comments. Explain
  dependency purpose, compile-time contract, or synthesis/interface safety.
- Types: place struct/class/typedef/using/enum contract comments immediately
  before the definition. Field comments may stay same-line for width,
  direction, or protocol role.
- Local variables, loops, assignments, and functional steps: use a block-leading
  comment for the step and same-line comments for critical writes, boundary
  checks, or protocol conversions.
- Testbenches: comment `main()`, case setup, expected values, kernel calls, and
  PASS/FAIL reporting with the same typed placement policy.
- Trivial lines: do not force comments onto plain braces, ordinary closing
  lines, or simple `return` statements.

The comments should preserve hardware intent at these points: top function
role and hardware boundary, every external argument's protocol/direction/bundle
role, each HLS pragma, key loops, local buffers, and testbench checks.

## Style Rules

- Prefer short `//` comments near the code they explain.
- Use `/* ... */` only for a file header or a multi-line hardware constraint.
- Explain why the hardware structure exists; do not restate the next C token.
- Keep comments consistent with generated code and the confirmed HLS spec.
- Do not add filler comments such as "generic generated line", commented-out old
  code, TODO/FIXME placeholders, contradictory comments, misplaced top-function
  notes, or forced translations of protocol/tool names.
