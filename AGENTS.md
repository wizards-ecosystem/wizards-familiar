# Working standards for The Wizard's Familiar

These apply to people and to AI agents equally. Read this before changing anything.

## What this project is

One stdlib-only Python file that talks to a local `llama-server` and edits code. The
constraints below are the product, not incidental.

| Constraint | Why |
| --- | --- |
| Zero runtime dependencies | A tool that edits your files should not arrive with a dependency tree to audit. CI enforces it |
| One file | It stays small enough to read end to end. Do not split `familiar.py` into a package |
| No cloud, no API key | Everything runs on the machine in front of you |
| macOS on Apple Silicon | The only platform that is tested and supported. `serve.sh` uses BSD `sysctl` and `stat` |

A change that trades one of these away for a feature is the wrong trade. Say so in the
pull request if you think an exception is warranted.

## Where things are

| Path | What |
| --- | --- |
| `familiar.py` | The whole agent. Config, tools, HTTP, context management, agent loop, line editor, REPL, and `selftest()` |
| `serve.sh` | Launches `llama-server` and sizes the context window from GPU memory headroom |
| `test_serve.sh` | Four boundary cases for `serve.sh`'s context picker |
| `FAMILIAR.md` | Notes the agent reads into its own prompt |
| `SECURITY.md` | The trust model. Read it before changing `tool_bash` or `git_guard` |
| `pyproject.toml` | Packaging. The dependency list is empty and CI checks that it stays empty |
| `.github/workflows/ci.yml` | The checks |

## Checks

```sh
./familiar.py --selftest
./test_serve.sh
```

Both must pass. The selftest stands up two mock servers and drives the real agent loop,
so it is a genuine test rather than a smoke check. A behavior change without a matching
case in `selftest()` is incomplete.

## Working on the loop-breakers

Several pieces exist because a 35B local model is weaker than a hosted one and gets
stuck: the re-read guard (`_READ_SEEN`), the consecutive-duplicate breaker (`prev_key`
in `agent_turn`), the verify-after-edit nudge, bare `cat` routed through `read_file`,
and directory reads listed rather than errored. They look like workarounds because they
are. Each has a selftest case pinning the behavior it was written for. Do not remove one
without reading that case first.

## Prose

Follow the ecosystem [brand guide](https://github.com/wizards-ecosystem/.github/blob/main/BRAND.md):
sentence case headings, plain writing for an engineer who will check, no hype, and
public wording that does not outrun the evidence. Write **The Wizard's Familiar** in
full on a first mention and **Familiar** afterwards. The repository slug is never a
display name.

The terminal interface keeps its own punctuation and glyphs. That is deliberate and the
brand guide allows it.

## Contributing

The organization's [CONTRIBUTING](https://github.com/wizards-ecosystem/.github/blob/main/CONTRIBUTING.md),
[code of conduct](https://github.com/wizards-ecosystem/.github/blob/main/CODE_OF_CONDUCT.md),
and issue and pull request templates apply. This repository does not override them.
