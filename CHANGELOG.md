# Changelog

## 0.1.0

First release under The Wizard's Ecosystem. The agent itself is unchanged from the
work that preceded this release; what changed is the name, the packaging, and the
documentation.

### Renamed

This project was previously `orinth-sidekick`, published from a personal account. The
old name was a misspelling of Ornith, the third-party model it launches, which is one
of the reasons it went. It is now **The Wizard's Familiar**, repository
`wizards-ecosystem/wizards-familiar`. GitHub redirects the old URL.

Nothing is kept for backward compatibility. If you used the previous version:

| Was | Is |
| --- | --- |
| `sidekick.py` | `familiar.py` |
| `sidekick <dir>` | `familiar <dir>` |
| `SIDEKICK.md` in your repo | `FAMILIAR.md` |
| `~/.sidekick_history` | `~/.familiar_history` |
| `SIDEKICK_URL`, `SIDEKICK_MODEL`, `SIDEKICK_MODEL_PATH`, `SIDEKICK_CTX_TOKENS`, `SIDEKICK_BASH_TIMEOUT`, `SIDEKICK_HISTFILE` | the same names with a `FAMILIAR_` prefix |

Rename your `SIDEKICK.md` and re-export any environment variables you had set. The old
history file is not read; move it if you want it back.

### Added

- `pip install` support. The quickstart previously said to run `sidekick`, but nothing
  in the repository put that command on `PATH`. Installing now provides `familiar` and
  `familiar-serve`, and the agent still has no runtime dependencies.
- `familiar --version`.
- Continuous integration on macOS, running both existing test suites, building the
  wheel, and failing if a runtime dependency is ever added.
- `SECURITY.md`, stating the trust model. The agent runs model-chosen shell commands
  unsandboxed, which until now was disclosed only in a source comment.
- This changelog.

### Fixed

- `LICENSE` had an empty copyright line. It now reads `Copyright (c) 2026 Isaac Limb`.
