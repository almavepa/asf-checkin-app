import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import requests

TIMEOUT = 30
CHUNK = 1024 * 512  # 512 KB chunks


def _exists(pid: int) -> bool:
    try:
        if pid <= 0:
            return False
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _wait_exit(pid: int, timeout=180):
    import time
    start = time.time()
    while time.time() - start < timeout:
        if not _exists(pid):
            return True
        time.sleep(0.5)
    return False


def _gh_headers():
    token = os.getenv("GITHUB_TOKEN")
    h = {}
    if token:
        h["Authorization"] = f"Bearer {token}"
        h["Accept"] = "application/octet-stream"
    return h


def _download(url: str, dest: Path):
    with requests.get(url, stream=True, timeout=TIMEOUT, headers=_gh_headers()) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(CHUNK):
                if chunk:
                    f.write(chunk)


def _read_sha256_text(txt: str) -> str | None:
    t = txt.strip()
    for line in t.splitlines():
        parts = line.strip().split()
        if parts and len(parts[0]) == 64:
            return parts[0].lower()
    if len(t) == 64:
        return t.lower()
    return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1024 * 1024), b""):
            h.update(c)
    return h.hexdigest().lower()


def _run_installer(installer: Path, install_dir: Path):
    cmd = [
        str(installer),
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/CLOSEAPPLICATIONS",
        "/RESTARTAPPLICATIONS",
        f'/DIR="{install_dir}"'
    ]
    subprocess.Popen(" ".join(cmd), shell=True)


def main():
    ap = argparse.ArgumentParser(description="Silent installer updater")
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("--dir", required=True)
    ap.add_argument("--installer", required=True)
    ap.add_argument("--sha256")
    args = ap.parse_args()

    if not _wait_exit(args.pid):
        print("Timeout waiting for app to close.")
        sys.exit(1)

    install_dir = Path(args.dir).resolve()

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        setup = tmp / "setup.exe"

        print("Downloading installer…")
        _download(args.installer, setup)

        if args.sha256:
            print("Verifying SHA256…")
            r = requests.get(args.sha256, timeout=TIMEOUT, headers=_gh_headers())
            r.raise_for_status()
            expected = _read_sha256_text(r.text)
            if expected and _sha256(setup) != expected:
                print("ERROR: SHA256 mismatch.")
                sys.exit(1)

        print("Running installer silently…")
        _run_installer(setup, install_dir)


if __name__ == "__main__":
    main()
