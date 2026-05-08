# HLS Library Policy

Load this reference before changing generated C/C++ include policy or adding new HLS library examples.

## Default Allowed Libraries

- `ap_int.h`: use for fixed-width integer datapaths and interface-aligned words.
- `ap_fixed.h`: use when range, integer bits, quantization, and overflow behavior are explicit.
- `hls_stream.h`: use for AXI4-Stream and internal FIFO-style dataflow channels.
- `hls_math.h`: use when synthesizable math functions are required and the numeric strategy is documented.

## Conditional Advanced Libraries

Use these only when the spec explicitly asks for the pattern and tests cover it:

- `hls_vector.h` for SIMD-like vector operations with clear lane width and packing intent.
- `hls_task.h` for task-level data-driven concurrency.
- `hls_streamofblocks.h` for block-stream data movement that replaces PIPO-style buffering.
- `hls_fence.h` only when ordering constraints are necessary to prevent unsafe memory or dataflow reordering.
- `hls_directio.h` only for dynamic control protocols in continuously running kernels.

## Forbidden Or Deprecated Libraries

- Do not use `hls_linear_algebra.h`.
- Do not use old DSP headers such as `hls_dsp.h`.
- Do not use arbitrary-precision C types in `.c` kernels; require C++ and the `ap_*` headers.

## Comment Requirements

When a generated kernel includes an HLS-specific library, comments must explain the hardware reason for the include: width control, FIFO/dataflow communication, math implementation, task concurrency, or ordering.
