# BhavAI: five changes worth taking

Status: **reviewed, nothing implemented.** This file is the whole output of the review. Each
item below was checked against `familiar.py` at the time of writing, so the gaps described are
real rather than assumed, but no code has been changed.

## Where this came from

BhavAI Terminal Edition is a third-party terminal coding agent in Python: a Reason-Act-Observe
loop, a sandboxed tool set, and a Sarvam or Groq backend. It was cloned into the ecosystem's
investigation tree, read for technique, and deleted. It solves the same problem The Wizard's
Familiar solves, for the same reason — a weaker model needs more scaffolding than a strong one —
so its workarounds are worth knowing even though its code cannot be used.

| | |
| --- | --- |
| Upstream | https://github.com/BhavneeshBanga/BhavAI-Terminal-Edition |
| Reviewed at | `67e329da154afc0722c080ae6af9b79e2fdf3b94` |
| Size | ~7,200 lines, 30 modules, 9 runtime dependencies |

**Its licence forbids reuse of its code.** `LICENSE.md` is "BhavAI Community License v1.0, All
Rights Reserved", and section 3 prohibits derivative works and using the source in another
project. Its `pyproject.toml` separately claims MIT; the licence file governs, and the
contradiction is a reason for caution rather than a loophole. Nothing below is a copy. Each item
is a technique described so it can be written from scratch, which is how it has to be done.

Every item is stdlib-only and none of them splits `familiar.py`, so the constraints in
[AGENTS.md](../../AGENTS.md) hold. Per that file, a behaviour change without a matching case in
`selftest()` is incomplete, so each item names the case it needs.

---

## 1. Widen the duplicate-call breaker to a window

**The gap is real.** `prev_key` in `agent_turn` ([familiar.py:392](../../familiar.py#L392),
compared at [familiar.py:414](../../familiar.py#L414)) holds exactly one key and catches only a
back-to-back repeat. A model that alternates two useless calls — read A, read B, read A, read B —
never trips it and burns the full `MAX_STEPS`. That is the most likely real loop for a 35B, and
it is currently invisible.

Two changes, both small:

- **Keep a short window instead of one key.** A list of the last four keys, trimmed on each
  append, tripping when the incoming key already appears some threshold of times. BhavAI uses
  three occurrences within four. The A-B-A-B case then trips on the second A.
- **Canonicalise the key.** The key today is the raw `arguments` string from the model, so two
  calls differing only in key order or whitespace are the same call and compare unequal. Parse
  and re-serialise with `json.dumps(args, sort_keys=True)` instead. The parsed `args` is already
  in hand at [familiar.py:409](../../familiar.py#L409), so this costs nothing.

Keep the existing behaviour of not executing the tool and feeding a note back as the result.

One thing in BhavAI's version is better than ours and worth copying in substance: its message
tells the model it already has the answer in an earlier observation, and that if the call is
genuinely needed again it must justify it in its `thought` field first. Ours says repeating
"changes nothing", which is a statement rather than an instruction and leaves no legitimate route
to a repeat. Give the model somewhere to go.

**Selftest:** extend the duplicate-breaker case at [familiar.py:853](../../familiar.py#L853) with an
A-B-A-B script that must break, plus a case pinning that reordered JSON keys compare equal.

## 2. Continue a completion that stopped on `max_tokens`

**The gap is real.** `chat()` reads `choices[0].delta` ([familiar.py:302](../../familiar.py#L302))
and never looks at `finish_reason`. A completion cut off at the output cap is returned as though
it were complete, and the loop proceeds on a truncated tool call or a half-written summary. On a
local 35B with a modest output budget that is routine, not an edge case.

The technique: when the finish reason is `length` (`max_tokens` on some servers), append what
came back as an assistant turn, append a short user turn telling the model to resume exactly
where it stopped, emit nothing but the continuation, and not repeat anything already written.
Then call again and concatenate. Bound the rounds — BhavAI allows six — and stop on any other
finish reason.

Take the flaw out while implementing it. BhavAI concatenates blindly and parses afterwards, so a
join that does not stitch cleanly corrupts the whole payload with nothing to detect it. Validate
the joined result before accepting it, and keep the ability to fall back to the first fragment.

`finish_reason` arrives on the choice rather than the delta, and llama.cpp sends it on the final
content frame, so it has to be captured inside the streaming loop rather than after it.

**Selftest:** a mock server returning a `length` finish on the first response and completing on
the second, asserting the fragments are joined and the tool call parses.

## 3. Keep the tail when truncating tool output

**The gap is real.** [familiar.py:270](../../familiar.py#L270) truncates head-only —
`result[:MAX_TOOL_OUTPUT]`, with `MAX_TOOL_OUTPUT = 8000`. For the outputs that matter most, a
failing test run or a stack trace or a long build, the verdict is at the end and the invocation
noise is at the start. Head-only truncation therefore discards the answer and keeps the preamble.

Split the budget: keep the first half and the last half with an explicit
`... [N lines omitted] ...` marker between them, so the model knows the gap is there instead of
inferring a contradiction. BhavAI does this by lines; by characters is fine and fits the existing
constant.

A handful of lines, and probably the best effort-to-benefit ratio of anything here.

**Selftest:** an over-long tool result whose first and last lines both survive, and whose marker
is present.

## 4. Validate an edit before writing it

BhavAI locates Python edits by walking the AST to the target node and using its `lineno` and
`end_lineno`, rather than by text match, so a docstring mentioning the function name cannot
misdirect the edit. Two properties are worth having whether or not we ever add AST location:

- **Parse the replacement before touching the file.** If the new source does not compile, fail
  and write nothing.
- **A miss is a no-op.** If the target is not found, change nothing and say so, rather than
  writing a partial result.

Our `edit_file` and `multi_edit` are string replacement, which is the right default and should
stay — it works on every file type, and AST location only ever works on Python. The transferable
part is the discipline: for a `.py` target, an `ast.parse` of the post-edit content before the
write turns a class of silent corruptions into a clean refusal, at the cost of one stdlib call.

Treat AST-based location as a separate and later question. BhavAI's own `find_references` is a
warning about doing it carelessly — it matches `ast.Name` and `ast.Attribute` by bare name with
no scope resolution, so it reports every unrelated attribute of the same name and is close to
useless on a real tree.

**Selftest:** an edit producing invalid Python is refused and leaves the file byte-identical.

## 5. Stage edits so `git diff HEAD` is always the agent's changeset

[README.md](../../README.md) tells the operator to run Familiar on a clean working tree so every
edit is reviewable with `git diff`. That is the only real recovery story we offer, and it depends
on the human remembering. BhavAI makes it mechanical: after each successful write it runs
`git add` on that file, best-effort and silent. Staged is then the agent's work, unstaged is
yours, and `git diff HEAD` is exactly what the agent did across the session.

This one needs a decision rather than an implementation. [SECURITY.md](../../SECURITY.md) and the
git guard restrict git to a read-only allowlist, so `git add` is a deliberate carve-out in the
one control we actually enforce. The carve-out has to be narrow: our own call on a specific path,
never reachable through the model's `bash`. Worth doing only if that boundary stays clean.

**Do not take the auto-init above it.** BhavAI runs `git init` plus `git add .` in whatever
directory it starts in, before its generated `.gitignore` can be trusted, which will sweep
secrets and build output into a first commit. Familiar should require an existing repository and
say so, not create one.

**Selftest:** would need a scratch repository. If that is too heavy, skip the item rather than
ship it untested.

---

## What was rejected, and why it is worth recording

**BhavAI's safety model is theatre, and it supports our position.** Its headline features are a
"Zero-Deletion Policy" and a "Sandboxed Scope". In practice `BLOCKED_COMMANDS` is nine substrings
matched by regex against a string that is then handed to `subprocess.Popen(shell=True)`.
`find . -delete`, `truncate -s 0`, `git clean -xdf`, `dd`, and a bare `> file` all pass. The
literal `format` is blocked, so it also rejects harmless commands. It is wrong in both directions
at once, and it sits beside a genuine path sandbox on the file tools, which makes the README's
safety claim look supported when it is not.

Familiar's SECURITY.md already says the true thing: the git allowlist and plan mode hold, and
neither constrains `bash`. Keep saying it. A denylist here would buy nothing and would cost the
credibility of the two limits that are real.

**Not taken:** its `.bhavai/skills` manifest, since `FAMILIAR.md` is the same idea at our scale;
its chunked-write tool, an artefact of a 4k output cap that item 2 answers better; and its
FastAPI settings backend, which is dead code upstream — `fastapi` is not a declared dependency
there.

## Suggested order

Items 3 and 1 first: both are small, both are verified gaps, and neither needs a design decision.
Item 2 next, as it needs care in the streaming loop. Item 4 after that. Item 5 only if the git
carve-out can be kept narrow.
