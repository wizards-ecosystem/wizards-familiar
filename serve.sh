#!/bin/sh
# Ornith-1.0-35B Q4_K_M from local disk — no network access at any point.
# --jinja is required for tool calling. q8 KV cache + flash attention halve per-token
# KV memory, so -c 32768 fits the same ~3 GB footprint 16k f16 used to at 32 GB.
# If the build rejects `-fa on`, use a bare `-fa`. If the model fails to load, free RAM,
# raise the GPU wired limit (sudo sysctl iogpu.wired_limit_mb=26000), or drop -c to 24576.
# Fallback model (slower, higher SWE-bench): Qwen3.6-27B-GGUF Q4_K_M
exec llama-server -m "$HOME/Models/ornith-1.0-35b-Q4_K_M.gguf" \
  --port 8321 -c 32768 -ngl 99 --jinja \
  -fa on --cache-type-k q8_0 --cache-type-v q8_0
