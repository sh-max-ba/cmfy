#!/usr/bin/env python3
"""
Model Downloader — web-установщик моделей для ComfyUI на Vast.ai.

Возможности:
  - выбор папки: автоскан реальных папок models/ + ввод своей
  - прогресс-бар со скоростью и ETA (парсится из aria2c)
  - отмена активной загрузки
  - список уже скачанного

Запуск:  python3 model_downloader.py
Порт:    7000 (DOWNLOADER_PORT)
Токены:  CIVITAI_TOKEN, HF_TOKEN (из окружения)
"""

import os
import re
import json
import shutil
import signal
import threading
import subprocess
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --- Конфигурация ----------------------------------------------------------

PORT = int(os.environ.get("DOWNLOADER_PORT", "7000"))
COMFY = os.environ.get("COMFYUI_PATH", "/workspace/ComfyUI")
MODELS = os.path.join(COMFY, "models")

CIVITAI_TOKEN = os.environ.get("CIVITAI_TOKEN", "")
HF_TOKEN = os.environ.get("HF_TOKEN", "")

# Стандартные папки ComfyUI — гарантированно показываем их, даже если пусты.
KNOWN_FOLDERS = [
    "loras", "checkpoints", "vae", "controlnet", "clip", "clip_vision",
    "embeddings", "upscale_models", "unet", "diffusion_models",
    "style_models", "gligen", "hypernetworks", "photomaker",
]

JOBS = {}
JOB_LOCK = threading.Lock()
_job_counter = 0


# --- Папки ------------------------------------------------------------------

def scan_folders():
    """Реальные папки в models/ + известные стандартные, объединённые."""
    found = set()
    if os.path.isdir(MODELS):
        for name in os.listdir(MODELS):
            if os.path.isdir(os.path.join(MODELS, name)) and not name.startswith("."):
                found.add(name)
    folders = sorted(found | set(KNOWN_FOLDERS))
    return folders


def safe_folder(folder):
    """Защита от выхода за пределы models/ (никаких ../, абсолютных путей)."""
    raw = (folder or "").strip()
    if not raw or raw.startswith("/") or "\\" in raw:
        raise ValueError("Недопустимое имя папки")
    folder = raw.strip("/")
    if not folder or ".." in folder.split("/"):
        raise ValueError("Недопустимое имя папки")
    return folder


# --- Резолверы ссылок -------------------------------------------------------

def resolve_civitai(url):
    if "/api/download/models/" in url:
        return _civitai_auth(url), None
    vid = None
    m = re.search(r"[?&]modelVersionId=(\d+)", url)
    if m:
        vid = m.group(1)
    else:
        m = re.search(r"/models/(\d+)", url)
        if not m:
            raise ValueError("Не похоже на ссылку civitai с id модели")
        data = _fetch_json(f"https://civitai.com/api/v1/models/{m.group(1)}")
        versions = data.get("modelVersions", [])
        if not versions:
            raise ValueError("У модели нет версий")
        vid = versions[0]["id"]
    vdata = _fetch_json(f"https://civitai.com/api/v1/model-versions/{vid}")
    files = vdata.get("files", [])
    if not files:
        raise ValueError("В версии нет файлов")
    primary = next((f for f in files if f.get("primary")), files[0])
    return _civitai_auth(primary["downloadUrl"]), primary.get("name")


def _civitai_auth(url):
    if CIVITAI_TOKEN:
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}token={CIVITAI_TOKEN}"
    return url


def resolve_hf(url):
    return url.replace("/blob/", "/resolve/")


def resolve_github(url):
    if "/blob/" in url:
        url = url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
    return url


def resolve(url):
    headers, name = [], None
    if "civitai.com" in url:
        url, name = resolve_civitai(url)
    elif "huggingface.co" in url:
        url = resolve_hf(url)
        if HF_TOKEN:
            headers.append(f"Authorization: Bearer {HF_TOKEN}")
    elif "github.com" in url or "raw.githubusercontent.com" in url:
        url = resolve_github(url)
    return url, headers, name


def _fetch_json(api_url):
    req = urllib.request.Request(api_url, headers={"User-Agent": "model-downloader"})
    if CIVITAI_TOKEN and "civitai.com" in api_url:
        req.add_header("Authorization", f"Bearer {CIVITAI_TOKEN}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


# --- Парсинг прогресса aria2c ----------------------------------------------

# Пример строки aria2c:
# [#abc123 1.2GiB/3.4GiB(35%) CN:16 DL:45MiB ETA:1m2s]
PROGRESS_RE = re.compile(
    r"\((?P<pct>\d+)%\).*?DL:(?P<speed>[\d.]+\w+).*?ETA:(?P<eta>[\dhms]+)"
)
PROGRESS_RE_NOPCT = re.compile(r"DL:(?P<speed>[\d.]+\w+)")


def parse_progress(line):
    m = PROGRESS_RE.search(line)
    if m:
        return {
            "pct": int(m.group("pct")),
            "speed": m.group("speed"),
            "eta": m.group("eta"),
        }
    m = PROGRESS_RE_NOPCT.search(line)
    if m:
        return {"pct": None, "speed": m.group("speed"), "eta": None}
    return None


# --- Скачивание -------------------------------------------------------------

def run_download(job_id, url, folder, custom_name):
    out_dir = os.path.join(MODELS, folder)
    os.makedirs(out_dir, exist_ok=True)

    def setj(**kw):
        with JOB_LOCK:
            JOBS[job_id].update(kw)

    def logline(msg):
        with JOB_LOCK:
            JOBS[job_id]["log"] += msg + "\n"

    try:
        final_url, headers, suggested = resolve(url)
        name = custom_name or suggested
        setj(name=name or "(имя определит сервер)")

        cmd = [
            "aria2c", "-x", "16", "-s", "16", "-k", "1M",
            "--summary-interval=1", "--console-log-level=warn",
            "--allow-overwrite=true", "--auto-file-renaming=false",
            "-d", out_dir,
        ]
        if name:
            cmd += ["-o", name]
        for h in headers:
            cmd += ["--header", h]
        cmd.append(final_url)

        logline(f"Папка: {out_dir}")
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, preexec_fn=os.setsid,
        )
        setj(pid=proc.pid)

        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            prog = parse_progress(line)
            if prog:
                setj(pct=prog["pct"], speed=prog["speed"], eta=prog["eta"])
            else:
                logline(line)

        proc.wait()
        with JOB_LOCK:
            cancelled = JOBS[job_id].get("status") == "cancelled"
        if cancelled:
            logline("Отменено пользователем ⛔")
        elif proc.returncode == 0:
            setj(status="done", pct=100, speed="", eta="")
            logline("Готово ✅")
        else:
            setj(status="error")
            logline(f"Ошибка (код {proc.returncode})")
    except Exception as e:
        setj(status="error")
        logline(f"Исключение: {e}")


def start_job(url, folder, custom_name):
    global _job_counter
    with JOB_LOCK:
        _job_counter += 1
        job_id = str(_job_counter)
        JOBS[job_id] = {
            "status": "running", "name": "", "folder": folder,
            "log": "", "url": url, "pct": None, "speed": "", "eta": "",
            "pid": None,
        }
    threading.Thread(
        target=run_download, args=(job_id, url, folder, custom_name), daemon=True
    ).start()
    return job_id


def cancel_job(job_id):
    with JOB_LOCK:
        job = JOBS.get(job_id)
        if not job or job["status"] != "running":
            return False
        pid = job.get("pid")
        job["status"] = "cancelled"
    if pid:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            pass
    return True


# --- Список скачанного ------------------------------------------------------

def list_models():
    result = {}
    if not os.path.isdir(MODELS):
        return result
    for folder in sorted(os.listdir(MODELS)):
        path = os.path.join(MODELS, folder)
        if not os.path.isdir(path):
            continue
        files = []
        for f in sorted(os.listdir(path)):
            fp = os.path.join(path, f)
            if os.path.isfile(fp):
                files.append({"name": f, "size": _human(os.path.getsize(fp))})
        if files:
            result[folder] = files
    return result


def delete_model(folder, name):
    folder = safe_folder(folder)
    if "/" in name or ".." in name:
        raise ValueError("Недопустимое имя файла")
    fp = os.path.join(MODELS, folder, name)
    if os.path.isfile(fp):
        os.remove(fp)
        return True
    return False


def _human(n):
    for unit in ["Б", "КБ", "МБ", "ГБ", "ТБ"]:
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ПБ"


# --- HTML -------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Model Downloader</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { font-family: system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
         background:#0d0f14; color:#e6e8ec; margin:0; padding:24px; }
  .wrap { max-width: 900px; margin:0 auto; }
  h1 { font-size:22px; margin:0 0 2px; letter-spacing:-0.3px; }
  .sub { color:#7b818c; font-size:13px; margin-bottom:22px; }
  .card { background:#13161d; border:1px solid #222834; border-radius:14px;
          padding:20px; margin-bottom:18px; }
  label { display:block; font-size:12px; color:#9aa0aa; margin:12px 0 5px;
          text-transform:uppercase; letter-spacing:0.4px; font-weight:600; }
  input,select { width:100%; padding:11px 13px; background:#0d0f14;
          border:1px solid #2a313d; border-radius:9px; color:#e6e8ec; font-size:14px;
          transition:border-color .15s; }
  input:focus,select:focus { outline:none; border-color:#3b82f6; }
  .row { display:flex; gap:12px; flex-wrap:wrap; }
  .row > div { flex:1; min-width:180px; }
  .folder-row { display:flex; gap:8px; align-items:flex-end; }
  .folder-row select { flex:2; } .folder-row input { flex:1; }
  button { padding:12px 20px; background:#3b82f6; color:#fff; border:0;
           border-radius:9px; font-size:14px; font-weight:600; cursor:pointer;
           transition:background .15s; }
  button:hover { background:#2f6fe0; }
  button:disabled { background:#252b36; color:#5a616c; cursor:not-allowed; }
  .btn-go { margin-top:18px; width:100%; }
  .btn-cancel { background:#3a2020; color:#f08585; padding:6px 14px; font-size:12px; }
  .btn-cancel:hover { background:#4a2525; }
  .btn-del { background:transparent; color:#5a616c; padding:3px 8px; font-size:11px; }
  .btn-del:hover { background:#3a1b1b; color:#f08585; }
  .job { background:#0d0f14; border:1px solid #222834; border-radius:11px;
         padding:14px 16px; margin-bottom:11px; }
  .job .head { display:flex; justify-content:space-between; align-items:center; gap:10px; }
  .job .title { font-weight:600; font-size:14px; word-break:break-all; }
  .meta { color:#6b7280; font-size:12px; margin-top:2px; word-break:break-all; }
  .badge { font-size:11px; padding:3px 10px; border-radius:20px; font-weight:700;
           text-transform:uppercase; letter-spacing:0.3px; white-space:nowrap; }
  .running { background:#2d3320; color:#d4e157; }
  .done { background:#16331f; color:#5fd98a; }
  .error { background:#3a1b1b; color:#f08585; }
  .cancelled { background:#2a2a2a; color:#9aa0aa; }
  .bar-wrap { height:8px; background:#1a1f29; border-radius:6px; margin-top:12px;
              overflow:hidden; }
  .bar { height:100%; background:linear-gradient(90deg,#3b82f6,#60a5fa);
         border-radius:6px; transition:width .4s ease; }
  .bar.indet { width:35% !important;
               animation:slide 1.2s ease-in-out infinite; }
  @keyframes slide { 0%{margin-left:-35%} 100%{margin-left:100%} }
  .stats { display:flex; gap:16px; margin-top:8px; font-size:12px; color:#8b909a; }
  .stats b { color:#cdd2da; font-weight:600; }
  pre { background:#070809; padding:9px 11px; border-radius:7px; overflow:auto;
        max-height:120px; font-size:11px; color:#7b818c; margin:10px 0 0;
        white-space:pre-wrap; word-break:break-all; }
  details { margin-top:4px; }
  summary { cursor:pointer; color:#7b818c; font-size:13px; padding:6px 0; }
  h3 { font-size:12px; color:#7b818c; margin:16px 0 4px; text-transform:uppercase;
       letter-spacing:0.4px; }
  table { width:100%; border-collapse:collapse; font-size:12px; }
  td { padding:5px 8px; border-bottom:1px solid #181d26; color:#b8bec8; }
  td.sz { text-align:right; color:#6b7280; white-space:nowrap; }
  td.act { text-align:right; width:60px; }
  .token-note { font-size:12px; color:#5a616c; margin-top:12px; }
  .ok{color:#5fd98a} .no{color:#f08585}
</style>
</head>
<body>
<div class="wrap">
  <h1>Model Downloader</h1>
  <div class="sub">civitai · huggingface · github → прямо на сервер</div>

  <div class="card">
    <label>Ссылка на модель</label>
    <input id="url" placeholder="https://civitai.com/models/12345  ·  https://huggingface.co/.../file.safetensors">

    <label>Папка назначения</label>
    <div class="folder-row">
      <select id="folder"></select>
      <input id="newfolder" placeholder="или новая папка">
    </div>

    <label>Имя файла (необязательно)</label>
    <input id="name" placeholder="оставь пустым — определит сам">

    <button class="btn-go" id="go" onclick="start()">Скачать</button>
    <div class="token-note">
      Токены — civitai: <span id="cv"></span> · HuggingFace: <span id="hf"></span>
      &nbsp;(env: CIVITAI_TOKEN, HF_TOKEN)
    </div>
  </div>

  <div id="jobs"></div>

  <details>
    <summary>Что уже скачано на сервере</summary>
    <div id="installed">загрузка…</div>
  </details>
</div>

<script>
async function loadFolders() {
  const r = await fetch('/folders');
  const d = await r.json();
  const sel = document.getElementById('folder');
  sel.innerHTML = d.folders.map(f => `<option value="${f}">${f}</option>`).join('');
  const lora = d.folders.indexOf('loras');
  if (lora >= 0) sel.selectedIndex = lora;
}

async function start() {
  const url = document.getElementById('url').value.trim();
  const newf = document.getElementById('newfolder').value.trim();
  const folder = newf || document.getElementById('folder').value;
  const name = document.getElementById('name').value.trim();
  if (!url) { alert('Вставь ссылку'); return; }
  const btn = document.getElementById('go');
  btn.disabled = true;
  try {
    await fetch('/download', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({url, folder, name})
    });
    document.getElementById('url').value = '';
    document.getElementById('name').value = '';
    document.getElementById('newfolder').value = '';
  } finally { btn.disabled = false; }
  loadFolders();
  poll();
}

async function cancel(id) {
  await fetch('/cancel', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({id})});
  poll();
}

function jobHtml(j) {
  const pct = j.pct;
  const barClass = (j.status === 'running' && pct === null) ? 'bar indet' : 'bar';
  const barStyle = pct !== null ? `width:${pct}%` : '';
  const showBar = j.status === 'running' || j.status === 'done';
  const stats = j.status === 'running'
    ? `<div class="stats">
         ${pct!==null?`<span><b>${pct}%</b></span>`:''}
         ${j.speed?`<span>↓ <b>${j.speed}</b></span>`:''}
         ${j.eta?`<span>ETA <b>${j.eta}</b></span>`:''}
       </div>` : '';
  const cancelBtn = j.status === 'running'
    ? `<button class="btn-cancel" onclick="cancel('${j.id}')">Отменить</button>` : '';
  return `<div class="card job">
    <div class="head">
      <div>
        <div class="title">${j.name || '(определяется…)'}</div>
        <div class="meta">${j.folder} · ${j.url}</div>
      </div>
      <div style="display:flex;gap:8px;align-items:center">
        ${cancelBtn}
        <span class="badge ${j.status}">${j.status}</span>
      </div>
    </div>
    ${showBar ? `<div class="bar-wrap"><div class="${barClass}" style="${barStyle}"></div></div>` : ''}
    ${stats}
    ${j.log ? `<details><summary>лог</summary><pre>${j.log}</pre></details>` : ''}
  </div>`;
}

async function poll() {
  const r = await fetch('/jobs');
  const d = await r.json();
  document.getElementById('jobs').innerHTML =
    d.jobs.map(jobHtml).join('') || '';
  document.getElementById('cv').innerHTML = d.civitai
    ? '<span class="ok">есть</span>' : '<span class="no">нет</span>';
  document.getElementById('hf').innerHTML = d.hf
    ? '<span class="ok">есть</span>' : '<span class="no">нет</span>';
  if (d.jobs.some(j => j.status === 'running')) setTimeout(poll, 1200);
  else loadInstalled();
}

async function loadInstalled() {
  const r = await fetch('/installed');
  const d = await r.json();
  let html = '';
  for (const [folder, files] of Object.entries(d)) {
    html += `<h3>${folder}</h3><table>`;
    html += files.map(f => `<tr>
      <td>${f.name}</td><td class="sz">${f.size}</td>
      <td class="act"><button class="btn-del"
        onclick="del('${folder}','${f.name.replace(/'/g,"\\'")}')">✕</button></td>
    </tr>`).join('');
    html += '</table>';
  }
  document.getElementById('installed').innerHTML = html || '<div class="sub">Пока пусто</div>';
}

async function del(folder, name) {
  if (!confirm('Удалить ' + name + '?')) return;
  await fetch('/delete', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({folder, name})});
  loadInstalled();
}

loadFolders();
poll();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or "{}")

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            self._send(200, HTML, "text/html; charset=utf-8")
        elif self.path == "/folders":
            self._send(200, json.dumps({"folders": scan_folders()}))
        elif self.path == "/jobs":
            with JOB_LOCK:
                jobs = [
                    {"id": k, **{kk: vv for kk, vv in v.items() if kk != "pid"}}
                    for k, v in sorted(JOBS.items(), key=lambda x: -int(x[0]))
                ]
            self._send(200, json.dumps({
                "jobs": jobs, "civitai": bool(CIVITAI_TOKEN), "hf": bool(HF_TOKEN),
            }))
        elif self.path == "/installed":
            self._send(200, json.dumps(list_models()))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        try:
            if self.path == "/download":
                p = self._read_json()
                url = (p.get("url") or "").strip()
                folder = safe_folder(p.get("folder") or "loras")
                name = (p.get("name") or "").strip() or None
                if not url:
                    self._send(400, json.dumps({"error": "no url"})); return
                jid = start_job(url, folder, name)
                self._send(200, json.dumps({"job_id": jid}))
            elif self.path == "/cancel":
                p = self._read_json()
                ok = cancel_job(str(p.get("id")))
                self._send(200, json.dumps({"cancelled": ok}))
            elif self.path == "/delete":
                p = self._read_json()
                ok = delete_model(p.get("folder"), p.get("name"))
                self._send(200, json.dumps({"deleted": ok}))
            else:
                self._send(404, json.dumps({"error": "not found"}))
        except Exception as e:
            self._send(400, json.dumps({"error": str(e)}))


def main():
    if not shutil.which("aria2c"):
        print("ВНИМАНИЕ: aria2c не найден. apt-get install -y aria2")
    os.makedirs(MODELS, exist_ok=True)
    print(f"Model Downloader → http://0.0.0.0:{PORT}  (models: {MODELS})")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
