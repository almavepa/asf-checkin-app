# updater_install.py
# - ZERO argumentos obrigatórios (funciona só com duplo clique)
# - Descobre o diretório da app pelo próprio .exe
# - Fecha a app atual (best-effort), mostra barra de progresso no download,
#   instala em silêncio e relança o CheckinApp.exe.
# - Aceita opcionalmente: --dir, --pid, --installer, --sha256 (se o main quiser passar)

import argparse
import hashlib
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

import requests
import tkinter as tk
from tkinter import ttk, messagebox

OWNER = "almavepa"
REPO  = "asf-checkin-app"
ASSET_RE = re.compile(r"^CheckinSetup-v\d+\.\d+\.\d+\.exe$")
UA = {"User-Agent": "ASF-Checkin-Updater/1.0"}
TIMEOUT = 15

def exe_dir() -> Path:
    """Pasta onde este updater está a correr (suporta .exe ou .py)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent

def latest_asset(token: Optional[str]) -> Tuple[str, Optional[str]]:
    url = f"https://api.github.com/repos/{OWNER}/{REPO}/releases/latest"
    headers = UA.copy()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = requests.get(url, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    rel = r.json()
    exe = None; sha = None
    for a in rel.get("assets", []) or []:
        name = (a.get("name") or "")
        if ASSET_RE.match(name):
            exe = a.get("browser_download_url")
        elif name.endswith(".sha256"):
            sha = a.get("browser_download_url")
    if not exe:
        raise RuntimeError("Installer asset not found in latest release.")
    return exe, sha

def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def fetch_sha_value(src: str) -> str:
    if re.match(r"^https?://", src, re.I):
        t = requests.get(src, headers=UA, timeout=TIMEOUT).text
    else:
        t = Path(src).read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"[A-Fa-f0-9]{64}", t)
    if not m:
        raise RuntimeError("No SHA256 found")
    return m.group(0).lower()

def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}"
        n /= 1024

def producer_download(url: str, dest: Path, q: "queue.Queue[tuple]", token: Optional[str]):
    headers = UA.copy()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with requests.get(url, headers=headers, stream=True, timeout=TIMEOUT) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", "0")) or None
        tmp = dest.with_suffix(".part")
        t0 = time.time(); done = 0
        with tmp.open("wb") as f:
            for chunk in r.iter_content(1024 * 64):
                if not chunk: continue
                f.write(chunk)
                done += len(chunk)
                rate = done / max(1e-6, (time.time()-t0))
                q.put((done, total, rate))
        tmp.replace(dest)
    q.put(("done", None, None))

def gui_download(url: str, dest: Path, token: Optional[str]) -> None:
    q: "queue.Queue[tuple]" = queue.Queue()
    root = tk.Tk(); root.title("Updating Checkin"); root.resizable(False, False)
    pad = {"padx":12, "pady":6}; frm = ttk.Frame(root); frm.grid(**pad)
    ttk.Label(frm, text="Downloading update…").grid(row=0, column=0, sticky="w")
    bar = ttk.Progressbar(frm, length=360, mode="determinate"); bar.grid(row=1, column=0, sticky="ew", pady=(4,0))
    info = ttk.Label(frm, text=""); info.grid(row=2, column=0, sticky="w")

    threading.Thread(target=producer_download, args=(url, dest, q, token), daemon=True).start()
    total = None
    def tick():
        nonlocal total
        try:
            while True:
                d, t, rate = q.get_nowait()
                if d == "done":
                    bar["value"] = 100; info.config(text="Download complete.")
                    root.after(300, root.destroy); return
                if t and total is None:
                    total = t; bar["maximum"] = t
                if isinstance(d, int):
                    if total:
                        bar["value"] = d
                        remain = max(0, total - d)
                        eta = int(remain / max(1, rate)) if rate else 0
                        info.config(text=f"{human(d)} / {human(total)}  •  {human(rate)}/s  •  ETA {eta}s")
                    else:
                        bar.config(mode="indeterminate"); bar.start(40)
                        info.config(text=f"{human(d)} downloaded  •  {human(rate)}/s")
        except queue.Empty:
            pass
        root.after(100, tick)
    root.after(100, tick); root.mainloop()

def best_effort_kill(dir_hint: Path):
    """Mata um CheckinApp.exe que esteja a correr (na mesma pasta), se existir."""
    # 1) taskkill por imagem e caminho
    try:
        exe = (dir_hint / "CheckinApp.exe").resolve()
        subprocess.run(["tasklist"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False)
        # tentar /FI por imagem
        subprocess.run(["taskkill", "/IM", "CheckinApp.exe", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    # 2) psutil (se existir) para garantir
    try:
        import psutil
        for p in psutil.process_iter(attrs=["name","exe"]):
            if (p.info.get("name") or "").lower() == "checkinapp.exe":
                try:
                    p.terminate(); p.wait(10)
                except Exception:
                    pass
    except Exception:
        pass

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", help="(opcional) pasta da app; por defeito, a pasta deste updater")
    ap.add_argument("--installer", help="(opcional) URL do instalador; por defeito, usa latest release")
    ap.add_argument("--sha256", help="(opcional) URL/ficheiro com sha256")
    ap.add_argument("--pid", help="(opcional) PID para terminar")
    args = ap.parse_args()

    token = os.getenv("GITHUB_TOKEN") or None

    # Base dir: auto, a não ser que --dir seja dado
    base = Path(args.dir) if args.dir else exe_dir()

    # Resolver URL do installer automaticamente se não vier
    inst_url = args.installer
    sha_src  = args.sha256
    if not inst_url:
        try:
            inst_url, sha_auto = latest_asset(token)
            if not sha_src:
                sha_src = sha_auto
        except Exception as e:
            try: messagebox.showerror("Update", f"Failed to resolve latest release:\n{e}")
            except Exception: pass
            return

    # Pasta de download estável (evita %TMP% colidir com Inno)
    dldir = base / "updates"
    try:
        dldir.mkdir(parents=True, exist_ok=True)
    except Exception:
        dldir = Path(tempfile.gettempdir())

    name = inst_url.split("/")[-1]
    dest = dldir / name

    # Fechar app (best-effort)
    if args.pid and args.pid.isdigit():
        try:
            import psutil
            p = psutil.Process(int(args.pid))
            p.terminate()
            try: p.wait(10)
            except Exception: pass
        except Exception:
            pass
    else:
        best_effort_kill(base)

    # Download com UI
    try:
        gui_download(inst_url, dest, token)
    except Exception as e:
        try: messagebox.showerror("Update", f"Download failed:\n{e}")
        except Exception: pass
        return

    # (Opcional) validar checksum
    if sha_src:
        try:
            expected = fetch_sha_value(sha_src)
            got = sha256_of(dest)
            if got.lower() != expected.lower():
                messagebox.showerror("Update", "Checksum mismatch. Update aborted.")
                return
        except Exception:
            pass  # não bloqueamos o update por falhar o fetch do sha

    # Instalar em modo silencioso
    try:
        subprocess.run([str(dest), "/VERYSILENT", "/NORESTART", "/SP-", "/CURRENTUSER"], check=True)
    except Exception as e:
        try: messagebox.showerror("Update", f"Silent install failed:\n{e}")
        except Exception: pass
        return

    # Relançar a app da pasta base
    try:
        app_exe = base / "CheckinApp.exe"
        if app_exe.exists():
            time.sleep(0.8)
            subprocess.Popen([str(app_exe)], close_fds=True)
    except Exception:
        pass

if __name__ == "__main__":
    main()
