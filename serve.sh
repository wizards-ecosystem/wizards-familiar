#!/bin/sh
# Ornith-1.0-35B Q4_K_M from local disk — no network access at any point.
# --jinja is required for tool calling. flash attention + q4 KV cache minimise per-token
# KV memory (q4 halves it vs q8; the precision loss is negligible next to the quality lost
# when the model forgets its task mid-run because context was trimmed).
#
# Context auto-scales to the GPU wired limit, because that — not the model (native 262k) —
# is the real ceiling on a 32 GB Mac. VERIFIED:
#   • default wired limit (~24 GB): -c 65536 runs clean; -c 98304 OutOfMemory's the Metal
#     compute buffer at inference (loads, then dies) — so 64k is the safe default.
#   • wired limit raised to 28672 MB: the full 256k loads and runs with zero OOM at ~55 tok/s.
# The bump does NOT survive reboot, so we pick -c from the *current* limit to avoid a config
# that silently OOMs after a restart. To raise it (per boot):
#   sudo sysctl iogpu.wired_limit_mb=28672
# then re-run this script. Bump the high tier to 262144 for the full window if you want it
# (reserves more KV RAM up front). Keep SIDEKICK_CTX_TOKENS below -c (caller sets ~55000 at
# 64k) so Sidekick trims before the server would reject. If the build rejects `-fa on`, use
# a bare `-fa`. Fallback model (slower, higher SWE-bench): Qwen3.6-27B-GGUF Q4_K_M.
WIRED=$(sysctl -n iogpu.wired_limit_mb 2>/dev/null || echo 0)
if [ "${WIRED:-0}" -ge 28000 ]; then CTX=131072; else CTX=65536; fi
echo "serve.sh: wired_limit=${WIRED} MB -> -c ${CTX}" >&2
exec llama-server -m "$HOME/Models/ornith-1.0-35b-Q4_K_M.gguf" \
  --port 8321 -c "$CTX" -ngl 99 --jinja \
  -fa on --cache-type-k q4_0 --cache-type-v q4_0
