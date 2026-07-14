#!/usr/bin/env bash
# Startup command per Azure App Service Linux (Configuration > Startup Command: `bash startup.sh`).
set -e

# I server MCP (es. analytics-mcp) sono pip-installati da requirements.txt:
# l'eseguibile si trova nel bin del virtualenv di Oryx (antenv), già attivo qui.
export PATH="$PATH:$VIRTUAL_ENV/bin:/home/.local/bin"

# DATA_DIR persistente su Azure: /home è montato e sopravvive ai riavvii.
export DATA_DIR="${DATA_DIR:-/home/data}"

# Bridge SSE con stato in memoria -> un solo worker (vedi README).
exec gunicorn app.main:app \
  --worker-class uvicorn.workers.UvicornWorker \
  --workers 1 \
  --bind 0.0.0.0:"${PORT:-8000}" \
  --timeout 600 \
  --access-logfile - \
  --error-logfile -
