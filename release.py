# release.py
import os
import re
import subprocess
from pathlib import Path
import shutil
import time
from string import Template
import argparse

import requests

# ---- CONFIG ----
OWNER = "almavepa"
REPO  = "asf-checkin-app"
ICON = str((Path(__file__).parent / "checkin.ico").resolve())
OUTDIR = Path("dist")  # output final do PyInstaller
TOKEN = os.getenv("GITHUB_TOKEN")
VERSION_FILE = Path("version.py")

# Pasta temporária fora do OneDrive para .iss
LOCALAPPDATA = Path(os.getenv("LOCALAPPDATA", str(Path.home()))).resolve()
SAFE_BUILD_DIR = LOCALAPPDATA / "ASFormacao" / "Checkin" / "build"
SAFE_BUILD_DIR.mkdir(parents=True, exist_ok=True)
ISS_PATH = SAFE_BUILD_DIR / f"installer_{os.getpid()}.iss"


# -------------------- UTIL --------------------
def run(cmd, check=True, capture_output=False, text=True):
    print(">", " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, check=check, capture_output=capture_output, text=text)

def ensure_iscc_on_path():
    try:
        subprocess.run(["iscc", "/?"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return "iscc"
    except Exception:
        full_path = r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
        if not Path(full_path).exists():
            raise SystemExit("Inno Setup `iscc` not found. Instala o Inno Setup 6 ou ajusta o caminho.")
        return full_path

def wait_until_unlocked(path: Path, timeout=10.0, poll=0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with path.open("rb"):
                return True
        except Exception:
            time.sleep(poll)
    return False

def read_version():
    m = re.search(r'__version__\s*=\s*"([^"]+)"', VERSION_FILE.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit("Could not read __version__ from version.py")
    return m.group(1)

def write_version(new_version: str):
    txt = VERSION_FILE.read_text(encoding="utf-8")
    txt = re.sub(r'__version__\s*=\s*"([^"]+)"', f'__version__ = "{new_version}"', txt)
    VERSION_FILE.write_text(txt, encoding="utf-8")

def bump_semver(ver: str, level: str) -> str:
    parts = ver.split(".")
    if len(parts) == 2:
        parts.append("0")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise SystemExit(f"Unsupported version format '{ver}'. Expected x.y.z")
    major, minor, patch = map(int, parts)
    if level == "major":
        major += 1; minor = 0; patch = 0
    elif level == "minor":
        minor += 1; patch = 0
    elif level == "patch":
        patch += 1
    else:
        raise SystemExit(f"Unknown bump level: {level}")
    return f"{major}.{minor}.{patch}"

def git_is_clean() -> bool:
    r = run(["git", "status", "--porcelain"], capture_output=True)
    return r.stdout.strip() == ""

def git_current_branch() -> str:
    r = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True)
    return r.stdout.strip()

def git_commit_all(message: str):
    run(["git", "add", "--all"])
    run(["git", "commit", "-m", message])

def git_tag(tag: str):
    run(["git", "tag", tag])

def git_push_with_tags():
    branch = git_current_branch()
    run(["git", "push", "origin", branch])
    run(["git", "push", "origin", "--tags"])


# -------------------- BUILD --------------------
def build_pyinstaller():
    OUTDIR.mkdir(parents=True, exist_ok=True)

    # limpar build/ e caches
    for folder in ["build", "__pycache__"]:
        p = Path(folder)
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)

    # limpar dist antigo
    if OUTDIR.exists():
        for f in OUTDIR.iterdir():
            if f.is_file():
                try: f.unlink()
                except: pass

    # Main app
    run([
        "pyinstaller", "main.py",
        "--noconfirm", "--onefile", "--clean",
        "--windowed",
        "--name", "CheckinApp",
        "--icon", ICON,
        "--add-data", "email.html;.",
        "--add-data", "fundo.jpg;.",
        "--hidden-import", "PIL._tkinter_finder",
        "--hidden-import", "Interface"
    ])

    # Updater sempre incluído
    run([
        "pyinstaller", "updater_install.py",
        "--noconfirm", "--onefile", "--clean",
        "--name", "updater_install",
        "--icon", ICON
    ])


def write_iss(version: str, use_icon: bool, out_iss: Path):
    out_iss.parent.mkdir(parents=True, exist_ok=True)

    abs_dist = str(OUTDIR.resolve()).replace("/", "\\")
    app_exe  = str((OUTDIR / "CheckinApp.exe").resolve()).replace("/", "\\")
    upd_exe  = str((OUTDIR / "updater_install.exe").resolve()).replace("/", "\\")
    icon_win = str((Path(ICON).resolve())).replace("/", "\\")

    icon_line = f"SetupIconFile={icon_win}" if use_icon else ""

    iss_tpl = Template(r"""
[Setup]
AppName=Checkin System
AppVersion=$VERSION
DefaultDirName={localappdata}\Programs\ASFormacao\Checkin
DefaultGroupName=ASFormacao\Checkin
OutputDir=$OUTDIR
OutputBaseFilename=CheckinSetup-v$VERSION
Compression=lzma2
SolidCompression=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
SetupLogging=yes
CloseApplications=yes
CloseApplicationsFilter=CheckinApp.exe
RestartApplications=no
$ICON_LINE

[Files]
Source: "$APP_EXE"; DestDir: "{app}"; Flags: ignoreversion
Source: "$UPD_EXE"; DestDir: "{app}"; Flags: ignoreversion 
[Icons]
Name: "{group}\Checkin System"; Filename: "{app}\CheckinApp.exe"
Name: "{userdesktop}\Checkin System"; Filename: "{app}\CheckinApp.exe"

[Run]
Filename: "{app}\CheckinApp.exe"; Flags: nowait postinstall skipifsilent
""")

    iss_text = iss_tpl.substitute(
        VERSION=version, OUTDIR=abs_dist,
        APP_EXE=app_exe, UPD_EXE=upd_exe,
        ICON_LINE=icon_line
    )
    out_iss.write_text(iss_text, encoding="utf-8")


def build_installer(version: str) -> Path:
    iscc_cmd = ensure_iscc_on_path()
    installer_name = f"CheckinSetup-v{version}.exe"
    old_installer = OUTDIR / installer_name
    if old_installer.exists():
        try:
            old_installer.unlink()
            print(f"[i] Deleted old installer: {old_installer}")
        except Exception as e:
            print(f"[!] Could not delete old installer: {e}")

    use_icon_first = True
    attempts, backoff = 5, 1.0
    for attempt in range(1, attempts + 1):
        try:
            write_iss(version, use_icon_first, ISS_PATH)
            if not wait_until_unlocked(ISS_PATH, timeout=5.0):
                print("[!] Aviso: .iss pode estar bloqueado; prosseguindo…")
            run([iscc_cmd, str(ISS_PATH)])
            break
        except subprocess.CalledProcessError:
            print(f"[!] ISCC falhou (tentativa {attempt}/{attempts}).")
            if use_icon_first:
                print("[i] A tentar novamente sem ícone…")
                use_icon_first = False
            if attempt < attempts:
                time.sleep(backoff)
                backoff *= 1.7
                continue
            raise
        finally:
            try:
                if ISS_PATH.exists():
                    ISS_PATH.unlink()
            except: pass

    out_path = OUTDIR / installer_name
    if not out_path.exists():
        raise SystemExit(f"[!] Installer not created in {OUTDIR}")
    print(f"[✔] Installer available at {out_path}")
    return out_path


# -------------------- RELEASE --------------------
def create_or_get_release_upload_url(version: str) -> str:
    if not TOKEN:
        raise SystemExit("GITHUB_TOKEN not set in environment.")
    rel_api = f"https://api.github.com/repos/{OWNER}/{REPO}/releases"
    headers = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"}
    r = requests.post(rel_api, headers=headers, json={
        "tag_name": f"v{version}",
        "name": f"Checkin v{version}",
        "draft": False, "prerelease": False
    })
    if r.status_code == 201:
        print(f"[i] Created GitHub release v{version}")
        return r.json()["upload_url"].split("{")[0]
    if r.status_code == 422:
        print(f"[i] Release v{version} já existe, a procurar…")
        r2 = requests.get(rel_api, headers=headers, params={"per_page": 100})
        r2.raise_for_status()
        for rel in r2.json():
            if rel.get("tag_name") == f"v{version}":
                print("[i] Found existing release.")
                return rel["upload_url"].split("{")[0]
        raise SystemExit("Release exists mas não consegui obter upload URL.")
    r.raise_for_status()

def upload_asset(upload_url: str, asset_path: Path):
    if not TOKEN:
        raise SystemExit("GITHUB_TOKEN not set in environment.")
    headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/octet-stream"}
    name = asset_path.name
    print(f"[i] Uploading asset {name}…")
    with asset_path.open("rb") as f:
        ur = requests.post(f"{upload_url}?name={name}", headers=headers, data=f)
    if ur.status_code == 422 and "already_exists" in ur.text:
        print("[i] Asset já existe. A apagar e re-upload…")
        rel_api = f"https://api.github.com/repos/{OWNER}/{REPO}/releases/tags/v{read_version()}"
        r = requests.get(rel_api, headers=headers)
        r.raise_for_status()
        data = r.json()
        asset = next((a for a in data.get("assets", []) if a.get("name") == name), None)
        if asset:
            requests.delete(asset["url"], headers=headers).raise_for_status()
            with asset_path.open("rb") as f2:
                requests.post(f"{upload_url}?name={name}", headers=headers, data=f2).raise_for_status()
            print("[✔] Asset re-uploaded.")
            return
    ur.raise_for_status()
    print("[✔] Asset uploaded.")


# -------------------- MAIN --------------------
def main():
    parser = argparse.ArgumentParser(description="Bump version, build installer, commit/push, and release.")
    parser.add_argument("--bump", choices=["patch", "minor", "major", "none"], default="none")
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--release", action="store_true")
    parser.add_argument("--skip-git-clean-check", action="store_true")
    args = parser.parse_args()

    old_version = read_version()
    version = old_version

    if args.bump != "none":
        new_version = bump_semver(old_version, args.bump)
        write_version(new_version)
        version = new_version
        print(f"[i] Bumped version: {old_version} -> {new_version}")
        if not args.skip_git_clean_check and not git_is_clean():
            print("[!] Working tree sujo, vou commitar tudo.")
        git_commit_all(f"chore(release): bump version to v{version}")
        git_tag(f"v{version}")
        git_push_with_tags()
    else:
        print(f"[i] Using existing version: {version}")

    if args.build:
        print(f"[i] Building v{version}")
        build_pyinstaller()
        out = build_installer(version)
        if not wait_until_unlocked(out):
            print("[!] Aviso: instalador pode estar bloqueado. Prosseguindo…")
    else:
        print("[i] Skipping build step.")

    if args.release:
        asset_path = OUTDIR / f"CheckinSetup-v{version}.exe"
        if not asset_path.exists():
            raise SystemExit(f"Installer not found at {asset_path}")
        upload_url = create_or_get_release_upload_url(version)
        upload_asset(upload_url, asset_path)
        print("[✔] Release uploaded successfully.")
    else:
        print("[i] Skipping GitHub release step.")

    print(f"[DONE] v{version} process finished.")


if __name__ == "__main__":
    main()
