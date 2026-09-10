# ripwire: an optional retrieval tool

Status: **assessment only. Nothing installed, nothing changed, nothing decided.** This file
is the whole output. Every claim about `familiar.py` below was checked by opening the file
at the time of writing; every claim about ripwire cites a path at the pin in section 1.

Unlike [the BhavAI assessment](bhavai.md), **this is not a list of techniques to
reimplement**. Nothing here is copied or adapted. ripwire is an Apache-2.0 external binary,
and the only question is whether Familiar should invoke one it does not ship. That question
runs straight into the first row of the constraints table in [AGENTS.md](../../AGENTS.md), so
section 3 is the whole decision and sections 4-6 only matter if section 3 goes the right way.

## 1. Where this came from

| | |
| --- | --- |
| Upstream | https://github.com/redhat-et/ripwire |
| License | Apache-2.0, Red Hat Emerging Technologies |
| Assessed at | `6488f6f4` (`v0.4.0-1-g6488f6f4`, 2026-09-06), 1,817 commits |
| Size | 152,787 non-test lines of C++23 in `src/`; 190 MB of vendored `third_party` (21 tree-sitter grammars) |
| Clone at | `wizards-ecosystem/services/wizards-conclave/exploration/ripwire/` (gitignored, not a dependency) |

ripwire is a retrieval tool for coding agents. Pointed at a repository with a stated task
it emits one ranked, deterministic bundle — the relevant symbols with signatures and doc
comments, their callers, complexity, git churn, change amplification, and the tests that
reach them — instead of the agent grepping and reading whole files to find the same thing.
`ripwire . --for="the task"` is the entire interface. No API key, no embeddings, no index
server, no daemon, no network at runtime.

It was assessed against Conclave, Pick, Familiar and Bella. **Familiar is the best fit of
the four**, and the reasons are specific to this codebase rather than general enthusiasm.

## 2. Why it is a good fit here specifically

**The platform matches exactly.** ripwire's flagship target is macOS arm64; per AGENTS.md
Familiar's only supported platform is macOS on Apple Silicon. There is no porting question.

**The problem it attacks is the one Familiar already spends code fighting.**
[familiar.py:24](../../familiar.py#L24) sets `CTX_CHARS = 55000 * 3`, sized to the server's real
window at startup by `resolve_ctx_budget()` ([familiar.py:338](../../familiar.py#L338)). When
that budget fills, [familiar.py:368](../../familiar.py#L368) **drops the oldest messages** and
prints `context full — dropped N old message(s)`. Familiar does not summarise or compact; it
forgets. Every token spent on an exploratory grep-and-read pass is bought by discarding
earlier turns of the same task.

The codebase is already explicit that this is the failure mode. The comment at
[familiar.py:224-226](../../familiar.py#L224-L226) reads *"a weak model reaches for cat and
re-dumps the same file into context"*, and `bare_cat_paths()` exists for no other reason than
to route a bare `cat` back through `read_file` so `_READ_SEEN` can dedupe it. The system
prompt spends a bullet on the same thing: *"You already have what you've read — don't re-read
a file that hasn't changed"* ([familiar.py:49](../../familiar.py#L49)).

AGENTS.md's "Working on the loop-breakers" section names the re-read guard, the
consecutive-duplicate breaker, the bare-`cat` routing and the rest, and says plainly: *"They
look like workarounds because they are."* Several of them are workarounds for **one** root
cause — a 35B model is bad at deciding what to read, so it reads too much and repeats itself.
ripwire is the only thing assessed so far that addresses that cause rather than containing its
symptoms.

**Nothing here proposes removing a loop-breaker.** Per AGENTS.md they each have a selftest
case pinning the behaviour they were written for, and they stay. This would sit upstream of
them.

## 3. The constraint question, which is the whole decision

AGENTS.md row one: *"Zero runtime dependencies — a tool that edits your files should not
arrive with a dependency tree to audit. CI enforces it."*

Be precise about what that does and does not rule out, because the two readings give
different answers.

**The letter is not violated.** The enforced check is on `pyproject.toml`'s dependency list
staying empty, and ripwire is not a Python package. Familiar already invokes external binaries
it does not declare and could not function without: `git` throughout `git_guard`
([familiar.py:218](../../familiar.py#L218)), and the system prompt tells the model to use `bash`
for *"grep, find, git, running code and tests"* ([familiar.py:53-54](../../familiar.py#L53-L54)).
Those are all undeclared host binaries. CI would stay green.

**The spirit is engaged, and honestly so.** `grep`, `find` and `git` are on every developer
Mac already; ripwire is a thing the user must go and install, and its repository carries
190 MB of vendored grammars. "Nothing to audit" is a weaker claim the day the README says to
install a second binary. That is a real cost against the identity, not a technicality to
argue past.

**The resolution that keeps both:** make it strictly optional and never required. Familiar
behaves exactly as it does today when ripwire is absent — no error, no warning, no degraded
path, no mention in the install instructions as a step. A user who has it gets a better
agent; a user who does not has lost nothing and installed nothing. Under that framing the
zero-dependency claim survives intact in both readings, because there is still no dependency:
there is an optional capability.

**This is a product decision and it is the owner's.** It is the kind of trade AGENTS.md says
to name out loud rather than slip in: *"A change that trades one of these away for a feature
is the wrong trade. Say so in the pull request if you think an exception is warranted."*
Consider this the saying-so. My read is that optional-and-silent is not an exception at all,
but that is an argument, not a ruling.

## 4. What adoption would actually look like

Two seams, and they differ in cost by a lot.

**Seam A — per-repo `FAMILIAR.md`, zero code change.**
`build_system_prompt()` ([familiar.py:36-56](../../familiar.py#L36-L56)) appends the working
directory's `FAMILIAR.md` to the system prompt when one exists
([familiar.py:42-44](../../familiar.py#L42-L44)). A user with ripwire installed adds a few lines
to the `FAMILIAR.md` of a repo they work on, and the agent starts reaching for it there.

**No change to `familiar.py`, therefore no behaviour change, therefore no selftest case
required.** This costs nothing, is reversible by deleting three lines, and is the honest way
to find out whether it helps before touching the product. Note that this file is read from
the *working directory* — it is the target repository's notes, not Familiar's own. Nothing
ships.

**Seam B — teach it in the base prompt, which is a real change.**
Adding ripwire to the tool bullet at [familiar.py:53-54](../../familiar.py#L53-L54) makes it part
of the product. That needs presence detection (`shutil.which`, stdlib) so the prompt does not
instruct the model to run a binary that is not there — a model told to use a missing tool will
burn steps discovering that, which is the exact waste this is meant to prevent.

Per AGENTS.md, *"a behavior change without a matching case in `selftest()` is incomplete."*
Two cases: the prompt gains the ripwire guidance when the binary is present, and is
byte-identical to today's when it is absent. The second is the one that matters — it is the
pin on "optional and silent."

**Do Seam A first. Seam B only on evidence from Seam A**, and only if section 3 is settled.

## 5. What has to be measured, because upstream's number is not ours

ripwire's headline is that a bundle costs about **5%** of what a grep-and-read pass spends.
Upstream states it as re-derived 2026-08-23 against its own corpus. Treat it as a reason to
try and nothing more:

- It was taken against frontier models. Familiar drives a **local 35B at Q4**, whose retrieval
  behaviour is exactly the thing that is different, and the direction of the difference is not
  obvious. A weaker model may benefit *more* (it is worse at choosing what to read) or **less**
  (it may fail to use a structured XML bundle well, and a bundle it cannot exploit is pure
  cost). This is genuinely unknown and should not be guessed at in either direction.
- The bundle is minified XML. Whether Ornith-1.0-35B reads that as well as it reads source is
  an open question with a real chance of "no".
- ripwire parses Python, TypeScript, Go, Rust, C/C++ and 16 more, but its measurements are
  strongest on its own C++ tree. Per-language quality varies and upstream says so.

The measurement to run is not a benchmark to invent. Upstream ships
`prompts/improve-for-my-language.md`, which harvests a real session's transcript — where the
tool answered, where it missed, where the operator fell back to grep — and requires every
finding to cite the moment it came from. Run it after Seam A on a repo you actually work on.
The honest test is whether `context full — dropped N old message(s)` fires less often on the
same task, and whether the task still completes.

If the answer is no, that is a finding worth having and worth sending upstream; the project
explicitly asks for exactly this and says it is worth more to them than a bug report.

## 6. What not to do

- **Do not vendor any of it.** 152,787 lines of C++23 against a 1,021-line Python file is not
  a merge, and the single-file shape is the product. There is no "reimplement in Python from a
  reading" list here and one should not be manufactured.
- **Do not add it to `pyproject.toml`.** It is not a Python package and CI is right to fail if
  the dependency list grows.
- **Do not make it required, or mention it as an install step.** That is the whole basis on
  which section 3 resolves.
- **Do not reach for its MCP server** (`src/mcpserver.h`). Familiar has a `bash` tool and no
  MCP client; the CLI through `bash` is the cheap path and the MCP interface is upstream's
  optional second one.
- **Do not remove or weaken a loop-breaker on the strength of this**, even if it helps. They
  are pinned by selftest cases and they hold when ripwire is absent, which is the default.

## 7. Questions for the owner

1. **Section 3 — is optional-and-silent acceptable, or does any external binary cross the
   zero-dependency line?** Everything else is downstream of this. A "no" closes the file
   cleanly and it becomes a declined-register entry.
2. **If yes: Seam A only, or Seam A then possibly Seam B?** Seam A ships nothing and commits
   to nothing. Seam B puts ripwire in the product and needs the two selftest cases.
3. **Is a null result worth the session it costs?** Measuring honestly means using it on a
   real task and possibly concluding it does not help a 35B. That is a genuine possible
   outcome, not a formality.

## 8. Provenance

Assessed 2026-09-06 against ripwire at `6488f6f4`. Every `familiar.py` line number above was
read at that date against the 1,021-line file. The clone is gitignored inside
`wizards-conclave/exploration/`, and the `## ripwire` section of the README there carries the
four-target assessment that this file is the Familiar half of. Bella has its own half in
`bella-services/docs/HANDOFF_2026-09-06_RIPWIRE.md`; Pick and Conclave were declined there.

Indexed in [the assessment register](README.md), alongside the BhavAI review.
