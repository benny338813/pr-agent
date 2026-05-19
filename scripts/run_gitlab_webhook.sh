#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

export PYENV_VERSION="${PYENV_VERSION:-pr-agent-env}"
export PYTHONPATH="${PYTHONPATH:-.}"
export PORT="${PORT:-3000}"

if [[ "${RUN_SMOKE:-0}" == "1" ]]; then
  pyenv exec python scripts/smoke_gitlab_jira_ainexus.py
fi

exec pyenv exec python -m pr_agent.servers.gitlab_webhook
