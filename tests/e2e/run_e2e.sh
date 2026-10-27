#!/usr/bin/env bash
# Build the add-on image locally, start GreenMail, and run the end-to-end suite.
# The image is tagged local/email_mcp:test and is not pushed anywhere.
set -euo pipefail
cd "$(dirname "$0")/../.."

if [[ -n "${DOCKER:-}" ]]; then
  # shellcheck disable=SC2206
  D=(${DOCKER})
elif docker info >/dev/null 2>&1; then
  D=(docker)
elif sudo docker info >/dev/null 2>&1; then
  D=(sudo docker)
else
  echo "Docker is not available; the end-to-end tests need a local Docker engine." >&2
  exit 1
fi

export DOCKER="${D[*]}"

"${D[@]}" build -q -t local/email_mcp:test email_mcp
"${D[@]}" rm -f greenmail >/dev/null 2>&1 || true
"${D[@]}" run -d --name greenmail \
  -p 127.0.0.1:3025:3025 \
  -p 127.0.0.1:3143:3143 \
  -p 127.0.0.1:19560:9560 \
  -e GREENMAIL_OPTS='-Dgreenmail.setup.test.smtp -Dgreenmail.setup.test.imap -Dgreenmail.hostname=0.0.0.0 -Dgreenmail.users=alice:fake-app-pw-alice@test.local,bob:fake-app-pw-bob@test.local -Dgreenmail.users.login=email' \
  greenmail/standalone:2.1.3 >/dev/null
sleep 8

if [[ -n "${PYTHON:-}" ]]; then
  PY=("${PYTHON}")
elif [[ -x .venv-srv/bin/python ]]; then
  PY=(.venv-srv/bin/python)
else
  PY=(python3)
fi

"${PY[@]}" -m pytest -q tests/e2e "$@"
