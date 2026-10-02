#!/usr/bin/env bash
# Lance CyColabConnect (interface web + serveur MCP) sur http://127.0.0.1:8420
set -u
cd "$(dirname "$0")"
if [ ! -f frontend/dist/index.html ]; then
  echo "Construction de l'interface..."
  (cd frontend && npm install && npm run build) || exit 1
fi
args=("$@")
while true; do
  uv run --directory backend colabconnect serve "${args[@]}"
  code=$?
  # 3 = redémarrage demandé après une mise à jour
  [ "$code" -eq 3 ] || exit "$code"
  args=("$@" --no-browser)
done
