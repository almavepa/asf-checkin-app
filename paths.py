import os

def get_paths():
    from pathlib import Path

    # Base path for the app itself (read-only after packaging)
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

    # User data path — visible in Documents
    documents = str(Path.home() / "Documents")
    DATA_DIR = os.path.join(documents, "CheckinApp")

    return APP_DIR, DATA_DIR

def ensure_file(path, contents=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(contents)
