#!/usr/bin/env bash
# Start MongoDB and run the photo picker menu inside Docker.
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  echo "Missing .env — copy .env.example and add your NVIDIA_API_KEY"
  exit 1
fi

# Docker socket is root:docker (mode 660). User must be in group 'docker'.
if ! docker info &>/dev/null; then
  if [[ -S /var/run/docker.sock ]] && ! groups | grep -q '\bdocker\b'; then
    echo "Docker permission denied: your user is not in the 'docker' group."
    echo ""
    echo "Fix (one-time), then log out and back in (or run: newgrp docker):"
    echo "  sudo usermod -aG docker \"\$USER\""
    echo "  newgrp docker"
    echo ""
    echo "Or run this script with sudo (not recommended long-term):"
    echo "  sudo ./docker-run.sh"
    exit 1
  fi
  echo "Docker is not available. Is the Docker daemon running?"
  echo "  sudo systemctl start docker"
  exit 1
fi

docker compose build app
docker compose up -d mongo

DB_NAME="${MONGO_DB:-uniroom-data}"
echo ""
echo "MongoDB is up."
echo "  Compass: mongodb://localhost:27017"
echo "  Database: ${DB_NAME}"
echo "  Collections: housing_listings, reviewed_files"
echo ""
echo "Opening photo menu (logs will appear below)..."
echo ""

docker compose run --rm -it app python select_photo.py "$@"
