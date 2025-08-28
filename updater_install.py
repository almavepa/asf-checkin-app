# updater_install.py
# - Download progress window (Tk)
# - Silent install (/VERYSILENT)
# - Optional SHA256 check
# - Works with or without --installer (auto-resolves latest release asset)
# - Best-effort close by --pid and relaunch app after install

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

# --- REPO CONFIG (ajusta se mudares o repo) ---
OWNER = "almavepa"
REPO  = "asf-checkin-app"
ASSET_RE = re.compile(r"^CheckinSetup-v\d+\.\d+\.\d+\.exe$")
UA = {"User-Agent": "ASF-Checkin-Updater/1.0"}
TIMEOUT = 15


# --------------- GITHUB ---------------
def latest_asset(token: Optional[str]) -> Tuple[str, Optional[str]]:
    """Return (installer_url, sha256_url_or_None) for the latest release."""
    url = f"https://api.github.com/repos/{OWNER}/{REPO}/releases/latest"
    headers = UA.copy()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = requests.get(url, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    rel = r.json()
    exe = None
    sha = None
    for a in rel.get("assets", []) or []:
        name = a.get("name") or ""
        if ASSET_RE.match(name):
            exe = a.get("browser_download_url")
        elif name.endswith(".sha256"):
            sha = a.get("browser_download_url")
    if not exe:
        raise RuntimeError("Installer asset not found in latest release.")
    return exe, sha


# --------------- CHECKSUM ---------------
def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def fetch_sha_value(src: str) -> str:
    """Accepts URL or local .sha256 file; returns hex digest."""
    if re.match(r"^https?://", src, re.I):
        t = requests.get(src, headers=UA, timeout=TIMEOUT).text
    else:
        t = Path(src).read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"[A-Fa-f0-9]{64}", t)
    if not m:
        raise RuntimeError("No SHA256 found in .sha256")
    return m.group(0).lower()


# --------------- DOWNLOAD (with progress) ---------------
def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}"
        n /= 1024

def producer_download(url: str, dest: Path, q: "queue.Queue[tuple]", token: Optional[str]):
    """Stream download and push (done_bytes, total_bytes, rate_Bps) to queue."""
    headers = UA.copy()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with requests.get(url, headers=headers, stream=True, timeout=TIMEOUT) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", "0")) or None
        tmp = dest.with_suffix(".part")
        t0 = time.time()
        done = 0
        with tmp.open("wb") as f:
            for chunk in r.iter_content(1024 * 64):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                rate = done / max(1e-6, (time.time() - t0))
                q.put((done, total, rate))
        tmp.replace(dest)
    q.put(("done", None, None))

def gui_download(url: str, dest: Path, token: Optional[str]) -> None:
    q: "queue.Queue[tuple]" = queue.Queue()

    root = tk.Tk()
    root.title("Updating Checkin")
    root.resizable(False, False)

    pad = {"padx": 12, "pady": 6}
    frm = ttk.Frame(root)
    frm.grid(**pad)

    lbl = ttk.Label(frm, text="Downloading update…")
    lbl.grid(row=0, column=0, sticky="w")
    bar = ttk.Progressbar(frm, length=360, mode="determinate")
    bar.grid(row=1, column=0, sticky="ew", pady=(4, 0))
    info = ttk.Label(frm, text="")
    info.grid(row=2, column=0, sticky="w")

    th = threading.Thread(target=producer_download, args=(url, dest, q, token), daemon=True)
    th.start()

    total = None

    def tick():
        nonlocal total
        try:
            while True:
                d, t, rate = q.get_nowait()
                if d == "done":
                    bar["value"] = 100
                    info.config(text="Download complete.")
                    root.after(300, root.destroy)
                    return
                if t and total is None:
                    total = t
                    bar["maximum"] = t
                if isinstance(d, int):
                    if total:
                        bar["value"] = d
                        remain = max(0, total - d)
                        eta = int(remain / max(1, rate)) if rate else 0
                        info.config(text=f"{human(d)} / {human(total)}  •  {human(rate)}/s  •  ETA {eta}s")
                    else:
                        # Unknown length → indeterminate
                        bar.config(mode="indeterminate")
                        bar.start(40)
                        info.config(text=f"{human(d)} downloaded  •  {human(rate)}/s")
        except queue.Empty:
            pass
        root.after(100, tick)

    root.after(100, tick)
    root.mainloop()


# --------------- APP CONTROL ---------------
def best_effort_kill(pid_str: Optional[str]):
    if not pid_str or not pid_str.isdigit():
        return
    pid = int(pid_str)
    try:
        import psutil  # optional
        p = psutil.Process(pid)
        p.terminate()
        try:
            p.wait(10)
        except Exception:
            pass
        return
    except Exception:
        # fallback for systems without psutil
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


# --------------- MAIN ---------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", help="App directory (where CheckinApp.exe lives). Used to relaunch after install.")
    ap.add_argument("--installer", help="Installer URL. If omitted, auto-fetch latest release asset.")
    ap.add_argument("--sha256", help="SHA256 url or file (optional).")
    ap.add_argument("--pid", help="PID to terminate before install (optional).")
    args = ap.parse_args()

    token = os.getenv("GITHUB_TOKEN") or None

    # Resolve installer URL if not provided
    inst_url = args.installer
    sha_src = args.sha256
    if not inst_url:
        try:
            inst_url, sha_auto = latest_asset(token)
            if not sha_src:
                sha_src = sha_auto
        except Exception as e:
            messagebox.showerror("Update", f"Failed to resolve latest release:\n{e}")
            return

    # Choose download target
    # Prefer a stable per-user folder (not %TEMP%) to avoid AV interference
    base = Path(args.dir) if args.dir else Path(os.getenv("LOCALAPPDATA", Path.home())) / "ASFormacao" / "Checkin"
    dldir = base / "updates"
    try:
        dldir.mkdir(parents=True, exist_ok=True)
    except Exception:
        # fallback to TEMP
        dldir = Path(tempfile.gettempdir())

    name = inst_url.split("/")[-1]
    dest = dldir / name

    # Close running app if pid provided
    best_effort_kill(args.pid)

    # Download with progress
    try:
        gui_download(inst_url, dest, token)
    except Exception as e:
        try:
            messagebox.showerror("Update", f"Download failed:\n{e}")
        except Exception:
            pass
        return

    # Optional checksum validation
    if sha_src:
        try:
            expected = fetch_sha_value(sha_src)
            got = sha256_of(dest)
            if got.lower() != expected.lower():
                messagebox.showerror("Update", "Checksum mismatch. Update aborted.")
                return
        except Exception:
            # Not fatal; proceed to install
            pass

    # Silent install (no wizard)
    try:
        subprocess.run(
            [str(dest), "/VERYSILENT", "/NORESTART", "/SP-", "/CURRENTUSER"],
            check=True
        )
    except Exception as e:
        try:
            messagebox.showerror("Update", f"Silent install failed:\n{e}")
        except Exception:
            pass
        return

    # Relaunch app if we know its directory
    try:
        if args.dir:
            app_exe = Path(args.dir) / "CheckinApp.exe"
            if app_exe.exists():
                # tiny delay to ensure files are settled
                time.sleep(0.8)
                subprocess.Popen([str(app_exe)], close_fds=True)
    except Exception:
        pass


if __name__ == "__main__":
    main()
