#!/usr/bin/env bash
# start.sh – constrói e inicia o MoonKobra com Docker (ainda não há imagem publicada)

set -euo pipefail

cd "$(dirname "$0")"

# .env a partir do exemplo, se ainda não existir
if [[ ! -f .env ]]; then
    if [[ -f .env.example ]]; then
        cp .env.example .env
        echo "[start] .env criado a partir de .env.example"
    else
        touch .env
    fi
fi

# config/ e o exemplo de config.ini
mkdir -p config
if [[ ! -f config/config.ini ]] && [[ ! -f config/config.ini.example ]] && [[ -f config.ini.example ]]; then
    cp config.ini.example config/config.ini.example
    echo "[start] config/config.ini.example criado"
fi

if ! docker info > /dev/null 2>&1; then
    echo "[start] Docker não encontrado – instale o Docker primeiro."
    exit 1
fi

echo "[start] Construindo e iniciando o MoonKobra ..."
docker-compose down --remove-orphans 2>/dev/null || true
docker-compose up -d --build

echo ""
echo "  ✓ MoonKobra rodando"
echo "  Web-UI : http://$(hostname -I | awk '{print $1}'):7125"
echo "  Logs   : docker-compose logs -f"
echo "  Parar  : docker-compose down"
