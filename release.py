# release.py
import os
import re
import subprocess
from pathlib import Path
import shutil
import time
from string import Template
from typing import Tuple

import requests

# ---- CONFIG ----
OWNER = "almavepa"
REPO  = "asf-checkin-app"
ICON = str((Path(__file__).parent / "checkin.ico").resolve())
OUTDIR = Path("dist")
ISS_PATH = Path("installer.iss")  # mantém exactamente este nome e local

TOKEN = os.getenv("GITHUB_TOKEN")  # defina no ambiente (recomendado)
TARGET_BRANCH = os.getenv("TARGET_BRANCH", "main")  # branch alvo para a release
BUMP_KIND = os.getenv("BUMP", "patch").lower()      # major | minor | patch (default)

# ---- Read version from version.py ----
VERSION_FILE = Path("version.py")
m = re.search(r'__version__\s*=\s*"([^"]+)"', VERSION_FILE.read_text(encoding="utf-8"))
if not m:
    raise SystemExit("Could not read __version__ from version.py")
VERSION = m.group(1)
INSTALLER_NAME = f"CheckinSetup-v{VERSION}.exe"


def run(cmd, **kwargs):
    print(">", " ".join(str(c) for c in cmd))
    subprocess.run(cmd, check=True, **kwargs)


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


# --------- BUMP VERSION ---------
_semver_re = re.compile(r'^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$')

def parse_semver(v: str) -> Tuple[int, int, int]:
    mm = _semver_re.match(v.strip())
    if not mm:
        raise SystemExit(f"Versão inválida em version.py: {v!r}. Esperado MAJOR.MINOR.PATCH")
    return int(mm.group(1)), int(mm.group(2)), int(mm.group(3))

def format_semver(maj:int, min_:int, pat:int) -> str:
    return f"{maj}.{min_}.{pat}"

def bump_version_string(v: str, kind: str) -> str:
    kind = kind.lower()
    maj, min_, pat = parse_semver(v)
    if kind == "major":
        return format_semver(maj+1, 0, 0)
    if kind == "minor":
        return format_semver(maj, min_+1, 0)
    if kind == "patch":
        return format_semver(maj, min_, pat+1)
    raise SystemExit(f"BUMP inválido: {kind}. Use major|minor|patch")

def write_version_py(new_version: str):
    txt = VERSION_FILE.read_text(encoding="utf-8")
    new_txt = re.sub(
        r'(__version__\s*=\s*")([^"]+)(")',
        r'\g<1>' + new_version + r'\g<3>',
        txt,
        count=1
    )
    VERSION_FILE.write_text(new_txt, encoding="utf-8")
    print(f"[✔] version.py atualizado para {new_version}")


def git_available():
    try:
        subprocess.run(["git", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return True
    except Exception:
        return False

def git_commit_and_push_version(new_version: str):
    if not git_available():
        raise SystemExit("git não encontrado. Instale e configure o git para continuar.")
    # Garantir que o ficheiro foi libertado por AV/Indexação
    wait_until_unlocked(VERSION_FILE, timeout=10.0)
    run(["git", "add", str(VERSION_FILE)])
    # Se não houver alterações, o commit falhará. Detectar.
    status = subprocess.run(["git", "diff", "--cached", "--quiet"])
    if status.returncode == 0:
        print("[i] Sem alterações em version.py para commitar.")
    else:
        run(["git", "commit", "-m", f"chore(release): bump version to v{new_version}"])
    run(["git", "push"])

    # Criar e enviar tag local (opcional mas útil)
    tag = f"v{new_version}"
    # Verifica se tag já existe localmente
    tag_list = subprocess.run(["git", "tag", "--list", tag], capture_output=True, text=True, check=True)
    if tag not in tag_list.stdout.splitlines():
        run(["git", "tag", tag])
    else:
        print(f"[i] Tag {tag} já existe localmente.")
    # Push da tag (se já existir remoto, será no-op/erro suave)
    try:
        run(["git", "push", "origin", tag])
    except subprocess.CalledProcessError:
        print(f"[!] Não foi possível enviar a tag {tag}. Prosseguir mesmo assim.")


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
        "--windowed",
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


def write_iss(use_icon: bool, version: str):
    OUTDIR.mkdir(parents=True, exist_ok=True)

    abs_dist = str(OUTDIR.resolve()).replace("/", "\\")
    app_exe  = str((OUTDIR / "CheckinApp.exe").resolve()).replace("/", "\\")
    upd_exe  = str((OUTDIR / "updater_install.exe").resolve()).replace("/", "\\")
    icon_win = str((Path(ICON).resolve())).replace("/", "\\")

    # Only include SetupIconFile if allowed
    icon_line = f"SetupIconFile={icon_win}" if use_icon else ""

    iss_tpl = Template(r"""
[Setup]
AppName=Checkin System
AppVersion=$VERSION
DefaultDirName={userappdata}\ASFormacao\Checkin
DefaultGroupName=ASFormacao\Checkin
OutputDir=$OUTDIR
OutputBaseFilename=CheckinSetup-v$VERSION
Compression=lzma2
SolidCompression=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
SetupLogging=yes
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
    ISS_PATH.write_text(iss_text, encoding="utf-8")


def build_installer(version: str):
    iscc_cmd = ensure_iscc_on_path()
    installer_name = f"CheckinSetup-v{version}.exe"

    # Delete old installer if present
    old_installer = OUTDIR / installer_name
    if old_installer.exists():
        try:
            old_installer.unlink()
            print(f"[i] Deleted old installer: {old_installer}")
        except Exception as e:
            print(f"[!] Could not delete old installer: {e}")

    # First try with icon
    try:
        write_iss(use_icon=True, version=version)
        run([iscc_cmd, str(ISS_PATH)])
    except subprocess.CalledProcessError:
        print("[!] ISCC failed (likely icon). Retrying without icon…")
        write_iss(use_icon=False, version=version)
        run([iscc_cmd, str(ISS_PATH)])

    # Verify installer was created
    if not (OUTDIR / installer_name).exists():
        raise SystemExit(f"[!] Installer not created in {OUTDIR}")
    else:
        print(f"[✔] Installer available at {OUTDIR / installer_name}")
    return installer_name


def get_or_create_release(version: str) -> str:
    """Devolve o upload_url (sem o sufixo {?name,label}) para a release vX.Y.Z.
       Cria a release se não existir. Reaproveita se já existir."""
    if not TOKEN:
        raise SystemExit("GITHUB_TOKEN not set in environment. Set it or store via your app's first-run prompt (this script needs it too).")

    rel_api = f"https://api.github.com/repos/{OWNER}/{REPO}/releases"
    headers = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"}

    # Tentar criar
    print(f"[i] Creating GitHub release v{version}…")
    r = requests.post(rel_api, headers=headers, json={
        "tag_name": f"v{version}",
        "name": f"Checkin v{version}",
        "draft": False,
        "prerelease": False,
        "target_commitish": TARGET_BRANCH,
    })

    if r.status_code == 201:
        upload_url = r.json()["upload_url"].split("{")[0]
        print("[✔] Release criada.")
        return upload_url

    # Se já existir (422), obter a release por tag
    if r.status_code == 422:
        print("[i] Release já existe. A obter upload_url…")
        gr = requests.get(f"{rel_api}/tags/v{version}", headers=headers)
        gr.raise_for_status()
        upload_url = gr.json()["upload_url"].split("{")[0]
        return upload_url

    # Outros erros
    r.raise_for_status()
    # fallback (não deverá chegar aqui)
    return ""


def delete_asset_if_exists(upload_url_base: str, asset_name: str):
    """Apaga o asset com o mesmo nome, se já existir, para evitar erro 422 no upload."""
    headers = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"}
    # A partir do upload_url, obter o recurso release para listar assets
    # upload_url_base é do tipo https://uploads.github.com/repos/.../releases/<id>/assets
    # O endpoint para ler a release com assets é em api.github.com/releases/<id>
    # Vamos inferir o release_id a partir do URL base (está imediatamente antes de '/assets')
    try:
        release_id = upload_url_base.rstrip("/").split("/")[-2]
        # Ler release e listar assets
        rel = requests.get(
            f"https://api.github.com/repos/{OWNER}/{REPO}/releases/{release_id}",
            headers=headers
        )
        rel.raise_for_status()
        assets = rel.json().get("assets", []) or []
        for a in assets:
            if a.get("name") == asset_name:
                asset_id = a.get("id")
                print(f"[i] Asset existente '{asset_name}' (id {asset_id}). A apagar…")
                dr = requests.delete(
                    f"https://api.github.com/repos/{OWNER}/{REPO}/releases/assets/{asset_id}",
                    headers=headers
                )
                # 204 esperado
                if dr.status_code in (200, 204):
                    print("[✔] Asset antigo removido.")
                else:
                    print(f"[!] Não foi possível remover asset antigo (status {dr.status_code}). Prosseguir…")
                break
    except Exception as e:
        print(f"[!] Falha ao tentar remover asset existente: {e}. Prosseguir…")


def create_release_and_upload(version: str, installer_filename: str):
    if not TOKEN:
        raise SystemExit("GITHUB_TOKEN not set in environment. Set it or store via your app's first-run prompt (this script needs it too).")

    upload_url = get_or_create_release(version)

    asset_path = OUTDIR / installer_filename
    if not asset_path.exists():
        raise SystemExit(f"Installer not found at {asset_path}")

    # Se já existir um asset com o mesmo nome, apagar
    delete_asset_if_exists(upload_url, installer_filename)

    print("[i] Uploading installer…")
    with asset_path.open("rb") as f:
        ur = requests.post(
            f"{upload_url}?name={installer_filename}",
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/octet-stream"},
            data=f
        )
    if ur.status_code not in (200, 201):
        print(ur.text)
    ur.raise_for_status()
    print("[✔] Release uploaded successfully.")


def main():
    global VERSION

    print(f"[i] Versão atual em version.py: {VERSION}")

    # 1) Bump de versão (antes de tudo)
    new_version = bump_version_string(VERSION, BUMP_KIND)
    print(f"[i] Bumping versão ({BUMP_KIND}) -> {new_version}")
    write_version_py(new_version)

    # 2) Commit + push do version.py (e tag)
    git_commit_and_push_version(new_version)

    # Atualizar variáveis dependentes da versão
    VERSION = new_version

    print(f"[i] Building v{VERSION}")
    # 3) Build executáveis
    build_pyinstaller()
    # 4) Build instalador (Inno Setup)
    installer_filename = build_installer(VERSION)

    # 5) Criar release e enviar asset
    create_release_and_upload(VERSION, installer_filename)

    print(f"[DONE] v{VERSION} built and published.")


if __name__ == "__main__":
    main()
