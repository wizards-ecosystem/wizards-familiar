#!/bin/sh
# Local Ornith-1.0-35B server. Context auto-scales to the GPU wired limit — see README.
WIRED=$(sysctl -n iogpu.wired_limit_mb 2>/dev/null || echo 0)
if [ "${WIRED:-0}" -ge 28000 ]; then CTX=131072; else CTX=65536; fi
echo "serve.sh: wired_limit=${WIRED} MB -> -c ${CTX}" >&2
exec llama-server -m "$HOME/Models/ornith-1.0-35b-Q4_K_M.gguf" \
  --port 8321 -c "$CTX" -ngl 99 --jinja \
  -fa on --cache-type-k q4_0 --cache-type-v q4_0
