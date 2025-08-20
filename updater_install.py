# updater_install.py — robusto, com logs de caminho e execução
from __future__ import annotations
import argparse, hashlib, os, sys, time, shutil, tempfile, subprocess
from pathlib import Path
from urllib.parse import urlparse
import requests

def dprint(*a): print("[updater]", *a, flush=True)

def download(url: str, out_path: Path, token: str | None = None) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": "CheckinUpdater/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    dprint("Downloading:", url)
    with requests.get(url, headers=headers, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError(f"Download falhou ou ficheiro vazio: {out_path}")
    dprint("Guardado em:", str(out_path))
    return out_path

def compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def wait_pid_exit(pid: int, timeout: int = 120):
    dprint(f"Aguardar que o processo {pid} termine…")
    start = time.time()
    while True:
        try:
            # no Windows isto dá erro se o processo já não existir
            os.kill(pid, 0)
            if time.time() - start > timeout:
                dprint("Timeout à espera do processo. Continuar mesmo assim.")
                return
            time.sleep(0.5)
        except OSError:
            dprint("Processo terminou.")
            return

def run_installer(installer: Path, log_path: Path | None = None):
    args = [
        str(installer),
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/SP-",
    ]
    if log_path:
        args.append(f"/LOG={str(log_path)}")
    dprint("A executar instalador:", args)
    # shell=False + lista de args evita problemas com espaços
    proc = subprocess.run(args, check=True)
    dprint("Instalador terminou com código", proc.returncode if hasattr(proc, "returncode") else 0)

def relaunch_app(app_dir: Path, exe_name: str = "CheckinApp.exe"):
    app = app_dir / exe_name
    if app.exists():
        dprint("A relançar app:", str(app))
        subprocess.Popen([str(app)], cwd=str(app_dir))
    else:
        dprint(f"(Aviso) Não encontrei {app}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, required=True, help="PID do processo principal")
    ap.add_argument("--dir", type=str, required=True, help="Pasta da app para relançar")
    ap.add_argument("--installer", type=str, required=True, help="URL do instalador .exe")
    ap.add_argument("--sha256", type=str, default="", help="URL do .sha256 ou hash diretamente")
    args = ap.parse_args()

    token = os.getenv("GITHUB_TOKEN") or None

    app_dir = Path(args.dir).resolve()
    temp_dir = Path(tempfile.gettempdir()) / "CheckinUpdater"
    temp_dir.mkdir(parents=True, exist_ok=True)

    # nome do ficheiro a partir do URL
    parsed = urlparse(args.installer)
    filename = Path(parsed.path).name or "installer.exe"
    installer_path = temp_dir / filename
    log_path = temp_dir / (filename + ".log")

    try:
        # 1) descarregar instalador
        download(args.installer, installer_path, token)

        # 2) validar sha256, se fornecido
        if args.sha256:
            expected = None
            if args.sha256.startswith("http"):
                sha_file = temp_dir / (filename + ".sha256.txt")
                download(args.sha256, sha_file, token)
                txt = sha_file.read_text(encoding="utf-8", errors="ignore")
                # procura um hex de 64 chars
                import re
                m = re.search(r"\b[a-fA-F0-9]{64}\b", txt)
                if m:
                    expected = m.group(0).lower()
            else:
                h = args.sha256.strip().lower()
                if len(h) == 64 and all(c in "0123456789abcdef" for c in h):
                    expected = h
            if expected:
                got = compute_sha256(installer_path)
                dprint("SHA256 esperado:", expected)
                dprint("SHA256 obtido :", got)
                if got != expected:
                    raise RuntimeError("SHA256 não corresponde. Abortado.")

        # 3) esperar que a app feche
        wait_pid_exit(args.pid)

        # 4) correr instalador
        run_installer(installer_path, log_path)

        # 5) relançar app
        relaunch_app(app_dir)

    except Exception as e:
        dprint("Falhou update:", e)
        # última tentativa: abrir pasta temp para inspeção
        try:
            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer", str(temp_dir)])
        except Exception:
            pass
        sys.exit(1)

if __name__ == "__main__":
    main()
