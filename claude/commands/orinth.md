---
description: Delegate a scoped coding task to the local Orinth model (free, offline)
argument-hint: <a scoped coding task with a clear way to verify it>
---

Delegate the following coding task to the `orinth` subagent. Do not implement it yourself —
invoke the subagent so the local Orinth model does the actual work, then relay its reviewed
result (summary + acceptance-check result + diff).

If the task is too fuzzy or too broad to hand off (no clear scope or no runnable "done when"
check), say so and either sharpen it into a delegable brief or do it yourself instead.

Task: $ARGUMENTS
