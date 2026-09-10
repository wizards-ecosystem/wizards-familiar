# Assessments

Third-party projects read against Familiar, with what came out of each. One file per
subject. Nothing here has been implemented: an assessment records a decision or a set of
proposals, and the code changes when someone picks one up.

Each file states the upstream, the licence, the exact revision assessed, and the date. A
claim about `familiar.py` in one of them was true at that date and against that revision.
Re-check before acting on a line number.

| Subject | Date | Outcome | Status |
| --- | --- | --- | --- |
| [BhavAI Terminal Edition](bhavai.md) | 2026-09-06 | Five changes to write from scratch, plus a rejected denylist recorded so it is not re-proposed | Reviewed, nothing implemented |
| [ripwire](ripwire.md) | 2026-09-06 | Recommended for Familiar, gated on one owner decision | Assessment only, question open |

## What each one is waiting on

**BhavAI** is a list of five proposals with a suggested order: items 3 and 1 first, then
2, then 4, and 5 only if the git carve-out stays narrow. Its licence forbids reuse of its
code, so every item is a technique described for writing from scratch, never a copy. Each
item names the `selftest()` case it needs, because [AGENTS.md](../../AGENTS.md) makes a
behaviour change without one incomplete.

**ripwire** is blocked on the owner question in its section 7: whether an optional
external binary crosses Familiar's zero-dependency line. A "no" closes the file and it
becomes a declined entry. Nothing is installed and nothing is decided. The four-target
assessment this is the Familiar half of lives in the `## ripwire` section of the
exploration register in the Conclave checkout, at
`services/wizards-conclave/exploration/README.md`. That tree is gitignored, so the path
resolves only on a machine that has it.
