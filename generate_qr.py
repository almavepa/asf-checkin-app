# generate_qr.py — robusto contra settings ausentes/caminhos errados
from __future__ import annotations
import os, sys, json, re, base64, smtplib, mimetypes
from pathlib import Path
from typing import Any, Dict, Tuple, Optional
from email.message import EmailMessage

import qrcode
from qrcode.constants import ERROR_CORRECT_M

# -------------------- Defaults seguros --------------------
DEFAULTS: Dict[str, Any] = {
    "qr_box_size": 10,
    "qr_border": 4,
    "email_sender_name": "ASFormação",
    "school_name": "ASFormação",
    "min_seconds_between_reads": 3,
    "google_sheet_id": "",
    "sheet_name": "Sheet1",
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_user": "",
    "smtp_password": "",
    "from_email": "",
    "notify_guardians": False,
}

# -------------------- Helpers de paths --------------------
def _user_appdata_dir() -> Path:
    base = os.getenv("APPDATA")
    if not base:
        base = str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "ASFormacao" / "Checkin"

def _exe_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent

def _cwd_dir() -> Path:
    # interface.py faz os.chdir(DATA_DIR) no arranque
    try:
        return Path.cwd()
    except Exception:
        return _exe_dir()

def _settings_candidates() -> list[Path]:
    return [
        _user_appdata_dir() / "settings.json",
        _exe_dir() / "settings.json",
        _cwd_dir() / "settings.json",
    ]

# -------------------- Load settings com merges/fallback --------------------
def _load_settings() -> Tuple[Dict[str, Any], Optional[Path]]:
    for p in _settings_candidates():
        try:
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    # merge sem sobrepor se valor vier None
                    merged = {**DEFAULTS, **{k: v for k, v in data.items() if v is not None}}
                    return merged, p
        except Exception:
            pass
    return DEFAULTS.copy(), None

def _safe_int(cfg: Dict[str, Any], key: str, default: int) -> int:
    v = cfg.get(key, default)
    try:
        if v in ("", None):
            return default
        return int(v)
    except Exception:
        return default

# -------------------- QR core --------------------
def _qr_params() -> Tuple[int, int, Optional[Path], Dict[str, Any]]:
    cfg, src = _load_settings()
    box = max(1, min(40, _safe_int(cfg, "qr_box_size", DEFAULTS["qr_box_size"])))
    border = max(1, min(10, _safe_int(cfg, "qr_border", DEFAULTS["qr_border"])))
    return box, border, src, cfg

def _sanitize_filename(s: str) -> str:
    s = re.sub(r"\s+", "_", s.strip())
    s = re.sub(r"[^A-Za-z0-9_\-]", "", s)
    return s or "qr"

def _ensure_dirs() -> Tuple[Path, Path]:
    # DATA_DIR é o CWD por causa do interface.py
    data_dir = _cwd_dir()
    qrcodes = data_dir / "qrcodes"
    qrcodes.mkdir(parents=True, exist_ok=True)
    return data_dir, qrcodes

def gerar_qr_para_id(student_id: str, nome: str) -> str:
    """
    Gera um QR (conteúdo: '{student_id}') e devolve o caminho do PNG criado.
    Usa box_size/border a partir do settings.json (com defaults seguros).
    """
    box, border, src, cfg = _qr_params()
    content = f"{student_id}"

    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_M,
        box_size=box,
        border=border,
    )
    qr.add_data(content)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    data_dir, qrcodes_dir = _ensure_dirs()
    filename = f"QR_{_sanitize_filename(student_id)}_{_sanitize_filename(nome)}.png"
    out_path = qrcodes_dir / filename
    img.save(out_path)
    return str(out_path)

# -------------------- Email (no-op se sem SMTP) --------------------
def _send_email_with_attachment(
    smtp_host: str, smtp_port: int, smtp_user: str, smtp_password: str,
    sender_email: str, sender_name: str,
    to_email: str, subject: str, html_body: str,
    attachment_path: str
) -> None:
    msg = EmailMessage()
    msg["From"] = f"{sender_name} <{sender_email}>" if sender_name else sender_email
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content("Ver anexo em HTML.")
    msg.add_alternative(html_body, subtype="html")

    ctype, _ = mimetypes.guess_type(attachment_path)
    maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
    with open(attachment_path, "rb") as f:
        msg.add_attachment(f.read(), maintype=maintype, subtype=subtype, filename=Path(attachment_path).name)

    with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15) as server:
        
        if smtp_user:
            server.login(smtp_user, smtp_password or "")
        server.send_message(msg)

def enviar_qr_por_email(caminho_qr: str, nome: str, to_email: str | None = None) -> None:
    """
    Envia o QR por email, SE houver SMTP configurado.
    - Se não houver SMTP ou 'from_email', faz no-op (não levanta exceção).
    - 'to_email' é opcional: se não vier, tenta 'ALUNO_EMAIL' do ambiente (.env) ou no-op.
    """
    _, _, _, cfg = _qr_params()

    smtp_host = (cfg.get("smtp_host") or "").strip()
    smtp_port = _safe_int(cfg, "smtp_port", 587)
    smtp_user = (cfg.get("smtp_user") or "").strip()
    smtp_pass = (cfg.get("smtp_password") or "").strip()
    sender    = (cfg.get("from_email") or "").strip()
    sender_nm = (cfg.get("email_sender_name") or "ASFormação").strip()
    school    = (cfg.get("school_name") or "ASFormação").strip()

    # Sem SMTP configurado → no-op silencioso
    if not smtp_host or not sender:
        return

    # destinatário: parâmetro, variável de ambiente ou falha silenciosa
    to = (to_email or os.getenv("ALUNO_EMAIL") or "").strip()
    if not to:
        return

    # corpo do email: tenta ler email.html (DATA_DIR ou APP_DIR), caso contrário usa um default simples
    data_dir = _cwd_dir()
    app_dir  = _exe_dir()
    email_tpl_candidates = [
        data_dir / "email.html",
        app_dir / "email.html",
    ]
    html = None
    for p in email_tpl_candidates:
        if p.exists():
            try:
                html = p.read_text(encoding="utf-8")
                break
            except Exception:
                pass
    if html is None:
        html = f"""<!doctype html><html><body>
        <p>Olá {nome},</p>
        <p>Segue em anexo o teu QR de acesso ({school}).</p>
        </body></html>"""

    subject = f"{school} – QR de acesso"

    try:
        _send_email_with_attachment(
            smtp_host, smtp_port, smtp_user, smtp_pass,
            sender, sender_nm, to, subject, html, caminho_qr
        )
    except Exception:
        # não propaga falha de email (interface.py já lida com aviso)
        pass
