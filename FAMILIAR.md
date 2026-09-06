# Project notes

Familiar reads this file at startup and appends it to its system prompt. It is notes
for the agent, not documentation for people. See AGENTS.md for that.

## Layout

`familiar.py` is the entire agent. `serve.sh` launches the model. Everything else is
documentation or packaging.

## Checks

```sh
./familiar.py --selftest   # the agent loop, edit tools, git guard, line editor. ~1s, no model
./test_serve.sh            # serve.sh's context picker at its measured boundaries
```

Run both before saying a change is done. The selftest is the real gate: it drives the
actual agent loop against two mock servers, so it catches most regressions.

## Rules that matter here

- Zero runtime dependencies. Standard library only. CI fails if a dependency appears.
- The single-file shape is the point. Do not split `familiar.py` into a package.
- A change to behavior gets a case in `selftest()` in the same commit.
- Do not touch the `Ornith` spellings. That is the upstream model's name, not ours.
