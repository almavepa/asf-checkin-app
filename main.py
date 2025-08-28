# main.py
# - Silently checks GitHub private release for updates on start (apenas no .exe, OPCIONAL)
# - Prompts for token on first run (encrypted save in %APPDATA%\ASFormacao\Checkin)
# - Ensures first-run seed files (settings.json, students.py, data/email.html, data/fundo.jpg)
# - Launches your Tk UI in Interface.py

import os
import re
import sys
import json
import base64
import subprocess
from pathlib import Path
import importlib, importlib.util
import runpy

import requests

from version import __version__
from config import load_token, prompt_and_store_token

OWNER = "almavepa"
REPO  = "asf-checkin-app"
GITHUB_LATEST = f"https://api.github.com/repos/{OWNER}/{REPO}/releases/latest"
INSTALLER_PATTERN = r"CheckinSetup-v\d+\.\d+\.\d+\.exe"
TIMEOUT = 12
UPDATER_NAME = "updater_install.exe" if getattr(sys, "frozen", False) else "updater_install.py"

# ---- Paths to Interface (works frozen or from source) ----
BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)).resolve()
# Em Windows o FS é case-insensitive, mas mantemos o nome original
INTERFACE_PATH = BASE / "Interface.py"

# ---- App data location (per-user) ----
APPDATA_DIR = Path(os.getenv("APPDATA") or Path.home() / "AppData" / "Roaming") / "ASFormacao" / "Checkin"
DATA_DIR = APPDATA_DIR / "data"
SETTINGS_FILE = APPDATA_DIR / "settings.json"
STUDENTS_FILE = APPDATA_DIR / "students.py"

# ---- Default seeds (safe first-run) ----
DEFAULT_SETTINGS = {
    "qr_box_size": 10,
    "qr_border": 4,
    "email_sender_name": "ASFormação",
    "school_name": "ASFormação",
    "min_seconds_between_reads": 5,
    "google_sheet_id": "",
    "sheet_name": "Sheet1",
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_user": "",
    "smtp_password": "",
    "from_email": "",
    "notify_guardians": False
}

DEFAULT_STUDENTS = """# dicionário de alunos: ID interno -> dados mínimos
students = {
    "S1001": {"number": 1001, "name": "Aluno Teste", "email": "encarregado@example.com"}
}
"""

DEFAULT_EMAIL_HTML = """<!doctype html>
<html><body>
  <p>Olá,</p>
  <p>Este é um email de teste do sistema de entradas.</p>
</body></html>
"""

# 1x1 px JPG (branco) para garantir que existe um fundo válido
FUNDO_JPG_BASE64 = (
    "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDABQODxAQEBQRFBUUGB0bKy0qKy8yPzQ+"
    "RUtBVVlfaH+GhY2SoKq1ucTG////////////////////////////////////////"
    "/////////////////////////////////////////////2wBDAUVERkdISGBoYH+J"
    "iYj////////////////////////////////////////////////////////////////"
    "//////////////////////////////////////////wAARCAAQABADASIAAhEBAxEB"
    "/8QAFQABAQAAAAAAAAAAAAAAAAAAAAb/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFA"
    "EBAAAAAAAAAAAAAAAAAAAAAP/EABQRAQAAAAAAAAAAAAAAAAAAAAD/2gAMAwEAAhED"
    "EQA/ALQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP/Z"
)

def _ensure_first_run_files():
    """Create per-user data folder and minimum seed files if they don't exist.
       Also merge missing keys into settings.json to avoid None values.
    """
    APPDATA_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # settings.json: create if missing, or merge defaults if partial/corrupted
    cfg = {}
    if SETTINGS_FILE.exists():
        try:
            cfg = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if not isinstance(cfg, dict):
                cfg = {}
        except Exception:
            cfg = {}
    changed = False
    for k, v in DEFAULT_SETTINGS.items():
        if k not in cfg:
            cfg[k] = v
            changed = True
    if not SETTINGS_FILE.exists() or changed:
        SETTINGS_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")

    # students.py: minimal if missing
    if not STUDENTS_FILE.exists():
        STUDENTS_FILE.write_text(DEFAULT_STUDENTS, encoding="utf-8")

    # data/email.html
    email_file = DATA_DIR / "email.html"
    if not email_file.exists():
        email_file.write_text(DEFAULT_EMAIL_HTML, encoding="utf-8")

    # data/fundo.jpg (tiny valid JPG)
    fundo_file = DATA_DIR / "fundo.jpg"
    if not fundo_file.exists():
        try:
            fundo_file.write_bytes(base64.b64decode(FUNDO_JPG_BASE64))
        except Exception:
            # fallback: create empty file (still better than nothing)
            fundo_file.touch()

def _load_interface():
    # 1) Try normal import (works when running from project root or frozen)
    try:
        return importlib.import_module("Interface")
    except ModuleNotFoundError:
        pass
    # 2) Try loading by absolute file path (works even if CWD is elsewhere)
    if INTERFACE_PATH.exists():
        spec = importlib.util.spec_from_file_location("Interface", str(INTERFACE_PATH))
        mod = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(mod)  # type: ignore
        return mod
    # 3) Nice error with diagnostics
    raise ModuleNotFoundError(
        f"Interface.py not found.\nSearched:\n- import 'Interface' on sys.path\n- {INTERFACE_PATH}"
    )

def app_dir() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) \
        else Path(__file__).resolve().parent

def _gh_headers(token: str | None):
    h = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "CheckinUpdater/1.0"
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h

def _vtuple(s: str):
    return tuple(int(x) for x in re.findall(r"\d+", s))

def _fetch_latest(token: str):
    r = requests.get(GITHUB_LATEST, timeout=TIMEOUT, headers=_gh_headers(token))
    r.raise_for_status()
    data = r.json()
    tag = (data.get("tag_name") or "").lstrip("vV")
    inst_url = None
    sha_url = None
    for a in data.get("assets", []):
        name = (a.get("name") or "")
        if re.fullmatch(INSTALLER_PATTERN, name):
            inst_url = a.get("browser_download_url")
        elif name.endswith(".sha256"):
            sha_url = a.get("browser_download_url")
    if not inst_url:
        raise RuntimeError("Installer asset not found in latest release (check INSTALLER_PATTERN).")
    return tag, inst_url, sha_url

def _maybe_update_silent():
    """
    MODO SILENCIOSO (opcional):
    - Só tenta update silencioso quando EMPACOTADO (PyInstaller) **e**
      a env var CHECKIN_SILENT_UPDATE=1.
    - Por defeito NÃO fecha a app nem faz handoff; a UI (Interface.py) mostra progresso.
    """
    if not getattr(sys, "frozen", False):
        return  # a correr do source → não tenta atualizar silenciosamente

    if os.getenv("CHECKIN_SKIP_UPDATE") == "1":
        return

    # Novo comportamento: apenas se explicitamente pedido
    if os.getenv("CHECKIN_SILENT_UPDATE") != "1":
        return

    token = load_token()
    if not token:
        token = prompt_and_store_token()
        if not token:
            return
    try:
        remote_ver, inst_url, sha_url = _fetch_latest(token)
        if _vtuple(remote_ver) > _vtuple(__version__):
            base = app_dir()
            upd = base / UPDATER_NAME
            if not upd.exists():
                print(f"[update] Updater not found at {upd}")
                return
            args = [
                str(upd),
                "--pid", str(os.getpid()),
                "--dir", str(base),
                "--installer", inst_url
            ]
            if sha_url:
                args += ["--sha256", sha_url]
            env = os.environ.copy()
            env["GITHUB_TOKEN"] = token
            cmd = [sys.executable] + args if upd.suffix.lower() == ".py" else args
            # Em modo silencioso, lançamos o updater e deixamos a app continuar
            subprocess.Popen(cmd, cwd=base, env=env)
            # NOTA: já NÃO fazemos sys.exit(0). A UI vai abrir normalmente.
    except Exception as e:
        print(f"[update] Check failed: {e}")

def _run_ui():
    interface = _load_interface()
    if hasattr(interface, "main") and callable(interface.main):
        interface.main()
    else:
        # fallback: execute module as __main__ (runs if you didn’t define main())
        runpy.run_module("Interface", run_name="__main__")

if __name__ == "__main__":
    # 1) Garantir ficheiros mínimos por utilizador (evita int(None))
    _ensure_first_run_files()
    # 2) Check updates (modo silencioso OPcional: só se CHECKIN_SILENT_UPDATE=1)
    _maybe_update_silent()
    # 3) Arrancar UI (o Interface.py mostra a janela de atualizações com progresso)
    _run_ui()
