import shutil
import subprocess
from pathlib import Path

def build_pyinstaller():
    dist_dir = Path("dist")
    build_dir = Path("build")
    spec_file = Path("CheckinApp.spec")

    # Limpar dist antigo
    if dist_dir.exists():
        for f in dist_dir.iterdir():
            if f.is_file():
                f.unlink()
            elif f.is_dir():
                shutil.rmtree(f, ignore_errors=True)

    if build_dir.exists():
        shutil.rmtree(build_dir, ignore_errors=True)

    if spec_file.exists():
        spec_file.unlink()

    # Executar PyInstaller
    cmd = [
        "pyinstaller",
        "--noconfirm",
        "--onefolder",
        "--name", "CheckinApp",
        "main.py"
    ]
    subprocess.run(cmd, check=True)
