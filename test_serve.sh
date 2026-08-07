#!/bin/sh
# ponytail: one check — the ctx picker at the boundaries we actually measured on an M1 Max 32 GB.
cd "$(dirname "$0")" || exit 1
fail=0

want() {
  got=$(CAP=$1 MODEL_MB=$2 RAM_MB=$3 SERVE_DRY_RUN=1 sh ./serve.sh 2>&1 | sed -n 's/.*-c \([0-9]*\).*/\1/p')
  [ "$got" = "$4" ] || { echo "FAIL cap=$1 ram=$3: want $4, got ${got:-none}"; fail=1; }
}

want 28672 20185 32768 131072   # measured: 131k generates at the raised limit
want 24576 20185 32768 65536    # measured: 96k errors at the stock limit, 64k generates
want 49152 20185 65536 131072   # 64 GB Mac needs no sudo

# 16 GB: must refuse, and must not advise a cap the machine can't reach
out=$(CAP=12288 MODEL_MB=20185 RAM_MB=16384 SERVE_DRY_RUN=1 sh ./serve.sh 2>&1) && {
  echo "FAIL: a 16 GB Mac should exit nonzero, not pick a context"; fail=1; }
case "$out" in
  *"can't hold it"*) ;;
  *) echo "FAIL: 16 GB should advise a smaller model, said: $out"; fail=1;;
esac

[ $fail -eq 0 ] && echo "serve.sh ctx picker OK"
exit $fail
