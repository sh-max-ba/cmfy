#!/usr/bin/env bash
#
# provision.sh — выполняется Vast.ai при первом старте инстанса
# (переменная PROVISIONING_SCRIPT = ссылка на этот файл, raw).
#
# Что делает:
#   1. Ставит aria2 (многопоточная качалка).
#   2. Скачивает наш web-установщик model_downloader.py.
#   3. Регистрирует его в supervisor → автозапуск + переживает рестарты.
#   4. (опционально) клонирует кастомные ноды и доустанавливает зависимости.
#
# ВАЖНО: замени RAW-ссылку на свою (где лежит model_downloader.py).

set -e
echo "=== provision.sh старт ==="

COMFY="${COMFYUI_PATH:-/workspace/ComfyUI}"
DOWNLOADER_PORT="${DOWNLOADER_PORT:-7000}"

# --- 1. aria2 -------------------------------------------------------------
echo "[1/4] Установка aria2..."
apt-get update -qq && apt-get install -y -qq aria2 || true

# --- 2. Скачиваем установщик ----------------------------------------------
echo "[2/4] Скачивание model_downloader.py..."
# ЗАМЕНИ ЭТУ ССЫЛКУ на свой raw-URL (GitHub / Gist):
DOWNLOADER_URL="https://raw.githubusercontent.com/ТВОЙ_ЛОГИН/ТВОЙ_РЕПО/main/model_downloader.py"

mkdir -p /opt/downloader
if curl -fsSL "$DOWNLOADER_URL" -o /opt/downloader/model_downloader.py; then
    echo "  установщик скачан"
else
    echo "  !! не удалось скачать установщик по $DOWNLOADER_URL"
    echo "  !! проверь ссылку или залей файл вручную в /opt/downloader/"
fi

# --- 3. supervisor: автозапуск установщика --------------------------------
echo "[3/4] Регистрация в supervisor..."
cat > /etc/supervisor/conf.d/model-downloader.conf <<EOF
[program:model-downloader]
command=python3 /opt/downloader/model_downloader.py
environment=DOWNLOADER_PORT="${DOWNLOADER_PORT}",COMFYUI_PATH="${COMFY}",CIVITAI_TOKEN="%(ENV_CIVITAI_TOKEN)s",HF_TOKEN="%(ENV_HF_TOKEN)s"
autostart=true
autorestart=true
stdout_logfile=/var/log/model-downloader.log
stderr_logfile=/var/log/model-downloader.log
EOF

supervisorctl reread && supervisorctl update || true

# --- 4. Кастомные ноды (по мере роста) ------------------------------------
echo "[4/4] Установка кастомных нод..."
NODES_DIR="$COMFY/custom_nodes"
mkdir -p "$NODES_DIR"
cd "$NODES_DIR"

# Добавляй сюда свои ноды по мере сборки воркфлоу ZIT Luneva.
# Пример:
#   clone_node https://github.com/автор/нода.git
clone_node() {
    local repo="$1"
    local name
    name=$(basename "$repo" .git)
    if [ ! -d "$name" ]; then
        echo "  clone $name"
        git clone --depth 1 "$repo" || echo "  !! не склонировался $repo"
        if [ -f "$name/requirements.txt" ]; then
            pip install -q -r "$name/requirements.txt" || true
        fi
    else
        echo "  $name уже есть, пропуск"
    fi
}

# ↓↓↓ сюда добавляй ноды ↓↓↓
# clone_node https://github.com/...

echo "=== provision.sh готово ==="
echo "Установщик: порт ${DOWNLOADER_PORT}"
