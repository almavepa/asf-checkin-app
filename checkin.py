# checkin.py
import os, sys, json, time, csv, logging
from datetime import datetime
import smtplib
from email.utils import formataddr
from email.message import EmailMessage  # << usar EmailMessage moderno
from dotenv import load_dotenv
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from logging.handlers import RotatingFileHandler

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
    today = datetime.now().date()
    for sid, info in list(last_scan_times.items()):
        if info["last_scan"].date() < today and info["last_tipo"] == "Entrada":
            last_scan_times[sid]["last_tipo"] = "Saída"
            logger.info(f"Forcing 'Saída' in memory for {sid} from previous day.")

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

def send_email_db(name: str, email1: str | None, email2: str | None, tipo: str, timestamp_str: str):
    recipients = [e for e in [(email1 or "").strip(), (email2 or "").strip()] if e]
    if not recipients:
        logger.info("No guardian emails in DB; skipping email.")
        return

    html_content = _build_email_html(name, tipo, timestamp_str)
    subject = f"Registo de {tipo} de {name}"

    # Construir mensagem com UTF-8 garantido
    msg = EmailMessage()
    # Texto simples de fallback (opcional)
    msg.set_content(f"{name}: {tipo} às {timestamp_str}")
    # Alternativa HTML (UTF-8 por defeito)
    msg.add_alternative(html_content, subtype="html")

    # Cabeçalhos (EmailMessage trata da codificação)
    # From pode ter nome com acentos; o servidor verá o envelope separado.
    from_display = "ASFormação"
    from_addr = SMTP_USER or ""
    msg["From"] = formataddr((from_display, from_addr))
    # To: só os emails (ASCII), para evitar problemas com nomes desconhecidos
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject

    last_err = None
    for attempt in range(1, 4):
        try:
            with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=15) as server:
                if SMTP_USER:
                    server.login(SMTP_USER, SMTP_PASS)
                # Enviar com envelope ASCII (só emails) e bytes para evitar ascii-encode
                server.sendmail(from_addr, recipients, msg.as_bytes())
            logger.info(f"Email sent to: {', '.join(recipients)}")
            return
        except Exception as e:
            last_err = e
            logger.warning(f"Email attempt {attempt} failed: {e}")
            time.sleep(min(2 ** attempt, 8))
    logger.error(f"Email failed after retries: {last_err}")

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
