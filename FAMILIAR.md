# Project notes

`familiar.py` contains the whole agent and selftest. `serve.sh` is an optional macOS
launcher.

Run `python familiar.py --selftest` on each core platform and `./test_serve.sh` on macOS.

- Standard library only. Keep the core in one file.
- Exactly Normal and Plan; Plan dispatches only native reads/search.
- Compaction is manual, in memory, and never resumes tools.
- Preserve real user text and complete tool-call/result groups.
- Behavior changes need focused regression coverage in `selftest()`.
- Keep documentation and comments minimal. `AGENTS.md` owns instructions.
- Ornith is the upstream model's spelling; do not change it.
