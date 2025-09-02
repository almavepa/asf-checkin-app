# add_student_cli.py
from __future__ import annotations
import os
import sys
from pathlib import Path

# 1) edita estes valores para o teu teste
STUDENT_NUMBER = 1234
FULL_NAME      = "Maria Silva"
EMAIL1         = "enc1@example.com"
EMAIL2         = "enc2@example.com"

# 2) onde estão os qrcodes
BASE_DIR   = Path.home() / "Documents" / "CheckinApp"
QRCODES_DIR = BASE_DIR / "qrcodes"

# 3) tenta usar o teu gerador; se não existir, gera localmente com 'qrcode'
def ensure_qr_png(student_number: int, full_name: str) -> Path:
    first, *rest = full_name.strip().split()
    last = rest[-1] if rest else ""
    safe_first = first.replace(" ", "_")
    safe_last  = last.replace(" ", "_")
    out = QRCODES_DIR / f"QR_{student_number}_{safe_first}_{safe_last}.png"
    QRCODES_DIR.mkdir(parents=True, exist_ok=True)

    # tenta usar o gerador da app (se existir)
    try:
        from generate_qr import gerar_qr_para_id  # se já tiveres esta função
        path_str = gerar_qr_para_id(str(student_number), full_name)  # deve devolver o caminho
        return Path(path_str)
    except Exception:
        pass

    # fallback: gerar com a biblioteca 'qrcode'
    try:
        import qrcode  # pip install qrcode[pil]
        img = qrcode.make(str(student_number))
        img.save(out)
        return out
    except Exception as e:
        print("[!] Falhou gerar QR automaticamente. Instala 'qrcode' com:")
        print("    py -m pip install qrcode[pil]")
        raise

def main():
    # garantir que db.py está disponível
    try:
        from db import upsert_student, save_qr_image
    except Exception as e:
        print("[!] Faltam utilitários da BD (db.py). Copia o db.py que te enviei para a pasta do projeto.")
        print(e)
        sys.exit(1)

    # 1) criar/atualizar aluno (texto)
    sid = upsert_student(
        number=int(STUDENT_NUMBER),
        name=FULL_NAME,
        email1=EMAIL1,
        email2=EMAIL2,
        qr_png=None,  # vamos guardar o BLOB já a seguir
    )
    print(f"[i] Student row id = {sid}")

    # 2) garantir/persistir PNG do QR e guardar o BLOB na BD
    qr_path = ensure_qr_png(int(STUDENT_NUMBER), FULL_NAME)
    with open(qr_path, "rb") as f:
        save_qr_image(int(STUDENT_NUMBER), f.read())

    print(f"[✔] Aluno {STUDENT_NUMBER} – {FULL_NAME} gravado e QR guardado na BD.")
    print(f"[ℹ] PNG em: {qr_path}")

if __name__ == "__main__":
    main()
