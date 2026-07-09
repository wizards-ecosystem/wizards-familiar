# Orinth Sidekick

A fully local coding agent. One trusted user, one machine, nothing sent anywhere.

This repo is a complete, portable setup package: the standalone local coding agent and
the model launcher. Use it in place of a cloud agent for tasks it can handle — it runs
entirely on your machine and costs nothing per token.

- **`sidekick.py`** — the entire agent: a stdlib-only Python CLI (no pip installs).
- **`serve.sh`** — starts the model: Ornith-1.0-35B (Q4_K_M GGUF) via llama.cpp's
  OpenAI-compatible `llama-server`. Auto-scales context to the GPU wired limit.
- **Model file** — `~/Models/ornith-1.0-35b-Q4_K_M.gguf` (21.2 GB, SHA-256 verified
  against Hugging Face: `ff25291b…dbec002`).

## Setup on a new machine

1. **Prerequisites** (macOS, Apple Silicon; a 32 GB Mac runs the 35B at Q4):
   ```sh
   brew install llama.cpp        # provides llama-server
   ```
2. **Get the model** (~21 GB) into `~/Models/`:
   ```sh
   mkdir -p ~/Models && curl -L -C - --retry 100 --retry-all-errors \
     -o ~/Models/ornith-1.0-35b-Q4_K_M.gguf \
     "https://huggingface.co/deepreinforce-ai/Ornith-1.0-35B-GGUF/resolve/main/ornith-1.0-35b-Q4_K_M.gguf"
   ```
3. **(Optional) more context** — the default is 64k, which works at the stock GPU wired
   limit. For up to the model's full 256k on a 32 GB Mac, raise the wired limit (per boot):
   ```sh
   sudo sysctl iogpu.wired_limit_mb=28672
   ```
   `serve.sh` detects this and bumps `-c` automatically (65536 → 131072).
4. **Start the model**: `./serve.sh` (leave running; serves port 8321).
5. **Use it**: `sidekick ~/dev/some-repo` (see Quickstart).

## Quickstart

1. **Start the model** (terminal 1, leave it running):
   ```sh
   ~/Documents/Orinth-Sidekick/serve.sh
   ```
   Ready in ~5s, serves port 8321.
2. **Open your project** (terminal 2):
   ```sh
   sidekick ~/dev/some-repo      # or: cd ~/dev/some-repo && sidekick
   ```
3. **Type a task.** Enter submits; **Shift+Enter** or **Alt+Enter** adds a newline;
   paste multi-line text and it stays intact. `/help` lists keys, `/new` clears
   context, `/tokens` shows context usage, ctrl-d quits.

That's the whole "connection": whatever folder Sidekick starts in is the
workspace — the model is told the path, and all file/bash tools work there.
Optional: drop a `SIDEKICK.md` in a repo (build commands, layout, conventions)
and it's added to the system prompt automatically.

Reasoning streams dim, answers stream normal, tool calls print as `→ name arg`.
A live token meter prints above each prompt (`ctx 12.3k / 28k tokens (44%)`), so
you can see how close you are before old turns get dropped; it warns near the
limit and prints a line whenever it trims. The count is the server's **real**
token usage (requested via `stream_options.include_usage`) once you've sent a
turn, and a `~` char-estimate before that. `/tokens` breaks usage down by role
and shows the measured prompt/completion split. Conversation memory defaults to
~28k tokens (`SIDEKICK_CTX_TOKENS`), oldest turns dropped first. The server hard
cap is set by `-c` in serve.sh — 64k (q4 KV cache + flash attention keep it in
RAM; that's the tested ceiling on a 32 GB Mac at the default GPU wired limit —
98k OOMs the Metal compute buffer unless you raise `iogpu.wired_limit_mb`). Raise
`SIDEKICK_CTX_TOKENS` toward the cap (e.g. 55000) for long single tasks that
shouldn't lose their instructions mid-run.

### Input

A small raw-mode editor handles the prompt (stdlib only, no readline dependency):
Enter submits, **Shift+Enter**/**Alt+Enter** insert a newline, and **bracketed
paste** keeps multi-line pastes intact instead of firing on the first newline.
Also: ↑/↓ recall previous prompts, ←/→/Home/End/Ctrl-A/Ctrl-E move, Ctrl-W deletes
a word, Ctrl-U clears the line, Ctrl-C abandons it, Ctrl-D (empty) quits.

Real **Shift+Enter** needs a terminal that speaks the kitty keyboard protocol
(kitty, Ghostty, WezTerm, iTerm2 ≥3.5). Everywhere else — including Terminal.app —
use **Alt+Enter** (Option+Enter, with "Use Option as Meta key" enabled). Paste and
everything else work in any terminal.

Prompts persist across sessions in `~/.sidekick_history` (JSON-per-line, last 1000
recalled; slash commands aren't saved), so ↑ recalls what you typed yesterday.

## How it works

Sidekick sends your conversation plus five tool schemas to the local server and
loops: the model streams either text (shown to you) or tool calls, which Sidekick
executes and feeds back, until the model answers in plain text (max 40 steps).

Tools: `read_file` (line-numbered, 400-line pages) · `write_file` ·
`edit_file` (exact-unique-match replace, or a `start_line`/`end_line` range when an exact
match is awkward) · `multi_edit` (several edits to one file in one atomic call) ·
`bash` (configurable timeout, default 300s — covers ls, grep, git, running code and tests).

History is trimmed oldest-first past ~28k tokens; tool output is capped at 8k chars.

## Config (env vars)

| Var | Default | Purpose |
|---|---|---|
| `SIDEKICK_URL` | `http://localhost:8321/v1` | any OpenAI-compatible server (LM Studio: `http://localhost:1234/v1`) |
| `SIDEKICK_CTX_TOKENS` | `28000` | history budget before old turns are dropped |
| `SIDEKICK_BASH_TIMEOUT` | `300` | per-command bash timeout (s); raise for slow builds/tests |
| `SIDEKICK_MODEL` | `local` | model name sent to the server (llama-server ignores it) |
| `SIDEKICK_HISTFILE` | `~/.sidekick_history` | where prompt history is stored (↑/↓ recall) |

## Measured (M1 Max 32 GB, 2026-07-03)

~38 tok/s generation, ~35 tok/s prompt processing at 16k context (f16 KV). Verified live:
multi-step tool chaining, bug-find-and-fix with minimal edit, multi-file summarize.
Re-measure at the new 32k / q8-KV setting — numbers above predate that change.

## Privacy

Inference is entirely on-device; `sidekick.py` talks only to localhost and has no
telemetry. `serve.sh` loads the model from local disk (`-m`), so the server makes
zero network requests. Git is restricted to read-only subcommands (status, log,
diff, show, branch, blame, …) — commit/merge/checkout/push are blocked in the
`bash` tool. Everything else in `bash` is unrestricted (trusted single user), so
the agent *can* run networked commands like `curl` or `npm install` if you ask.

## Testing

```sh
./sidekick.py --selftest
```

Drives the real agent loop against an in-process mock server (tool call →
execution → final answer) plus edit/bash edge cases, the input editor (layout,
key handling, paste/Shift+Enter parsing), and the token meter. No model needed,
runs in ~1s.

## Troubleshooting

- **Model won't load / out of memory** — close RAM-heavy apps, or raise the GPU
  wired limit: `sudo sysctl iogpu.wired_limit_mb=26000`.
- **"cannot reach http://localhost:8321"** — start `./serve.sh`.
- **Downloading another model on a flaky connection** — llama.cpp's `-hf`
  downloader gives up after 3 drops. Use resumable curl instead:
  `curl -L -C - --retry 100 --retry-delay 5 --retry-all-errors --speed-limit 10240 --speed-time 30 -o file.gguf <resolve-url>`
- **Fallback model** — Qwen3.6-27B Q4_K_M (higher SWE-bench, ~3× slower dense
  decode): download its GGUF, point `serve.sh -m` at it, same everything else.
- **Garbled tool calls** — make sure `--jinja` stayed in `serve.sh`; it selects
  the chat template that formats Ornith's tool calls.
