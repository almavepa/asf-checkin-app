import argparse
import os
import subprocess
import sys
import tempfile
import threading
import requests
import tkinter as tk
from tkinter import ttk

def download_with_progress(url, dest, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = requests.get(url, headers=headers, stream=True, timeout=30)
    r.raise_for_status()
    total = int(r.headers.get("Content-Length", 0))

    with open(dest, "wb") as f:
        downloaded = 0
        for chunk in r.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                yield downloaded, total

def run_gui_download(url, dest, token=None):
    root = tk.Tk()
    root.title("Atualização do Checkin")
    root.geometry("400x120")
    root.resizable(False, False)

    label = tk.Label(root, text="A transferir atualização...")
    label.pack(pady=10)

    progress = ttk.Progressbar(root, length=350, mode="determinate")
    progress.pack(pady=5)

    percent = tk.Label(root, text="0%")
    percent.pack()

    def worker():
        try:
            for downloaded, total in download_with_progress(url, dest, token):
                if total > 0:
                    value = int(downloaded * 100 / total)
                    progress["value"] = value
                    percent["text"] = f"{value}%"
                    root.update_idletasks()
            root.quit()
        except Exception as e:
            label["text"] = f"Erro: {e}"

    threading.Thread(target=worker, daemon=True).start()
    root.mainloop()

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--installer", required=True, help="URL do instalador no GitHub")
    p.add_argument("--dir", required=True, help="Pasta onde a app está instalada")
    p.add_argument("--pid", help="PID da app para fechar antes")
    p.add_argument("--sha256", help="(opcional) checksum")
    args = p.parse_args()

    token = os.getenv("GITHUB_TOKEN")
    tmpfile = Path(tempfile.gettempdir()) / "checkin_update.exe"

    # 1) Download com barra
    run_gui_download(args.installer, tmpfile, token)

    # 2) Se pid fornecido, tentar fechar a app
    if args.pid and args.pid.isdigit():
        try:
            import psutil
            psutil.Process(int(args.pid)).terminate()
        except Exception:
            pass

    # 3) Executar instalador silenciosamente
    subprocess.run([
        str(tmpfile),
        "/VERYSILENT", "/NORESTART", "/SP-", "/CURRENTUSER"
    ], check=True)

    # 4) Relançar app principal (opcional)
    app_exe = Path(args.dir) / "CheckinApp.exe"
    if app_exe.exists():
        subprocess.Popen([str(app_exe)], close_fds=True)

if __name__ == "__main__":
    from pathlib import Path
    main()
