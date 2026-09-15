#!/usr/bin/env bash
# Install the orchestrate skill by symlinking it into your Claude Code skills dir.
# Use this if you'd rather not go through the plugin system. Re-running is safe.
#
#   ./install.sh           install (or refresh) the symlink
#   ./install.sh --check   report environment readiness only
#   ./install.sh --remove  remove the symlink

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFG="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
SRC="$REPO/skills/orchestrate"
DEST="$CFG/skills/orchestrate"
BINDIR="${HERDR_ORCH_BIN:-$HOME/.local/bin}"
BINLINK="$BINDIR/orch"

ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; }

check() {
  echo "Environment:"
  if command -v herdr >/dev/null 2>&1; then
    ok "herdr $(herdr --version 2>/dev/null | awk '{print $2}') at $(command -v herdr)"
    if herdr status >/dev/null 2>&1; then
      ok "herdr server reachable"
    else
      warn "herdr server not running — start it by launching 'herdr'"
    fi
  else
    bad "herdr not on PATH — install it first, the skill cannot work without it"
  fi

  command -v git >/dev/null 2>&1 && ok "git $(git --version | awk '{print $3}')" || bad "git missing (required for worktree isolation)"
  command -v jq  >/dev/null 2>&1 && ok "jq present" || bad "jq missing — required: the spawn path parses herdr JSON with it"

  if command -v gh >/dev/null 2>&1; then
    if gh auth status >/dev/null 2>&1; then
      # Parse the account from local auth state rather than calling the API,
      # so a flaky github.com doesn't turn a green check into a wall of JSON.
      acct="$(gh auth status 2>&1 | sed -n 's/.*account \([^ ][^ ]*\).*/\1/p' | head -1)"
      ok "gh authenticated${acct:+ ($acct)}"
    else
      warn "gh present but not authenticated — run 'gh auth login' for GitHub intake"
    fi
  else
    warn "gh missing — GitHub issue intake will fall back to pasted text"
  fi

  command -v glab >/dev/null 2>&1 && ok "glab present (GitLab intake)" || true

  echo "Config:"
  ok "skills dir: $CFG/skills"
  if command -v orch >/dev/null 2>&1; then
    ok "orch on PATH at $(command -v orch)"
  elif [ -x "$BINLINK" ]; then
    warn "orch installed at $BINLINK but that dir is not on your PATH — add it to your shell profile"
  else
    warn "orch not installed yet — run ./install.sh"
  fi
  [ -n "${HERDR_ORCHESTRATOR_HOME:-}" ] \
    && ok "board root (override): $HERDR_ORCHESTRATOR_HOME" \
    || ok "board root (default): $HOME/.claude/orchestrator"

  if [ "${HERDR_ENV:-}" = 1 ]; then
    ok "running inside a Herdr pane"
  else
    warn "not inside a Herdr pane — the skill refuses to run until Claude Code is launched from one"
  fi
}

case "${1:-install}" in
  --check|-c)
    check
    ;;
  --remove)
    [ -L "$BINLINK" ] && { rm "$BINLINK"; ok "removed $BINLINK"; }
    if [ -L "$DEST" ]; then
      rm "$DEST"; ok "removed $DEST"
    elif [ -e "$DEST" ]; then
      bad "$DEST is not a symlink — leaving it alone, remove it by hand if you meant to"; exit 1
    else
      ok "nothing installed at $DEST"
    fi
    ;;
  install)
    [ -f "$SRC/SKILL.md" ] || { bad "no SKILL.md at $SRC — run this from inside the repo"; exit 1; }
    mkdir -p "$CFG/skills"

    if [ -L "$DEST" ]; then
      rm "$DEST"
    elif [ -e "$DEST" ]; then
      bad "$DEST already exists and is not a symlink."
      echo "     Move or delete it first — refusing to overwrite something you may have written by hand."
      exit 1
    fi

    ln -s "$SRC" "$DEST"
    ok "linked $DEST -> $SRC"

    mkdir -p "$BINDIR"
    if [ -L "$BINLINK" ] || [ ! -e "$BINLINK" ]; then
      rm -f "$BINLINK"; ln -s "$REPO/bin/orch" "$BINLINK"
      ok "linked $BINLINK -> $REPO/bin/orch"
    else
      warn "$BINLINK exists and is not a symlink — leaving it; put $REPO/bin on PATH yourself"
    fi
    echo
    check
    echo
    echo "Next:"
    echo "  1. ensure $BINDIR is on your PATH"
    echo "  2. start a Herdr pane and launch Claude Code in it"
    echo "  3. optional board UI:  python3 $REPO/webapp/server.py"
    ;;
  *)
    echo "usage: ./install.sh [install|--check|--remove]" >&2
    exit 2
    ;;
esac
