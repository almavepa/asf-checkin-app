# notifier.py
import os, sys, time, threading, smtplib, ssl
from email.message import EmailMessage
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.expanduser("~"), "Documents", "CheckinApp", ".env"))

SMTP_SERVER = os.getenv("SMTP_SERVER", "")
SMTP_PORT   = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER   = os.getenv("SMTP_USER", "")
SMTP_PASS   = os.getenv("SMTP_PASS", "")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "alice@asformacao.pt")

RECIPIENTS = ["alice@asformacao.com"]

def _send(subject: str, body: str):
    if not SMTP_SERVER:
        print("[!] SMTP_SERVER não definido no .env")
        return
    try:
        msg = EmailMessage()
        msg["From"] = SMTP_USER
        msg["To"] = ", ".join(RECIPIENTS)
        msg["Subject"] = subject
        msg.set_content(body)

        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, context=ctx) as server:
            if SMTP_USER:
                server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        print(f"[✔] Email enviado: {subject}")
    except Exception as e:
        print(f"[!] Falha ao enviar email: {e}")

def notify_startup():
    _send("CheckinApp iniciada", "A aplicação foi aberta.")

def notify_shutdown():
    _send("CheckinApp encerrada", "A aplicação foi fechada.")

def notify_error(exc_type, exc_value, tb):
    import traceback
    body = "".join(traceback.format_exception(exc_type, exc_value, tb))
    _send("ERRO crítico no CheckinApp", body)

# ---- Monitor do scanner ----
_last_scanner_ok = time.time()
def mark_scanner_ok():
    global _last_scanner_ok, _is_down
    _last_scanner_ok = time.time()

def _scanner_watchdog():
    global _last_scanner_ok, _is_down, _last_alert_epoch
    while True:
        if time.time() - _last_scanner_ok > 600:  # 10 min
            _send("Scanner offline", "O scanner está desconectado há mais de 10 minutos.")
            # evitar spam: só 1 email por período
            _last_scanner_ok = time.time()
        time.sleep(60)

def start_scanner_watchdog():
    t = threading.Thread(target=_scanner_watchdog, daemon=True)
    t.start()

# -------------------------------------------------
# Scanner events (log only, no spam)
# -------------------------------------------------

_scanner_reopen_count = 0


def notify_scanner_error(err: str):
    """
    Erro real do scanner (exceção no serial).
    Deve ser raro e importante.
    """
    try:
        log("SCANNER_ERROR", err)
    except Exception:
        pass


def notify_scanner_reopen():
    """
    Reabertura automática (manutenção preventiva).
    Log agregado para não criar ruído.
    """
    global _scanner_reopen_count
    _scanner_reopen_count += 1

    # só loga de 6 em 6 (≈ 1h se reopen=10min)
    if _scanner_reopen_count % 6 == 0:
        try:
            log(
                "SCANNER_REOPEN",
                f"Scanner reaberto automaticamente {_scanner_reopen_count} vezes"
            )
        except Exception:
            pass


def notify_scanner_recovered(port: str):
    """
    Scanner voltou a funcionar após erro.
    """
    try:
        log("SCANNER_OK", f"Scanner reconectado em {port}")
    except Exception:
        pass
