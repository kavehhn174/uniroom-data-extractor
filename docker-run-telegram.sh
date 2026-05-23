#!/usr/bin/env bash
# Start MongoDB and run the Telegram bot inside Docker (background + logs).
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  echo "Missing .env — copy .env.example and add NVIDIA_API_KEY and TELEGRAM_BOT_TOKEN"
  exit 1
fi

if ! docker info &>/dev/null; then
  if [[ -S /var/run/docker.sock ]] && ! groups | grep -q '\bdocker\b'; then
    echo "Docker permission denied: your user is not in the 'docker' group."
    echo ""
    echo "Fix (one-time), then log out and back in (or run: newgrp docker):"
    echo "  sudo usermod -aG docker \"\$USER\""
    echo "  newgrp docker"
    exit 1
  fi
  echo "Docker is not available. Is the Docker daemon running?"
  echo "  sudo systemctl start docker"
  exit 1
fi

# shellcheck source=/dev/null
set -a
source .env
set +a

docker compose build telegram
docker compose up -d mongo telegram

DB_NAME="${MONGO_DB:-uniroom-data}"
echo ""
echo "Telegram bot is running."
echo "  Logs:    docker compose logs -f telegram"
echo "  Stop:    docker compose stop telegram"
echo "  MongoDB: mongodb://localhost:27017"
echo "  Database: ${DB_NAME}"
echo ""
echo "Follow bot logs (Ctrl+C stops viewing logs, bot keeps running)..."
echo ""

docker compose logs -f telegram
