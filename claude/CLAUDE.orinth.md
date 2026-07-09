<!-- ORINTH-DELEGATION:START (managed by Orinth-Sidekick install.sh) -->
## Orinth (local model) delegation

Delegate to the `orinth` subagent (not direct edits) when a coding task is (a) self-contained and ≤2 files, (b) clearly specified with a runnable pass/fail check (a test or command), and (c) generation-heavy — writing a substantial function, module, or test suite. A failing test with a localized bug also qualifies. It offloads the work to a free local model. Do NOT delegate one-liners or tiny edits (doing them yourself is faster and no cheaper), fuzzy specs, broad/cross-file refactors, architecture, or open-ended debugging with no repro. Briefing/review details live in the subagent; force it with `/orinth`.
<!-- ORINTH-DELEGATION:END -->
