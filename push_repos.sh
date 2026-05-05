#!/usr/bin/env bash
# push_repos.sh — single entry point to publish Discord-COO bot updates to every
# canonical GitHub destination, so we never have to dig up the right repo / token /
# command pair again.
#
# Targets (in priority order):
#   1. adrien422/discord-coo-bot         (PRIMARY — bot-only repo, public)
#        ↳ contains: discord_coo_bot.py + helpers + AGENTS.md / DISCORD_ENVIRONMENT.md.
#        ↳ both Adrien and Dan are collaborators.
#        ↳ push every time the bot code or workspace docs change.
#
#   2. adrien422/claude-vps-template     (secondary — full sanitised vps-skill bundle)
#        ↳ contains: the entire ~/workbench/vps-skill/ tree (sanitised).
#        ↳ Dan was added as a collaborator on $(date +%Y-%m-%d).
#        ↳ push when changes go beyond the bot — global rules, scripts, other skills.
#
#   3. arman-kb24/claude-vps-template    (mirror — same as #2, on Arman's account)
#        ↳ kept in sync because gh CLI is logged in as arman-kb24 by default.
#
# Tokens come from ~/workbench/skills/github-api/references/credentials.md. The script
# never echoes them.
#
# Usage:
#   ./push_repos.sh                     # push to all three targets
#   ./push_repos.sh --bot               # push only the bot repo (#1)
#   ./push_repos.sh --template          # push only the template repos (#2 and #3)
#   ./push_repos.sh --dry-run           # do everything except the final git push
#   ./push_repos.sh --message "fix x"   # custom commit message for the bot repo
#
# Pre-reqs:
#   - The three tokens listed below are valid.
#   - ~/workbench/scripts/push_vps_state.sh exists (used for #2 and #3 staging).
#   - python3 + git available.
#
# Exit codes:
#   0  every requested target succeeded
#   1  arg parsing or unexpected error
#   2  selftest failed before any push
#   3  one or more git pushes failed
set -euo pipefail

CREDS_FILE="${CREDS_FILE:-/home/arman/workbench/skills/github-api/references/credentials.md}"

extract_token() {
  local needle="$1"
  awk -v needle="$needle" '
    $0 ~ needle { found = 1; next }
    found && /^ghp_/ { print; exit }
  ' "$CREDS_FILE"
}

ADRIEN_TOKEN="${ADRIEN_TOKEN:-$(extract_token "Adrien")}"
ARMAN_TOKEN="${ARMAN_TOKEN:-$(extract_token "Arman")}"
DAN_TOKEN="${DAN_TOKEN:-$(extract_token "Dan")}"

if [ -z "${ADRIEN_TOKEN:-}" ]; then echo "missing Adrien GitHub token (looked in $CREDS_FILE)" >&2; exit 1; fi
if [ -z "${ARMAN_TOKEN:-}" ]; then echo "missing Arman GitHub token (looked in $CREDS_FILE)" >&2; exit 1; fi
# Dan's token only needed for collaborator-invite auto-accept; not strictly required for push.

DO_BOT=1
DO_TEMPLATE=1
DRY_RUN=0
COMMIT_MSG=""

while [ $# -gt 0 ]; do
  case "$1" in
    --bot) DO_TEMPLATE=0; shift;;
    --template) DO_BOT=0; shift;;
    --dry-run) DRY_RUN=1; shift;;
    --message) COMMIT_MSG="$2"; shift 2;;
    -h|--help) sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 1;;
  esac
done

DISCORD_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKSPACE_DIR="/home/arman/workbench/discord-coo-workspace"

# 0. Self-test before pushing anything anywhere. Refuse to push if it fails.
echo "[push_repos] running stage-1 selftest…"
python3 "$DISCORD_DIR/discord_coo_selftest_stage1.py" >/dev/null
echo "[push_repos] selftest OK"

push_failures=0

# 1. PRIMARY — adrien422/discord-coo-bot
if [ "$DO_BOT" = "1" ]; then
  STAGE="/tmp/discord-coo-bot-push"
  rm -rf "$STAGE"
  echo "[push_repos] cloning adrien422/discord-coo-bot…"
  git clone --depth 1 "https://adrien422:${ADRIEN_TOKEN}@github.com/adrien422/discord-coo-bot.git" "$STAGE" >/dev/null 2>&1

  cp "$DISCORD_DIR"/discord_coo_bot.py \
     "$DISCORD_DIR"/propose_doc_change.py \
     "$DISCORD_DIR"/discord_coo_selftest_stage1.py \
     "$DISCORD_DIR"/run_discord_coo.sh \
     "$DISCORD_DIR"/discord-coo.service \
     "$DISCORD_DIR"/bootstrap_discord_env.py \
     "$DISCORD_DIR"/register_cockpit_commands.py \
     "$DISCORD_DIR"/discord_coo_looptest.py \
     "$DISCORD_DIR"/discord_coo_selfcheck.py \
     "$DISCORD_DIR"/watch_discord_coo.sh \
     "$DISCORD_DIR"/push_repos.sh \
     "$STAGE/"

  cp "$WORKSPACE_DIR"/AGENTS.md "$STAGE/AGENTS.md"
  cp "$WORKSPACE_DIR"/DISCORD_ENVIRONMENT.md "$STAGE/DISCORD_ENVIRONMENT.md"

  cd "$STAGE"
  git add -A
  if git diff --cached --quiet; then
    echo "[push_repos] discord-coo-bot: no changes to commit"
  else
    msg="${COMMIT_MSG:-update Discord COO bot ($(date -u +%Y-%m-%dT%H:%MZ))}"
    git -c user.email=adrien@projectbyall.com -c user.name=adrien422 commit -m "$msg" >/dev/null
    if [ "$DRY_RUN" = "1" ]; then
      echo "[push_repos] dry-run: would push to adrien422/discord-coo-bot"
    else
      if git push origin main; then
        echo "[push_repos] adrien422/discord-coo-bot: pushed"
      else
        echo "[push_repos] adrien422/discord-coo-bot: PUSH FAILED" >&2
        push_failures=$((push_failures + 1))
      fi
    fi
  fi
  cd "$DISCORD_DIR"
fi

# 2 + 3. Template snapshots (Adrien public + Arman mirror).
if [ "$DO_TEMPLATE" = "1" ]; then
  if [ ! -x /home/arman/workbench/scripts/push_vps_state.sh ]; then
    echo "[push_repos] missing push_vps_state.sh; skipping template repos" >&2
    push_failures=$((push_failures + 1))
  else
    if [ "$DRY_RUN" = "1" ]; then
      echo "[push_repos] dry-run: would run push_vps_state.sh --public for both template mirrors"
    else
      # Stage the sanitised snapshot once (push_vps_state.sh's repo-creation step
      # may fail on adrien422 because gh is logged in as arman-kb24, but it still
      # leaves a clean /tmp/vps-public-stage tree we can push from over HTTPS).
      /home/arman/workbench/scripts/push_vps_state.sh --public --repo arman-kb24/claude-vps-template --force >/tmp/push_vps_state.log 2>&1 || true
      tail -5 /tmp/push_vps_state.log

      if [ ! -d /tmp/vps-public-stage/.git ]; then
        echo "[push_repos] /tmp/vps-public-stage missing or not a git repo; cannot push templates" >&2
        push_failures=$((push_failures + 1))
      else
        push_template() {
          local owner="$1" token="$2"
          cd /tmp/vps-public-stage
          git remote remove origin 2>/dev/null || true
          git remote add origin "https://${owner}:${token}@github.com/${owner}/claude-vps-template.git"
          git fetch origin main >/dev/null 2>&1 || true
          if git push -u origin main --force 2>&1 | tail -3; then
            echo "[push_repos] ${owner}/claude-vps-template: pushed"
            return 0
          fi
          echo "[push_repos] ${owner}/claude-vps-template: PUSH FAILED" >&2
          return 1
        }

        echo "[push_repos] pushing adrien422/claude-vps-template…"
        push_template adrien422 "$ADRIEN_TOKEN" || push_failures=$((push_failures + 1))

        echo "[push_repos] pushing arman-kb24/claude-vps-template…"
        push_template arman-kb24 "$ARMAN_TOKEN" || push_failures=$((push_failures + 1))

        cd "$DISCORD_DIR"
      fi
    fi
  fi
fi

if [ "$push_failures" -gt 0 ]; then
  echo "[push_repos] $push_failures target(s) failed" >&2
  exit 3
fi
echo "[push_repos] done"
