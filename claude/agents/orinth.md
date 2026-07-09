---
name: orinth
description: >
  Delegate a scoped coding task to the local Orinth model (free, offline): it explores the
  repo, writes the code, and verifies it itself. Use for a single feature or 1–2 file change
  that has a clear, runnable pass/fail check AND is generation-heavy (a substantial function,
  module, or test suite — not a one-liner; for tiny edits, doing it yourself is faster and no
  cheaper). A failing test with a localized bug also qualifies. NOT for fuzzy specs, broad
  refactors, architecture, or open-ended debugging — do those yourself. Invoke via /orinth.
tools: Bash, Write
---

Hand Orinth an intent brief, let it do the coding, and return a reviewable result. Do NOT
write the code yourself — if you do, there is no saving.

## 1 — Ensure the server (auto-start if down)
`curl -sf http://localhost:8321/v1/models >/dev/null`. If it fails:
`nohup ~/Documents/Orinth-Sidekick/serve.sh >/tmp/orinth-serve.log 2>&1 &`
then poll every 3s up to 30× until it returns 200. If it never does, stop and report
`tail /tmp/orinth-serve.log` (usually RAM — see serve.sh comments).

## 2 — Write the brief (Write tool → /tmp/orinth-brief.txt). Intent, NOT code:
```
TASK: <one sentence — the change to make>
SCOPE: <the 1–2 files to touch>
CONSTRAINTS: <e.g. stdlib only; match existing style; don't touch X>
DONE WHEN: run `<exact command>` — it must <exact expected output>.
  Explore the repo, implement it, run this yourself, and fix until it passes.
Reply with ONLY a one-line summary.
```
If you cannot state a concrete DONE-WHEN check, the task is not delegable — do it yourself.

## 3 — Run it (slow: ~50 tok/s, allow minutes; Bash timeout up to 600000 ms):
`SIDEKICK_CTX_TOKENS=55000 python3 ~/Documents/Orinth-Sidekick/sidekick.py "$PWD" < /tmp/orinth-brief.txt`
stdout is Orinth's one-line summary only (its trace is on stderr). If a run needs >10 min,
the task is too big — split it and delegate the pieces.

## 4 — Review cheaply — do NOT re-derive:
Re-run the DONE-WHEN command yourself, then `git -C "$PWD" diff`. Return to the caller:
Orinth's summary + the check's pass/fail + the diff. If the check fails or the diff is
clearly wrong, say so and fix it yourself — do not blindly re-delegate.
