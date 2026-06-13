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
#   5. Качает auto-модели из избранного (GitHub) и восстанавливает воркфлоу.
#
# Переменные окружения инстанса:
#   CIVITAI_TOKEN, HF_TOKEN  - токены для приватных/гейтнутых моделей
#   GITHUB_TOKEN             - fine-grained PAT (Contents: r/w) для избранного/воркфлоу
#   GITHUB_REPO              - "owner/repo", напр. sh-max-ba/cmfy
#   GITHUB_BRANCH            - ветка (по умолчанию main)

set -e
echo "=== provision.sh старт ==="

COMFY="${COMFYUI_PATH:-/workspace/ComfyUI}"
DOWNLOADER_PORT="${DOWNLOADER_PORT:-7000}"

# --- 1. aria2 -------------------------------------------------------------
echo "[1/4] Установка aria2..."
apt-get update -qq && apt-get install -y -qq aria2 || true

# --- 2. Скачиваем установщик ----------------------------------------------
echo "[2/4] Скачивание model_downloader.py..."
DOWNLOADER_URL="https://raw.githubusercontent.com/sh-max-ba/cmfy/main/model_downloader.py"

mkdir -p /opt/downloader
if curl -fsSL "$DOWNLOADER_URL" -o /opt/downloader/model_downloader.py; then
    echo "  установщик скачан"
else
    echo "  !! не удалось скачать установщик по $DOWNLOADER_URL"
    echo "  !! проверь ссылку или залей файл вручную в /opt/downloader/"
fi

# --- 3. supervisor: автозапуск установщика --------------------------------
echo "[3/4] Регистрация в supervisor..."

# Пишем окружение в отдельный файл из ТЕКУЩИХ переменных (надёжнее, чем
# %(ENV_..)s в supervisor, который видит не все переменные инстанса).
cat > /opt/downloader/env.sh <<EOF
export DOWNLOADER_PORT="${DOWNLOADER_PORT}"
export COMFYUI_PATH="${COMFY}"
export CIVITAI_TOKEN="${CIVITAI_TOKEN:-}"
export HF_TOKEN="${HF_TOKEN:-}"
export GITHUB_TOKEN="${GITHUB_TOKEN:-}"
export GITHUB_REPO="${GITHUB_REPO:-}"
export GITHUB_BRANCH="${GITHUB_BRANCH:-main}"
EOF

# Запускаем через обёртку, которая сначала подгружает env.sh.
cat > /opt/downloader/run.sh <<'EOF'
#!/usr/bin/env bash
source /opt/downloader/env.sh
exec python3 /opt/downloader/model_downloader.py
EOF
chmod +x /opt/downloader/run.sh

cat > /etc/supervisor/conf.d/model-downloader.conf <<EOF
[program:model-downloader]
command=/opt/downloader/run.sh
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

# --- 5. Избранное и воркфлоу из GitHub ------------------------------------
echo "[5/5] Восстановление избранного и воркфлоу из GitHub..."
GITHUB_REPO="${GITHUB_REPO:-}"
GITHUB_BRANCH="${GITHUB_BRANCH:-main}"

if [ -n "$GITHUB_REPO" ]; then
    RAW="https://raw.githubusercontent.com/${GITHUB_REPO}/${GITHUB_BRANCH}"

    # 5a. Автокачка моделей с флагом auto:true из favorites.json
    echo "  Чтение favorites.json..."
    FAV=$(curl -fsSL "${RAW}/favorites.json" 2>/dev/null || echo "")
    if [ -n "$FAV" ]; then
        # парсим питоном (jq может не быть), качаем только auto:true
        echo "$FAV" | python3 - "$COMFY" "$CIVITAI_TOKEN" "$HF_TOKEN" <<'PYEOF'
import sys, json, os, subprocess, re, urllib.request
data = sys.stdin.read()
comfy, civitai_token, hf_token = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    favs = json.loads(data)
except Exception:
    favs = []
for f in favs:
    if not f.get("auto"):
        continue
    url = f.get("url", ""); folder = f.get("folder", "loras"); name = f.get("name", "")
    out = os.path.join(comfy, "models", folder)
    os.makedirs(out, exist_ok=True)
    # резолв ссылок (упрощённый, как в установщике)
    headers = []
    if "civitai.com" in url:
        if "/api/download/" not in url:
            m = re.search(r"/models/(\d+)", url)
            if m:
                try:
                    api = f"https://civitai.com/api/v1/models/{m.group(1)}"
                    req = urllib.request.Request(api, headers={"User-Agent":"prov"})
                    d = json.loads(urllib.request.urlopen(req, timeout=30).read())
                    vid = d["modelVersions"][0]["id"]
                    vd = json.loads(urllib.request.urlopen(
                        f"https://civitai.com/api/v1/model-versions/{vid}", timeout=30).read())
                    files = vd["files"]; pr = next((x for x in files if x.get("primary")), files[0])
                    url = pr["downloadUrl"]; name = name or pr.get("name")
                except Exception as e:
                    print(f"  !! civitai resolve fail: {e}"); continue
        if civitai_token:
            url += ("&" if "?" in url else "?") + f"token={civitai_token}"
    elif "huggingface.co" in url:
        url = url.replace("/blob/", "/resolve/")
        if hf_token:
            headers += ["--header", f"Authorization: Bearer {hf_token}"]
    elif "github.com" in url and "/blob/" in url:
        url = url.replace("github.com","raw.githubusercontent.com").replace("/blob/","/")
    cmd = ["aria2c","-x","16","-s","16","-k","1M","--allow-overwrite=true",
           "--auto-file-renaming=false","-d",out] + headers
    if name: cmd += ["-o", name]
    cmd.append(url)
    print(f"  auto-download → {folder}/{name or '(имя с сервера)'}")
    subprocess.run(cmd)
PYEOF
    else
        echo "  favorites.json не найден или пуст"
    fi

    # 5b. Восстановление воркфлоу
    echo "  Восстановление воркфлоу..."
    WF_DIR="$COMFY/user/default/workflows"
    mkdir -p "$WF_DIR"
    # список воркфлоу из репо через GitHub API (если задан токен — с авторизацией)
    API="https://api.github.com/repos/${GITHUB_REPO}/contents/workflows?ref=${GITHUB_BRANCH}"
    if [ -n "$GITHUB_TOKEN" ]; then
        WF_LIST=$(curl -fsSL -H "Authorization: Bearer ${GITHUB_TOKEN}" -H "User-Agent: prov" "$API" 2>/dev/null || echo "")
    else
        WF_LIST=$(curl -fsSL -H "User-Agent: prov" "$API" 2>/dev/null || echo "")
    fi
    echo "$WF_LIST" | python3 - "$RAW" "$WF_DIR" <<'PYEOF'
import sys, json, urllib.request, os
raw, wf_dir = sys.argv[1], sys.argv[2]
data = sys.stdin.read()
try:
    items = json.loads(data)
except Exception:
    items = []
if isinstance(items, list):
    for it in items:
        n = it.get("name","")
        if n.endswith(".json"):
            try:
                urllib.request.urlretrieve(f"{raw}/workflows/{n}", os.path.join(wf_dir, n))
                print(f"  workflow ← {n}")
            except Exception as e:
                print(f"  !! {n}: {e}")
PYEOF
else
    echo "  GITHUB_REPO не задан — пропуск избранного/воркфлоу"
fi

echo "=== provision.sh готово ==="
echo "Установщик: порт ${DOWNLOADER_PORT}"
