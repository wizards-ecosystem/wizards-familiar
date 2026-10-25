# Security

The Wizard's Familiar runs model-chosen commands as you. Normal has no sandbox or
approval prompt: commands can read/write your files and reach the network. Repository
content can influence the model. Use trusted code and a reviewable git working tree.

Plan exposes and dispatches only native read/list/search, rejecting shell and mutation
calls even when fabricated. This is a tool boundary, not OS isolation against hostile
filesystems. Normal's textual git allowlist prevents common accidental commits/pushes;
quoting or indirection can bypass it. It is not an adversarial boundary.

Model HTTP accepts loopback endpoints only, disables inherited proxies and rejects
remote redirects. Familiar makes no telemetry, cloud inference or model-download
calls. The local server can still log or proxy data; Normal shell networking remains
unrestricted. Compatible servers must honor bounded generation and terminal markers.
Incomplete streams authorize no tools; interrupted groups retain accurate receipts.

Source reads and search are bounded and report truncation. Edits require regular,
writable UTF-8 files up to 8 MiB, preserve BOM/newlines, reject mixed endings/symlinks,
and compare content fingerprints before replacement. Same-directory atomic publication
protects original bytes against pre-commit failure. It does not promise power-loss
durability, every ACL/xattr, adversarial race protection or containment of detached
command descendants. New-file publication requires filesystem hard-link support.

Conversation and compaction state stay in memory. User-input history is separate and
enabled by default; it can contain private text. `FAMILIAR_HISTORY=0` disables history
reads/writes. `/new` and exit release references, without secure RAM-erasure guarantees
or control over swap, crash dumps, terminal scrollback or server logging.

Report violations of these documented contracts through this repository's private
vulnerability reporting (Security, Report a vulnerability). Include the version,
Python/server/model details and a minimal reproduction without credentials. Intended
unsandboxed command behavior is outside that contract. This policy overrides the
organization default.

The optional macOS launcher runs a separately provisioned Ornith-1.0-35B model. Its
weights, license and behavior belong to the upstream publisher. Familiar does not
verify model downloads.
