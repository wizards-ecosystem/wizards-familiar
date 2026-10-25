#!/bin/sh
cd "$(dirname "$0")" || exit 1
fail=0

want() {
  got=$(CAP=$1 MODEL_MB=$2 RAM_MB=$3 SERVE_DRY_RUN=1 sh ./serve.sh 2>&1 | sed -n 's/.*-c \([0-9]*\).*/\1/p')
  [ "$got" = "$4" ] || { echo "FAIL cap=$1 ram=$3: want $4, got ${got:-none}"; fail=1; }
}

want 28672 20185 32768 131072   # measured: 131k generates at the raised limit
want 24576 20185 32768 65536    # measured: 96k errors at the stock limit, 64k generates
want 49152 20185 65536 131072

out=$(CAP=12288 MODEL_MB=20185 RAM_MB=16384 SERVE_DRY_RUN=1 sh ./serve.sh 2>&1) && {
  echo "FAIL: a 16 GB Mac should exit nonzero, not pick a context"; fail=1; }
case "$out" in
  *"can't hold it"*) ;;
  *) echo "FAIL: 16 GB should advise a smaller model, said: $out"; fail=1;;
esac

for value in bad -1 0 08 '1+2' 1000000000; do
  if CAP=$value MODEL_MB=20185 RAM_MB=32768 SERVE_DRY_RUN=1 sh ./serve.sh >/dev/null 2>&1; then
    echo "FAIL: invalid CAP $value accepted"; fail=1
  fi
done
if CAP=28672 MODEL_MB=bad RAM_MB=32768 SERVE_DRY_RUN=1 sh ./serve.sh >/dev/null 2>&1; then
  echo "FAIL: invalid model size accepted"; fail=1
fi
if CAP=28672 MODEL_MB=20185 RAM_MB=bad SERVE_DRY_RUN=1 sh ./serve.sh >/dev/null 2>&1; then
  echo "FAIL: invalid RAM size accepted"; fail=1
fi

scratch=$(mktemp -d) || exit 1
trap 'rm -rf "$scratch"' EXIT HUP INT TERM
cat > "$scratch/uname" <<'SH'
#!/bin/sh
if [ "$1" = -s ]; then echo "${TEST_PLATFORM:-Darwin}"; else echo arm64; fi
SH
chmod +x "$scratch/uname"
shell=$(command -v sh)
envbin=$(command -v env)
out=$("$envbin" PATH="$scratch" FAMILIAR_MODEL_PATH="$scratch/missing" "$shell" ./serve.sh 2>&1) && {
  echo "FAIL: missing llama-server accepted"; fail=1; }
case "$out" in *"llama-server is missing"*) ;; *) echo "FAIL: missing-server preflight: $out"; fail=1;; esac
printf '#!/bin/sh\nexit 0\n' > "$scratch/llama-server"
chmod +x "$scratch/llama-server"
out=$("$envbin" PATH="$scratch" FAMILIAR_MODEL_PATH="$scratch/missing" "$shell" ./serve.sh 2>&1) && {
  echo "FAIL: missing model accepted"; fail=1; }
case "$out" in *"model is missing or unreadable"*) ;; *) echo "FAIL: missing-model preflight: $out"; fail=1;; esac
out=$(TEST_PLATFORM=Linux "$envbin" PATH="$scratch" "$shell" ./serve.sh 2>&1) && {
  echo "FAIL: non-macOS launch accepted"; fail=1; }
case "$out" in *"requires macOS"*) ;; *) echo "FAIL: platform preflight: $out"; fail=1;; esac

[ $fail -eq 0 ] && echo "serve.sh ctx picker OK"
exit $fail
