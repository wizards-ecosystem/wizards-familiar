#!/bin/sh
# Install the Claude Code integration for Orinth Sidekick.
# Idempotent: symlinks the subagent + slash command into ~/.claude and merges the
# delegation rule into ~/.claude/CLAUDE.md. Re-run any time; safe to run twice.
# The repo stays the single source of truth — edit files under claude/ and the live
# integration updates through the symlinks. Uninstall: ./install.sh --uninstall
set -eu
REPO="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
AGENT="$CLAUDE_DIR/agents/orinth.md"
CMD="$CLAUDE_DIR/commands/orinth.md"
CLAUDE_MD="$CLAUDE_DIR/CLAUDE.md"
START="<!-- ORINTH-DELEGATION:START (managed by Orinth-Sidekick install.sh) -->"
END="<!-- ORINTH-DELEGATION:END -->"

if [ "${1:-}" = "--uninstall" ]; then
  rm -f "$AGENT" "$CMD"
  if [ -f "$CLAUDE_MD" ]; then
    # strip the managed block
    awk -v s="$START" -v e="$END" '
      $0==s{skip=1} !skip{print} $0==e{skip=0}' "$CLAUDE_MD" > "$CLAUDE_MD.tmp" && mv "$CLAUDE_MD.tmp" "$CLAUDE_MD"
  fi
  echo "orinth integration removed from $CLAUDE_DIR"
  exit 0
fi

mkdir -p "$CLAUDE_DIR/agents" "$CLAUDE_DIR/commands"
ln -sf "$REPO/claude/agents/orinth.md" "$AGENT"
ln -sf "$REPO/claude/commands/orinth.md" "$CMD"
echo "linked  $AGENT -> repo"
echo "linked  $CMD -> repo"

# merge the delegation rule into CLAUDE.md, replacing any prior managed block
touch "$CLAUDE_MD"
if grep -qF "$START" "$CLAUDE_MD"; then
  awk -v s="$START" -v e="$END" '$0==s{skip=1} !skip{print} $0==e{skip=0}' "$CLAUDE_MD" > "$CLAUDE_MD.tmp" && mv "$CLAUDE_MD.tmp" "$CLAUDE_MD"
fi
printf '\n%s\n' "$(cat "$REPO/claude/CLAUDE.orinth.md")" >> "$CLAUDE_MD"
echo "merged  delegation rule into $CLAUDE_MD"

echo
echo "Done. Start the model with ./serve.sh, then use /orinth <task> in Claude Code."
echo "(A fresh Claude Code session may be needed to pick up the new subagent.)"
