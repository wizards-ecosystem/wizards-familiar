#!/bin/sh
# Ornith-1.0-35B Q4_K_M from local disk — no network access at any point.
# --jinja is required for tool calling. flash attention + q4 KV cache minimise per-token
# KV memory. q4 KV (was q8) halves the per-token footprint, buying ~3x the window so a
# delegated coding task finishes before context is trimmed — the slight KV-precision loss
# is negligible next to the quality lost when the model forgets its task mid-run.
#
# -c 65536 (64k) is the tested ceiling on this 32 GB Mac at the DEFAULT GPU wired
# limit: the 20 GB weights + q4 KV + Metal compute buffers fit; -c 98304 does NOT
# (it loads but OutOfMemory's the compute buffer at inference — verified). To push
# past 64k you must first raise the wired limit, which needs sudo:
#   sudo sysctl iogpu.wired_limit_mb=26000   # then -c 98304 fits (~96k)
# If a task still overflows 64k, step -c down to 49152. Keep SIDEKICK_CTX_TOKENS
# below -c (the caller sets ~55000) so Sidekick trims before the server rejects.
# If the build rejects `-fa on`, use a bare `-fa`.
# Fallback model (slower, higher SWE-bench): Qwen3.6-27B-GGUF Q4_K_M
exec llama-server -m "$HOME/Models/ornith-1.0-35b-Q4_K_M.gguf" \
  --port 8321 -c 65536 -ngl 99 --jinja \
  -fa on --cache-type-k q4_0 --cache-type-v q4_0
