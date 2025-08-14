# gerar_qr.py
import os
import re
import qrcode
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders
from dotenv import load_dotenv
from paths import get_paths 


# --- Dados dos alunos ---
# Espera-se um ficheiro students.py com um dicionário:
# students = { "1001": ["Maria Silva", "maria@ex.com", "maria2@ex.com"] }
from students import students

# =========================
#   CONFIGURAÇÃO DE EMAIL
# =========================
# --- Carregar variáveis do .env ---
load_dotenv()

SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = int(os.getenv("SMTP_PORT"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
DESTINATARIO_FIXO = "alice@asformacao.com"   # <- email para onde vais enviar
ENVIAR_EMAIL = True                         # põe False se quiseres só gerar os QR sem enviar
APP_DIR, DATA_DIR = get_paths()
# Pasta de saída dos QR

PASTA_QR = os.path.join(DATA_DIR, "qrcodes")
os.makedirs(PASTA_QR, exist_ok=True)

import os
from generate_qr import PASTA_QR
print("QRs go to:", os.path.abspath(PASTA_QR))

# =========================
#     FUNÇÕES AUXILIARES
# =========================
def _sanitize_filename(texto: str) -> str:
    # troca espaços por "_" e remove caracteres problemáticos
    texto = texto.strip().replace(" ", "_")
    return re.sub(r"[^A-Za-z0-9_\-\.]", "", texto)

def gerar_qr_para_id(student_id: str, nome: str) -> str:
    """Gera o QR para um aluno e devolve o caminho do ficheiro PNG."""
    os.makedirs(PASTA_QR, exist_ok=True)
    conteudo_qr = student_id  # só o ID
    img = qrcode.make(conteudo_qr)
    filename = f"{PASTA_QR}/{_sanitize_filename(student_id)}_{_sanitize_filename(nome)}.png"
    img.save(filename)
    return filename

def enviar_qr_por_email(caminho_qr: str, nome_aluno: str) -> None:
    """Envia o ficheiro de QR em anexo para o destinatário fixo."""
    if not os.path.exists(caminho_qr):
        raise FileNotFoundError(f"Ficheiro não encontrado: {caminho_qr}")

    msg = MIMEMultipart()
    msg["From"] = SMTP_USER
    msg["To"] = DESTINATARIO_FIXO
    msg["Subject"] = f"Código QR de {nome_aluno}"

    corpo = (
        f"Olá,\n\n"
        f"Segue em anexo o código QR do(a) aluno(a) {nome_aluno}.\n\n"
        f"Cumps."
    )
    msg.attach(MIMEText(corpo, "plain"))

    with open(caminho_qr, "rb") as f:
        parte = MIMEBase("application", "octet-stream")
        parte.set_payload(f.read())
        encoders.encode_base64(parte)
        parte.add_header(
            "Content-Disposition",
            f'attachment; filename="{os.path.basename(caminho_qr)}"'
        )
        msg.attach(parte)

    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=5) as server:
        #server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)

def gerar_qr_para_todos(enviar_email: bool = ENVIAR_EMAIL):
    os.makedirs(PASTA_QR, exist_ok=True)
    print(f"[i] Pasta '{PASTA_QR}' pronta.")



    total = len(students)
    ok_qr, ok_mail = 0, 0

    for student_id, dados in students.items():
        # suporta registos com 2 ou 3 campos
        if len(dados) >= 1:
            nome = dados[0]
        else:
            print(f"[!] ID {student_id} sem nome — a ignorar.")
            continue

        try:
            caminho = gerar_qr_para_id(student_id, nome)
            ok_qr += 1
            print(f"[✓] QR gerado para {nome} → {caminho}")

            if enviar_email:
                try:
                    enviar_qr_por_email(caminho, nome)
                    ok_mail += 1
                    print(f"[✉] Email enviado para {DESTINATARIO_FIXO} ({nome})")
                except Exception as e:
                    print(f"[!] Falha a enviar email ({nome}): {e}")

        except Exception as e:
            print(f"[!] Erro com {nome} (ID {student_id}): {e}")

    print(f"\n--- Resumo ---\nAlunos: {total}\nQR gerados: {ok_qr}\nEmails enviados: {ok_mail if enviar_email else '— (desativado)'}")

# =========================
#       EXECUÇÃO
# =========================
if __name__ == "__main__":
    gerar_qr_para_todos()
