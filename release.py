# release.py
import os
import re
import subprocess
from pathlib import Path
import shutil
import time

import requests

# ---- CONFIG ----
OWNER = "almavepa"
REPO  = "asf-checkin-app"
ICON = str((Path(__file__).parent / "checkin.ico").resolve())
OUTDIR = Path("dist")
ISS_PATH = Path("installer.iss")  # mantém exactamente este nome e local

TOKEN = os.getenv("GITHUB_TOKEN")  # defina no ambiente (recomendado)

# ---- Read version from version.py ----
m = re.search(r'__version__\s*=\s*"([^"]+)"', Path("version.py").read_text(encoding="utf-8"))
if not m:
    raise SystemExit("Could not read __version__ from version.py")
VERSION = m.group(1)
INSTALLER_NAME = f"CheckinSetup-v{VERSION}.exe"


def run(cmd):
    print(">", " ".join(str(c) for c in cmd))
    subprocess.run(cmd, check=True)


def ensure_iscc_on_path():
    try:
        subprocess.run(["iscc", "/?"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return "iscc"
    except Exception:
        full_path = r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
        if not Path(full_path).exists():
            raise SystemExit("Inno Setup `iscc` not found. Install Inno Setup 6 or update the path in release.py.")
        return full_path


def wait_until_unlocked(path: Path, timeout=10.0, poll=0.2):
    """Espera até o ficheiro estar livre (sem locks de AV/Indexação/OneDrive)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with path.open("rb"):
                return True
        except Exception:
            time.sleep(poll)
    return False


def build_pyinstaller():
    OUTDIR.mkdir(parents=True, exist_ok=True)

    # Clean old build/cache
    for folder in ["build", "__pycache__"]:
        p = Path(folder)
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)

    # Limpar ficheiros antigos em dist
    if OUTDIR.exists():
        for f in OUTDIR.iterdir():
            if f.is_file():
                try:
                    f.unlink()
                except Exception:
                    pass

    # Main app
    run([
        "pyinstaller", "main.py",
        "--noconfirm", "--onefile", "--clean",
        "--name", "CheckinApp",
        "--icon", ICON,
        "--add-data", "email.html;.",
        "--add-data", "fundo.jpg;.",
        "--hidden-import", "PIL._tkinter_finder",
        "--hidden-import", "Interface"
    ])

    # Updater
    run([
        "pyinstaller", "updater_install.py",
        "--noconfirm", "--onefile", "--clean",
        "--name", "updater_install",
        "--icon", ICON
    ])


def write_iss():
    OUTDIR.mkdir(parents=True, exist_ok=True)

    # cópia do ícone (evita locks no original se estiver aberto noutro programa)
    temp_icon = OUTDIR / f"setup_icon_{VERSION}.ico"
    shutil.copy2(ICON, temp_icon)
    icon_win_path = str(temp_icon).replace("/", "\\")

    # NOTA: mantemos OutputDir=dist e OutputBaseFilename=CheckinSetup-v{VERSION}
    iss = f"""
[Setup]
AppName=Checkin System
AppVersion={VERSION}
DefaultDirName={{userappdata}}\\ASFormacao\\Checkin
DefaultGroupName=ASFormacao\\Checkin
OutputDir=dist
OutputBaseFilename=CheckinSetup-v{VERSION}
Compression=lzma2
SolidCompression=yes
DisableProgramGroupPage=yes
SetupIconFile={icon_win_path}

[Files]
Source: "dist\\CheckinApp.exe"; DestDir: "{{{{app}}}}"; Flags: ignoreversion
Source: "dist\\updater_install.exe"; DestDir: "{{{{app}}}}"; Flags: ignoreversion

[Icons]
Name: "{{{{group}}}}\\Checkin System"; Filename: "{{{{app}}}}\\CheckinApp.exe"
Name: "{{{{commondesktop}}}}\\Checkin System"; Filename: "{{{{app}}}}\\CheckinApp.exe"

[Run]
Filename: "{{{{app}}}}\\CheckinApp.exe"; Flags: nowait postinstall skipifsilent
""".strip() + "\n"

    # Escrever para ficheiro temporário e fazer rename atómico para evitar locks
    tmp_path = ISS_PATH.with_suffix(".iss.tmp")
    tmp_path.write_text(iss, encoding="utf-8")
    tmp_path.replace(ISS_PATH)  # move/rename atómico

    # Esperar o desbloqueio do .iss (OneDrive/Defender podem agarrá-lo por instantes)
    if not wait_until_unlocked(ISS_PATH, timeout=10.0, poll=0.2):
        raise SystemExit(f"'{ISS_PATH}' está bloqueado por outro processo. Feche editores/OneDrive e tente novamente.")


def build_installer():
    iscc_cmd = ensure_iscc_on_path()
    write_iss()
    time.sleep(0.3)  # pequena folga extra
    run([iscc_cmd, str(ISS_PATH)])


def create_release_and_upload():
    if not TOKEN:
        raise SystemExit("GITHUB_TOKEN not set in environment. Set it or store via your app's first-run prompt (this script needs it too).")

    print("[i] Creating GitHub release v%s…" % VERSION)
    rel_api = f"https://api.github.com/repos/{OWNER}/{REPO}/releases"
    headers = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"}

    r = requests.post(rel_api, headers=headers, json={
        "tag_name": f"v{VERSION}",
        "name": f"Checkin v{VERSION}",
        "draft": False,
        "prerelease": False
    })
    r.raise_for_status()
    upload_url = r.json()["upload_url"].split("{")[0]

    asset_path = OUTDIR / INSTALLER_NAME
    if not asset_path.exists():
        raise SystemExit(f"Installer not found at {asset_path}")

    print("[i] Uploading installer…")
    with asset_path.open("rb") as f:
        ur = requests.post(
            f"{upload_url}?name={INSTALLER_NAME}",
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/octet-stream"},
            data=f
        )
    ur.raise_for_status()
    print("[✔] Release uploaded successfully.")


if __name__ == "__main__":
    print(f"[i] Building v{VERSION}")
    build_pyinstaller()
    build_installer()
    print(f"[DONE] v{VERSION} built and published.")
