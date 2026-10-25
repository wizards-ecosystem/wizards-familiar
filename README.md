<h1 align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/familiar-logo-dark.png">
    <img src="docs/assets/familiar-logo.png" alt="The Wizard's Familiar" width="420">
  </picture>
</h1>

One local coding agent for small changes. One Python file, standard library only.
Python 3.11+ on Windows, Linux or macOS; no cloud inference or API key.

## Run

Start a local OpenAI-compatible server with native streaming tool calls, then:

```sh
python -m pip install .
familiar /path/to/workspace
# Or: python familiar.py /path/to/workspace
```

No model is downloaded. `serve.sh` / `familiar-serve` optionally launches a separately
provisioned Ornith-1.0-35B Q4_K_M model on macOS on Apple Silicon. Other platforms
start their server separately. Put project instructions in `FAMILIAR.md` (up to 8192 bytes).

## Use

Normal executes commands and edits with your permissions, without a sandbox or
approval prompt. Plan dispatches only native read/list/search. [SECURITY.md](SECURITY.md)
explains the trust boundary. The shell tool uses `/bin/sh` or Windows `COMSPEC`.

`/plan` toggles modes; `/new` clears conversation and keeps the mode. `/compact`
summarizes older observations once in memory, preserving real user messages and recent
complete tool groups; it never resumes tools. Context pauses before losing messages.
`/tokens` shows estimates/reserves, `/draft` shows an unsent rejected prompt, `/help`
lists input controls, and `/quit` exits. Windows/non-TTY input is one line.

Conversations are never saved. Input history is separate and stores private prompts
by default; set `FAMILIAR_HISTORY=0` to disable all history file reads/writes.

## Settings

Set environment variables before starting (`export NAME=value` on POSIX,
`$env:NAME='value'` in PowerShell).

| Variable | Default / meaning |
| --- | --- |
| `FAMILIAR_URL`, `FAMILIAR_MODEL` | `http://127.0.0.1:8321/v1`, `local`; loopback only |
| `FAMILIAR_CTX_TOKENS` | Working ceiling, clamped to discovered server capacity; required if unknown. Minimum 8192 |
| `FAMILIAR_MAX_TOKENS` | `min(2048, window/8)`; allowed 256..window/4 |
| `FAMILIAR_TOKEN_FIELD` | `max_tokens`; or `max_completion_tokens` |
| `FAMILIAR_STREAM_USAGE` | `1`; use `0` if usage requests are unsupported |
| `FAMILIAR_BASH_TIMEOUT`, `FAMILIAR_SHELL` | 300 seconds; optional compatible shell executable |
| `FAMILIAR_HISTORY`, `FAMILIAR_HISTFILE` | `1`; `~/.familiar_history` |
| `FAMILIAR_MODEL_PATH` | Optional launcher's GGUF path |

Budgeting counts serialized UTF-8 messages/tools plus framing, calibrated upward against
usage. It remains an estimate. Requests reserve the completion, `max(256, window/32)`
safety, `min(1024, window/16)` compaction and its instruction, plus bounded next-response
and tool growth. `/tokens` exposes the values; old usage is never labeled current.

## Checks and layout

```sh
python familiar.py --selftest
./test_serve.sh
```

`familiar.py` holds the core and selftests; `serve.sh`/`test_serve.sh` own the optional
macOS launcher. CI covers Windows/Linux/macOS on Python 3.11/3.14.
[AGENTS.md](AGENTS.md) holds contribution rules. [MIT](LICENSE).
