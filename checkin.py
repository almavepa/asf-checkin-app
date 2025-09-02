# checkin.py
import os, sys, json, time, csv, logging, importlib.util
from datetime import datetime
import smtplib
from email.utils import formataddr
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from logging.handlers import RotatingFileHandler
# de:
from db import write_checkin
# para:
from db import log_event

from paths import get_paths, ensure_file

# --- NOVO: acesso à BD MariaDB ---
try:
    from db import write_checkin, db_available
except Exception:
    # fallback se db.py não existir ainda
    def write_checkin(*args, **kwargs):  # type: ignore
        pass
    def db_available() -> bool:  # type: ignore
        return False

# ---------------- paths & first-run ----------------
APP_DIR, DATA_DIR = get_paths()

EMAIL_HTML       = os.path.join(APP_DIR, "email.html")
FUNDO_IMG        = os.path.join(APP_DIR, "fundo.jpg")  # optional

STUDENTS_FILE    = os.path.join(DATA_DIR, "students.py")
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
ensure_file(STUDENTS_FILE, 'students = {"1001": ["Aluno Exemplo", "exemplo@mail.com", ""]}\n')
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

# ---------------- students loader ----------------
def _load_students_dict():
    try:
        spec = importlib.util.spec_from_file_location("students_data", STUDENTS_FILE)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore
        return dict(mod.students)
    except Exception as e:
        logger.error(f"Failed to load students.py from {STUDENTS_FILE}: {e}")
        return {}

STUDENTS = _load_students_dict()

def reload_students():
    global STUDENTS
    STUDENTS = _load_students_dict()
    logger.info("Reloaded students.py")

def get_emails(students_dict, sid):
    dados  = students_dict.get(sid, [])
    nome   = dados[0].strip() if len(dados) > 0 and dados[0] else ""
    email1 = dados[1].strip() if len(dados) > 1 and dados[1] else ""
    email2 = dados[2].strip() if len(dados) > 2 and dados[2] else ""
    return nome, email1, email2

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
def send_email(student_id, student_name, tipo, timestamp_str):
    try:
        with open(EMAIL_HTML, "r", encoding="utf-8") as file:
            html_template = file.read()
    except Exception as e:
        logger.error(f"Failed to read template {EMAIL_HTML}: {e}")
        html_template = "<p>{nome}: {tipo} às {hora}</p>"

    hora = timestamp_str[9:14] if len(timestamp_str) >= 14 else timestamp_str
    html_content = (
        html_template
        .replace("{{nome}}", student_name)
        .replace("{{tipo}}", tipo.lower())
        .replace("{{hora}}", hora)
    )

    _, email1, email2 = get_emails(STUDENTS, student_id)
    recipients = [e for e in (email1, email2) if e]
    if not recipients:
        logger.warning("No email ..."); return

    msg = MIMEMultipart()
    msg["From"] = formataddr(("ASFormação", SMTP_USER or ""))
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = f"Registo de {tipo} de {student_name}"
    msg.attach(MIMEText(html_content, "html"))

    last_err = None
    for attempt in range(1, 4):
        try:
            with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=15) as server:
                server.login(SMTP_USER, SMTP_PASS)
                server.sendmail(SMTP_USER, recipients, msg.as_string())
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
    info = STUDENTS.get(student_id, ["DESCONHECIDO", ""])
    student_name = info[0]
    cooldown = 10
    tipo = "Entrada"

    # toggle entrada/saída based on last scan
    if student_id in last_scan_times:
        prev = last_scan_times[student_id]
        secs = (ts - prev["last_scan"]).total_seconds()
        if secs < cooldown:
            logger.debug(f"Ignored; last scan {int(secs)}s ago.")
            return
        tipo = "Saída" if prev["last_tipo"] == "Entrada" else "Entrada"

    formatted = ts.strftime("%d-%m-%y %H:%M:%S")
    append_row_resilient([formatted, student_id, student_name, tipo])
    if LOCAL_CSV:
        append_local_record(student_id, student_name, tipo, ts)

    # --- NOVO: escrever também em MariaDB (se disponível) ---
    try:
        # student_id é string; na BD usamos número inteiro
        sid_num = int("".join(ch for ch in str(student_id) if ch.isdigit()))
        log_event(sid_num, tipo, DEVICE_NAME)
    except Exception as e:
        # nunca quebrar o fluxo se a BD falhar
        logger.warning(f"DB write skipped/failure: {e}")

    last_scan_times[student_id] = {"last_scan": ts, "last_tipo": tipo}
    save_scan_cache()

    logger.info(f"{tipo} registada: {student_name} ({student_id}) às {formatted}  in {time.time()-start:.3f}s")
    send_email(student_id, student_name, tipo, formatted)
    return student_name, tipo
