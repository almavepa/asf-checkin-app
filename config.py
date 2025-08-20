import os
import configparser
from pathlib import Path

# We’ll use Fernet for simple local encryption of the token.
# Install deps once: pip install cryptography
from cryptography.fernet import Fernet

APP_VENDOR = "ASFormacao"
APP_NAME = "asf-checkin-app"
DATA_DIR = Path(os.getenv("APPDATA", Path.home())) / APP_VENDOR / APP_NAME
DATA_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_PATH = DATA_DIR / "config.ini"
KEY_PATH = DATA_DIR / "key.key"

SECTION = "github"
FIELD = "token_enc"


def _get_or_create_key() -> bytes:
    if KEY_PATH.exists():
        return KEY_PATH.read_bytes()
    key = Fernet.generate_key()
    KEY_PATH.write_bytes(key)
    return key


def save_token_plain(token: str):
    """For rare debugging only (not used automatically)."""
    cfg = configparser.ConfigParser()
    if CONFIG_PATH.exists():
        cfg.read(CONFIG_PATH, encoding="utf-8")
    if SECTION not in cfg:
        cfg[SECTION] = {}
    cfg[SECTION]["token_plain"] = token
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        cfg.write(f)


def save_token(token: str):
    key = _get_or_create_key()
    f = Fernet(key)
    token_enc = f.encrypt(token.encode("utf-8")).decode("ascii")

    cfg = configparser.ConfigParser()
    if CONFIG_PATH.exists():
        cfg.read(CONFIG_PATH, encoding="utf-8")
    if SECTION not in cfg:
        cfg[SECTION] = {}
    cfg[SECTION][FIELD] = token_enc

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        cfg.write(f)


def load_token() -> str | None:
    if not CONFIG_PATH.exists():
        return None
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH, encoding="utf-8")
    if SECTION not in cfg or FIELD not in cfg[SECTION]:
        return None
    token_enc = cfg[SECTION][FIELD].strip()
    if not token_enc:
        return None
    key = _get_or_create_key()
    f = Fernet(key)
    try:
        token = f.decrypt(token_enc.encode("ascii")).decode("utf-8")
        return token
    except Exception:
        return None


def prompt_and_store_token() -> str | None:
    """Small GUI prompt (password-style) to capture the token on first run."""
    import tkinter as tk
    from tkinter import simpledialog, messagebox

    root = tk.Tk()
    root.withdraw()
    messagebox.showinfo(
        "GitHub Token Needed",
        "Please paste your GitHub Access Token.\n"
        "It is required to download updates from the private repository."
    )
    token = simpledialog.askstring(
        "GitHub Token",
        "Enter your GitHub Access Token:",
        show="*"
    )
    root.destroy()
    if token:
        token = token.strip()
        if token:
            save_token(token)
            return token
    return None
