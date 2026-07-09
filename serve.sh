#!/bin/sh
# Ornith-1.0-35B Q4_K_M from local disk — no network access at any point.
# --jinja is required for tool calling. flash attention + q4 KV cache minimise per-token
# KV memory. q4 KV (was q8) halves the per-token footprint, buying ~3x the window so a
# delegated coding task finishes before context is trimmed — the slight KV-precision loss
# is negligible next to the quality lost when the model forgets its task mid-run.
#
# -c 98304 (~96k) is the target on a 32 GB Mac. If the model won't load beside the
# 21.2 GB weights: raise the GPU wired limit first
#   sudo sysctl iogpu.wired_limit_mb=26000
# then, if still tight, step -c down to 65536, then 49152.
# If the build rejects `-fa on`, use a bare `-fa`.
# Fallback model (slower, higher SWE-bench): Qwen3.6-27B-GGUF Q4_K_M
exec llama-server -m "$HOME/Models/ornith-1.0-35b-Q4_K_M.gguf" \
  --port 8321 -c 98304 -ngl 99 --jinja \
  -fa on --cache-type-k q4_0 --cache-type-v q4_0
