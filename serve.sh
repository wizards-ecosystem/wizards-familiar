#!/bin/sh
# Local Ornith-1.0-35B server. Context is sized from GPU-wired headroom — see README.
MODEL="${FAMILIAR_MODEL_PATH:-$HOME/Models/ornith-1.0-35b-Q4_K_M.gguf}"

WIRED=$(sysctl -n iogpu.wired_limit_mb 2>/dev/null || echo 0)
if [ "${WIRED:-0}" -gt 0 ]; then
  CAP=${CAP:-$WIRED}
else
  CAP=${CAP:-$(( $(sysctl -n hw.memsize) / 1048576 * 3 / 4 ))}
fi
MODEL_MB=${MODEL_MB:-$(( $(stat -f%z "$MODEL") / 1048576 ))}
RAM_MB=${RAM_MB:-$(( $(sysctl -n hw.memsize) / 1048576 ))}
HEADROOM=$(( CAP - MODEL_MB ))

# ~50 MB of KV per 1k ctx at q4_0, measured on an M1 Max 32 GB.
# Re-measure if you change quant, KV type, or model. Caps at 131072 (highest verified).
CTX=0
for C in 131072 98304 65536 32768 16384; do
  if [ $(( C / 1024 * 50 )) -le "$HEADROOM" ]; then CTX=$C; break; fi
done

SUGGEST=$(( MODEL_MB + 7500 ))
if [ "$CTX" -eq 0 ]; then
  echo "serve.sh: model is ${MODEL_MB} MB but the GPU wired cap is only ${CAP} MB." >&2
  if [ "$SUGGEST" -gt $(( RAM_MB * 90 / 100 )) ]; then
    echo "  ${RAM_MB} MB of RAM can't hold it. Use a smaller model — see README." >&2
  else
    echo "  Raise the cap (per boot):  sudo sysctl iogpu.wired_limit_mb=${SUGGEST}" >&2
  fi
  exit 1
fi

echo "serve.sh: cap=${CAP} MB, model=${MODEL_MB} MB, headroom=${HEADROOM} MB -> -c ${CTX}" >&2
if [ "${WIRED:-0}" -eq 0 ] && [ "$CTX" -lt 131072 ]; then
  echo "  More context (per boot):  sudo sysctl iogpu.wired_limit_mb=${SUGGEST}" >&2
fi

[ -n "$SERVE_DRY_RUN" ] && exit 0

exec llama-server -m "$MODEL" \
  --port 8321 -c "$CTX" -ngl 99 --jinja \
  -fa on --cache-type-k q4_0 --cache-type-v q4_0
