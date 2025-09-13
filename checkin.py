# checkin.py
import io, contextlib, traceback
from email.headerregistry import Address
from email.policy import SMTP as SMTP_POLICY
import os, sys, json, time, csv, logging
from datetime import datetime
import smtplib
from email.utils import formataddr
from email.message import EmailMessage  # EmailMessage moderno
from email.headerregistry import Address  # <- robusto para nomes com acentos
from dotenv import load_dotenv
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from logging.handlers import RotatingFileHandler
from datetime import datetime, time
from db import write_checkin

# DB: agora usamos diretamente a BD para nome/emails e registos
from db import log_event, get_student_by_number

from paths import get_paths, ensure_file

# ---------------- consola "à prova de UTF-8" ----------------
try:
    if getattr(sys, "stdout", None):
        sys.stdout.reconfigure(encoding="utf-8")
    if getattr(sys, "stderr", None):
        sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    # Em versões antigas de Python ou ambientes sem reconfigure, ignorar
    pass

# ---------------- paths & first-run ----------------
APP_DIR, DATA_DIR = get_paths()

EMAIL_HTML       = os.path.join(APP_DIR, "email.html")
FUNDO_IMG        = os.path.join(APP_DIR, "fundo.jpg")  # optional

# (REMOVIDO) STUDENTS_FILE: deixamos de usar students.py
REGISTOS_DIR     = os.path.join(DATA_DIR, "registos")
QRCODES_DIR      = os.path.join(DATA_DIR, "qrcodes")
LOG_DIR          = os.path.join(DATA_DIR, "logs")
CACHE_FILE       = os.path.join(DATA_DIR, "scan_cache.json")
PENDING_FILE     = os.path.join(DATA_DIR, "pending_rows.json")
ENV_FILE         = os.path.join(DATA_DIR, ".env")
CREDENTIALS_JSON = os.path.join(DATA_DIR, "credentials.json")

os.makedirs(REGISTOS_DIR, exist_ok=True)
os.makedirs(QRCODES_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

# seed minimal files on first run
# (REMOVIDO) ensure_file de students.py
ensure_file(ENV_FILE, "SMTP_SERVER=\nSMTP_PORT=465\nSMTP_USER=\nSMTP_PASS=\nSCANNER_PORT=COM3\nSCANNER_BAUD=9600\n")

# ---------------- logging ----------------
logger = logging.getLogger("app")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    _h = RotatingFileHandler(os.path.join(LOG_DIR, "app.log"), maxBytes=500_000, backupCount=3, encoding="utf-8")
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(_h)

# ---------------- env ----------------
load_dotenv(ENV_FILE)  # << use %APPDATA%/.env
SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT   = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER   = os.getenv("SMTP_USER")
SMTP_PASS   = os.getenv("SMTP_PASS")
LOCAL_CSV   = os.getenv("LOCAL_CSV", "1").lower() in ("1", "true", "yes")
DEVICE_NAME = os.getenv("MACHINE_NAME", None)  # Rececao / Piso 0
MIN_COOLDOWN = int(os.getenv("MIN_SECONDS_BETWEEN_READS", "10") or "10")

# ---------------- Google Sheets ----------------
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
sheet = None
try:
    if os.path.exists(CREDENTIALS_JSON):
        CREDS = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_JSON, SCOPE)
        client = gspread.authorize(CREDS)
        sheet = client.open("Registo de entradas - versão de teste").sheet1
    else:
        logger.warning(f"credentials.json not found at {CREDENTIALS_JSON}; Sheets disabled.")
except Exception as e:
    logger.error(f"Sheets auth failed: {e}")
    sheet = None

# ---------------- cache (entrada/saída state) ----------------
last_scan_times = {}

def load_scan_cache():
    global last_scan_times
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for sid, info in data.items():
                last_scan_times[sid] = {
                    "last_scan": datetime.strptime(info["last_scan"], "%Y-%m-%d %H:%M:%S"),
                    "last_tipo": info["last_tipo"],
                }
            logger.debug(f"Cache loaded: {last_scan_times}")
        except Exception as e:
            logger.error(f"Failed to read cache {CACHE_FILE}: {e}")

def save_scan_cache():
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            data = {
                sid: {
                    "last_scan": info["last_scan"].strftime("%Y-%m-%d %H:%M:%S"),
                    "last_tipo": info["last_tipo"],
                }
                for sid, info in last_scan_times.items()
            }
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.debug(f"Cache saved.")
    except Exception as e:
        logger.error(f"Failed to write cache {CACHE_FILE}: {e}")



def reset_unfinished_entries():
    """Para cada aluno que ficou com 'Entrada' em dias anteriores,
    regista uma 'Saída' real na BD às 23:59 desse dia e atualiza em memória.
    """
    today = datetime.now().date()
    for sid, info in list(last_scan_times.items()):
        if info["last_scan"].date() < today and info["last_tipo"] == "Entrada":
            # Força saída às 23:59 do dia da última entrada
            ts_saida = datetime.combine(info["last_scan"].date(), time(23, 59, 0))
            try:
                write_checkin(int(sid), "", "Saída", ts=ts_saida)
                last_scan_times[sid]["last_tipo"] = "Saída"
                logger.info(f"Forçada 'Saída' na BD e em memória para {sid} ({ts_saida}).")
            except Exception as e:
                logger.error(f"Falhou forçar 'Saída' para {sid}: {e}")

# ---------------- local CSV mirror ----------------
def _ensure_day_csv(ts: datetime) -> str:
    os.makedirs(REGISTOS_DIR, exist_ok=True)
    path = os.path.join(REGISTOS_DIR, f"registo_{ts.date()}.csv")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8", newline="") as f:
            csv.writer(f).writerow(["ID", "Nome", "Data", "Hora", "Ação"])
    return path

def append_local_record(student_id: str, student_name: str, tipo: str, ts: datetime) -> None:
    path = _ensure_day_csv(ts)
    with open(path, "a", encoding="utf-8", newline="") as f:
        csv.writer(f).writerow([student_id, student_name, str(ts.date()), ts.strftime("%H:%M:%S"), tipo])

# ---------------- pending rows (when offline) ----------------
def _load_pending():
    if not os.path.exists(PENDING_FILE):
        return []
    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to read {PENDING_FILE}: {e}")
        return []

def _save_pending(rows):
    try:
        with open(PENDING_FILE, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Failed to write {PENDING_FILE}: {e}")

def append_row_resilient(row):
    if sheet is None:
        pending = _load_pending(); pending.append(row); _save_pending(pending)
        logger.info("Sheets disabled/offline; buffered row.")
        return False
    try:
        sheet.append_row(row)
        logger.info(f"Sheet append OK: {row}")
        flush_pending_rows()
        return True
    except Exception as e:
        logger.error(f"Sheet append failed; buffering: {e}")
        pending = _load_pending(); pending.append(row); _save_pending(pending)
        return False

def flush_pending_rows():
    pending = _load_pending()
    if not pending or sheet is None:
        return
    still = []
    for row in pending:
        try:
            sheet.append_row(row)
        except Exception as e:
            logger.warning(f"Flush failed for {row}: {e}")
            still.append(row)
    _save_pending(still)

# ---------------- email ----------------
def _load_email_template() -> str:
    try:
        with open(EMAIL_HTML, "r", encoding="utf-8") as file:
            return file.read()
    except Exception as e:
        logger.error(f"Failed to read template {EMAIL_HTML}: {e}")
        return "<p>{nome}: {tipo} às {hora}</p>"

def _build_email_html(name: str, tipo: str, timestamp_str: str) -> str:
    html_template = _load_email_template()
    hora = timestamp_str[9:14] if len(timestamp_str) >= 14 else timestamp_str
    return (
        html_template
        .replace("{{nome}}", name)
        .replace("{{tipo}}", tipo.lower())
        .replace("{{hora}}", hora)
    )

def _address_from_display_email(display_name: str, email_addr: str) -> Address:
    """Cria um Address que codifica corretamente nomes com acentos no header."""
    local, _, domain = (email_addr or "").partition("@")
    return Address(display_name=display_name or "", username=local, domain=domain)

import io, contextlib, traceback
from email.headerregistry import Address
from email.policy import SMTP as SMTP_POLICY

def _address_from_display_email(display_name: str, email_addr: str) -> Address:
    local, _, domain = (email_addr or "").partition("@")
    return Address(display_name=display_name or "", username=local, domain=domain)

def _smtp_debug_to_logger(smtp_obj, logger):
    """
    Redireciona o debug do smtplib para o logger (nível DEBUG) usando um buffer.
    Usar com redirect_stdout no bloco em que se faz EHLO/STARTTLS/send.
    """
    smtp_obj.set_debuglevel(1)
    buf = io.StringIO()
    return buf, contextlib.redirect_stdout(buf), lambda: logger.debug("SMTP DEBUG:\n%s", buf.getvalue())

def send_email_db(name: str, email1: str | None, email2: str | None,
                  tipo: str, timestamp_str: str):
    import socket, ssl, smtplib

    recipients = [e for e in [(email1 or "").strip(), (email2 or "").strip()] if e]
    if not recipients:
        logger.warning("Email: sem destinatários (email1=%r, email2=%r) — a ignorar envio.",
                       email1, email2)
        return

    html_content = _build_email_html(name, tipo, timestamp_str)
    subject = f"Registo de {tipo} de {name}"

    msg = EmailMessage()
    msg.set_content(f"{name}: {tipo} às {timestamp_str}")
    msg.add_alternative(html_content, subtype="html")
    from_display = "ASFormação"
    from_addr = SMTP_USER or ""
    msg["From"] = formataddr((from_display, from_addr))
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject

    # Resolver IPv4/IPv6
    family = socket.AF_INET if os.getenv("SMTP_FORCE_IPV4", "0") in ("1", "true", "yes") else socket.AF_UNSPEC
    try:
        infos = socket.getaddrinfo(SMTP_SERVER, SMTP_PORT, family, socket.SOCK_STREAM)
    except Exception as e:
        logger.error("SMTP: falha a resolver %s:%s (%s)", SMTP_SERVER, SMTP_PORT, e)
        return

    infos.sort(key=lambda x: 0 if x[0] == socket.AF_INET else 1)  # IPv4 primeiro
    TIMEOUT = int(os.getenv("SMTP_TIMEOUT", "60") or "60")
    context = ssl.create_default_context()

    last_err = None
    for af, socktype, proto, _, sockaddr in infos:
        ip = sockaddr[0]
        ipver = "IPv4" if af == socket.AF_INET else "IPv6"
        try:
            logger.info("SMTP: tentar %s %s:%s (%s)", SMTP_SERVER, ip, SMTP_PORT, ipver)

            with socket.create_connection((ip, SMTP_PORT), timeout=TIMEOUT) as raw:
                with context.wrap_socket(raw, server_hostname=SMTP_SERVER) as tls_sock:
                    server = smtplib.SMTP_SSL()
                    #server.set_debuglevel(1)
                    try:
                        server.sock = tls_sock
                        server.file = server.sock.makefile("rb")

                        code, banner = server.getreply()
                        if code != 220:
                            raise smtplib.SMTPResponseException(code, banner)

                        server.ehlo("asf-checkin")  # hostname neutro
                        if SMTP_USER:
                            server.login(SMTP_USER, SMTP_PASS)

                        mail_opts = []
                        if server.has_extn('smtputf8'):
                            mail_opts.append('SMTPUTF8')

                        server.send_message(msg, mail_options=mail_opts)

                    finally:
                        try:
                            server.quit()
                        except Exception:
                            server.close()

            logger.info("Email: enviado OK | to=%s | subj=%r | via %s %s:%s",
                        ", ".join(recipients), subject, ipver, ip, SMTP_PORT)
            return
        except Exception as e:
            last_err = e
            logger.error("SMTP: falha via %s %s:%s | err=%r", ipver, ip, SMTP_PORT, e)

    logger.error("Email: falhou em todos os IPs | último erro: %r", last_err)





# ---------------- main check-in API ----------------
def log_checkin(student_id):
    start = time.time()
    ts = datetime.now()
    cooldown = MIN_COOLDOWN
    tipo = "Entrada"

    # toggle entrada/saída based on last scan
    if student_id in last_scan_times:
        prev = last_scan_times[student_id]
        secs = (ts - prev["last_scan"]).total_seconds()
        if secs < cooldown:
            logger.debug(f"Ignored; last scan {int(secs)}s ago.")
            return
        tipo = "Saída" if prev["last_tipo"] == "Entrada" else "Entrada"

    # Extrair número para a BD
    digits = "".join(ch for ch in str(student_id) if ch.isdigit())
    if not digits:
        logger.warning(f"QR inválido (sem dígitos): {student_id!r}")
        return
    sid_num = int(digits)

    # Buscar aluno na BD (NÃO criar)
    try:
        row = get_student_by_number(sid_num)  # esperado: dict com keys name, email1, email2
    except Exception as e:
        logger.error(f"DB read failed for student {sid_num}: {e}")
        return

    if not row:
        logger.info(f"Unknown QR (not in DB): {student_id} (num={sid_num})")
        return  # UI deve mostrar "QR não reconhecido na base de dados"

    student_name = (row.get("name") or f"Aluno {sid_num}") if isinstance(row, dict) else f"Aluno {sid_num}"
    email1 = row.get("email1") if isinstance(row, dict) else None
    email2 = row.get("email2") if isinstance(row, dict) else None

    # 1) MariaDB primeiro (fonte principal)
    try:
        log_event(sid_num, tipo, DEVICE_NAME)
    except Exception as e:
        # não quebrar — Sheets é backup, mas sem DB não há registo "oficial"
        logger.warning(f"DB write skipped/failure: {e}")

    # 2) Backup para Sheets
    formatted = ts.strftime("%d-%m-%y %H:%M:%S")
    append_row_resilient([formatted, student_id, student_name, tipo])

    # 3) (Opcional) CSV espelho
    if LOCAL_CSV:
        append_local_record(student_id, student_name, tipo, ts)

    # Cache + email
    last_scan_times[student_id] = {"last_scan": ts, "last_tipo": tipo}
    save_scan_cache()

    logger.info(f"{tipo} registada: {student_name} ({student_id}) às {formatted}  in {time.time()-start:.3f}s")
    try:
        send_email_db(student_name, email1, email2, tipo, formatted)
    except Exception as e:
        logger.warning(f"Email send failed: {e}")

    return student_name, tipo
