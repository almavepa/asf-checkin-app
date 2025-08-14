# main.py
# - Silently checks GitHub private release for updates on start
# - Prompts for token on first run (encrypted save in %APPDATA%\ASFormacao\Checkin)
# - Launches your Tk UI in interface.py
import os
import re
import subprocess
import sys
from pathlib import Path
import importlib, importlib.util
from pathlib import Path
import runpy
import Interface



import requests

from version import __version__
from config import load_token, prompt_and_store_token

OWNER = "almavepa"
REPO  = "asf-checkin-app"
GITHUB_LATEST = f"https://api.github.com/repos/{OWNER}/{REPO}/releases/latest"
INSTALLER_PATTERN = r"CheckinSetup-v\d+\.\d+\.\d+\.exe"
TIMEOUT = 12
UPDATER_NAME = "updater_install.exe" if getattr(sys, "frozen", False) else "updater_install.py"
# Ensure the app directory is importable (both source and PyInstaller)
BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)).resolve()
INTERFACE_PATH = BASE / "Interface.py"

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
    # Token: load or prompt once (silent otherwise)
    token = load_token()
    if not token:
        token = prompt_and_store_token()
        if not token:
            # No token → skip update check, still run the app
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
            # Silent handoff: spawn updater and exit. Updater waits for us to close.
            subprocess.Popen(cmd, cwd=base, env=env)
            sys.exit(0)
    except Exception as e:
        # Fail safe: never block app if update check fails
        print(f"[update] Check failed: {e}")
        print(f"[update] Check failed: {e}")


def _run_ui():
    interface = _load_interface()
    if hasattr(interface, "main") and callable(interface.main):
        interface.main()
    else:
        # fallback: execute module as __main__ (runs if you didn’t define main())
        runpy.run_module("Interface", run_name="__main__")


if __name__ == "__main__":
    _maybe_update_silent()
    _run_ui()
