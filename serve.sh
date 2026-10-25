#!/bin/sh
# The 50 MB per 1k context estimate applies only to this Ornith/KV setup.
MODEL="${FAMILIAR_MODEL_PATH:-$HOME/Models/ornith-1.0-35b-Q4_K_M.gguf}"

die() { echo "serve.sh: $*" >&2; exit 1; }
number() {
  case "$2" in ''|0*|*[!0-9]*) die "$1 must be a positive integer without leading zeroes";; esac
  [ "${#2}" -le "${3:-9}" ] && [ "$2" -gt 0 ] || die "$1 is outside the supported numeric range"
}

if [ "${SERVE_DRY_RUN:-0}" != 1 ]; then
  [ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ] || die "launcher requires macOS on Apple Silicon; start your local server separately"
  command -v llama-server >/dev/null 2>&1 || die "llama-server is missing; install llama.cpp first"
  [ -f "$MODEL" ] && [ -r "$MODEL" ] || die "model is missing or unreadable: $MODEL"
fi

if [ -z "${RAM_MB:-}" ]; then
  RAM_BYTES=$(sysctl -n hw.memsize) || die "cannot read RAM size"
  number RAM_BYTES "$RAM_BYTES" 15
  RAM_MB=$(( RAM_BYTES / 1048576 ))
fi
number RAM_MB "$RAM_MB"
WIRED=0
if [ -z "${CAP:-}" ]; then
  WIRED=$(sysctl -n iogpu.wired_limit_mb 2>/dev/null || echo 0)
  case "$WIRED" in ''|*[!0-9]*) die "invalid GPU wired limit";; esac
  [ "${#WIRED}" -le 9 ] || die "invalid GPU wired limit"
  if [ "$WIRED" -gt 0 ]; then CAP=$WIRED; else CAP=$(( RAM_MB * 3 / 4 )); fi
fi
number CAP "$CAP"
if [ -z "${MODEL_MB:-}" ]; then
  MODEL_BYTES=$(stat -f%z "$MODEL") || die "cannot read model size"
  number MODEL_BYTES "$MODEL_BYTES" 15
  MODEL_MB=$(( MODEL_BYTES / 1048576 ))
fi
number MODEL_MB "$MODEL_MB"
HEADROOM=$(( CAP - MODEL_MB ))

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

[ "${SERVE_DRY_RUN:-0}" = 1 ] && exit 0

exec llama-server -m "$MODEL" \
  --port 8321 -c "$CTX" -ngl 99 --jinja \
  -fa on --cache-type-k q4_0 --cache-type-v q4_0
