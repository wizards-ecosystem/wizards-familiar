# Security

This file overrides the [organization default](https://github.com/wizards-ecosystem/.github/blob/main/SECURITY.md)
for The Wizard's Familiar.

## The trust model, stated plainly

Familiar is an agent that runs shell commands chosen by a language model, on your
machine, as you, with no sandbox. That is what it is for. It is not a hardened
boundary and it is not trying to be one.

Concretely:

- `bash` runs whatever the model asks for, with your user's full permissions. It can
  read any file you can read, write any file you can write, and reach the network.
- The model's input includes the contents of files it reads. A repository you did not
  write can therefore influence what the agent tries to do next. Treat running
  Familiar on untrusted code the same way you would treat running that code.
- There is no approval prompt before a command runs. Commands are printed as they
  execute, not before.

Two limits do hold, and they are tested:

| Limit | Where |
| --- | --- |
| git is restricted to a read-only allowlist, so the agent cannot commit, push, checkout, merge, or delete a branch | `GIT_RO` and `git_guard()` in `familiar.py` |
| `/plan` mode removes the write, edit and multi-edit tools from the tool list and also refuses them at execution | `active_tools()` and `run_tool()` |

Both are covered by `./familiar.py --selftest`. The git guard handles `-C`, chained
commands, and the mutating forms of `git branch`.

Neither limit constrains `bash`, and the git guard is a guardrail rather than a
boundary. It matches `git <subcommand>` textually, so quoting or indirection slips
past it. Both of these are allowed through today, verified against `git_guard()`:

```sh
git 'commit' -m x      # quoted subcommand does not match
g=git; $g commit -m x  # the name is not literally "git" at match time
```

Chained commands, `-C`, `xargs git ...`, `sh -c '...'`, and an absolute path to the
binary are all caught. The guard exists to stop a weak model from casually running
`git commit`, which is the failure it was written for. It is not a defense against a
model or a repository trying to get around it, and nothing stops `rm` regardless. If
you need a real boundary, run Familiar in a container or a VM.

## Reduce the blast radius

- Run it against a git working tree with your work committed, so any edit is reviewable
  with `git diff` and reversible.
- Prefer `/plan` when pointing it at code you do not know.
- Do not run it as root.

## What is in scope for a report

A defect where Familiar does something the documented model says it cannot: the git
guard letting a mutating git command through, plan mode applying an edit, the re-read
guard or the trim logic corrupting a file, or a crash that leaves a partially written
file behind. `tool_multi_edit` is meant to be atomic; a partial write from it is a bug.

Out of scope: the fact that `bash` is unsandboxed, and anything downstream of that. It
is the documented design.

## Reporting

Use GitHub's private vulnerability reporting on this repository (Security, then Report
a vulnerability). Do not open a public issue.

Include the commit, your Python version, the model and `llama-server` build, and the
smallest reproduction you can get to. Remove credentials and private source first.

This project has no supported-version policy, disclosure timeline, or response-time
commitment. A report will be read and handled.

## The model is not ours

Familiar launches Ornith-1.0-35B from Hugging Face. That model's weights, licence, and
behavior are the upstream publisher's, not this project's. Familiar does not verify the
download beyond what `curl` reports.
