# db.py — MariaDB helpers alinhados com o teu esquema
from __future__ import annotations
import os
from pathlib import Path
from typing import Optional
import pymysql
from dotenv import load_dotenv

def _get_data_dir() -> Path:
    try:
        from paths import get_paths
        _, data_dir = get_paths()
        return Path(data_dir)
    except Exception:
        return Path.home() / "Documents" / "CheckinApp"

DATA_DIR = _get_data_dir()
ENV_FILE = DATA_DIR / ".env"
if ENV_FILE.exists():
    load_dotenv(ENV_FILE)

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "checkin_user")
DB_PASS = os.getenv("DB_PASSWORD", "checkin_pass")
DB_NAME = os.getenv("DB_NAME", "checkin_db")
DEVICE  = os.getenv("MACHINE_NAME", None)  # Rececao / Piso 0

def _connect():
    return pymysql.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASS,
        database=DB_NAME, charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )

# ---------- STUDENTS ----------
def upsert_student(student_number: int, name: str,
                   email1: str | None = None, email2: str | None = None,
                   qr_png: bytes | None = None) -> int:
    """
    Insere/atualiza aluno na tabela students:
      columns: student_number (UNIQUE), name, email1, email2, qr_code (LONGBLOB), status (ENUM)
    Devolve students.id
    """
    with _connect() as conn, conn.cursor() as cur:
        # tenta obter id atual
        cur.execute("SELECT id FROM students WHERE student_number=%s", (student_number,))
        row = cur.fetchone()
        if row is None:
            # INSERT
            cols = ["student_number", "name"]
            vals = [student_number, name or f"Aluno {student_number}"]
            if email1 is not None: cols.append("email1"); vals.append(email1)
            if email2 is not None: cols.append("email2"); vals.append(email2)
            # qr_code é opcional — só guarda se vier bytes e a coluna existir
            cur.execute(
                "SELECT COUNT(*) AS c FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='students' AND COLUMN_NAME='qr_code'",
                (DB_NAME,)
            )
            if (cur.fetchone() or {}).get("c", 0) > 0 and qr_png is not None:
                cols.append("qr_code"); vals.append(qr_png)
            # status default 'Saída' se existir a coluna
            cur.execute(
                "SELECT COUNT(*) AS c FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='students' AND COLUMN_NAME='status'",
                (DB_NAME,)
            )
            if (cur.fetchone() or {}).get("c", 0) > 0:
                cols.append("status"); vals.append("Saída")

            placeholders = ", ".join(["%s"] * len(vals))
            cur.execute(f"INSERT INTO students ({', '.join(cols)}) VALUES ({placeholders})", vals)
            # obter id
            cur.execute("SELECT id FROM students WHERE student_number=%s", (student_number,))
            rid = cur.fetchone()
            return int(rid["id"]) if rid else 0
        else:
            # UPDATE
            sets, vals = ["name=%s"], [name]
            if email1 is not None: sets.append("email1=%s"); vals.append(email1)
            if email2 is not None: sets.append("email2=%s"); vals.append(email2)
            if qr_png is not None:
                # só atualiza se a coluna existir
                cur.execute(
                    "SELECT COUNT(*) AS c FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='students' AND COLUMN_NAME='qr_code'",
                    (DB_NAME,)
                )
                if (cur.fetchone() or {}).get("c", 0) > 0:
                    sets.append("qr_code=%s"); vals.append(qr_png)
            vals.append(student_number)
            cur.execute(f"UPDATE students SET {', '.join(sets)} WHERE student_number=%s", vals)
            return int(row["id"])

def save_qr_image(student_number: int, qr_png: bytes) -> None:
    """Atualiza o BLOB do QR (se a coluna existir)."""
    if not qr_png:
        return
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS c FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='students' AND COLUMN_NAME='qr_code'",
            (DB_NAME,)
        )
        if (cur.fetchone() or {}).get("c", 0) == 0:
            return
        cur.execute(
            "UPDATE students SET qr_code=%s WHERE student_number=%s",
            (qr_png, student_number)
        )

def get_student_by_number(student_number: int) -> dict | None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM students WHERE student_number=%s", (student_number,))
        return cur.fetchone()

# ---------- CHECKINS ----------
def log_event(student_number: int, action: str, device_name: str | None = None) -> None:
    """
    Escreve no histórico (checkins) e atualiza o 'status' em students se a coluna existir.
    checkins columns: id, student_id (FK students.id), timestamp (DATETIME), action (ENUM), device_name (opcional)
    """
    with _connect() as conn, conn.cursor() as cur:
        # obter students.id — sem criar automaticamente
        cur.execute("SELECT id FROM students WHERE student_number=%s", (student_number,))
        r = cur.fetchone()
        if r is None:
            raise ValueError(f"Aluno {student_number} não existe na BD")

        sid = int(r["id"])

        # inserir em checkins
        # saber se device_name existe
        cur.execute(
            "SELECT COUNT(*) AS c FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='checkins' AND COLUMN_NAME='device_name'",
            (DB_NAME,)
        )
        has_device = (cur.fetchone() or {}).get("c", 0) > 0

        if has_device:
            cur.execute(
                "INSERT INTO checkins (student_id, timestamp, action, device_name) VALUES (%s, NOW(), %s, %s)",
                (sid, action, device_name or DEVICE)
            )
        else:
            cur.execute(
                "INSERT INTO checkins (student_id, timestamp, action) VALUES (%s, NOW(), %s)",
                (sid, action)
            )

        # atualizar estado em students (se existir)
        cur.execute(
            "SELECT COUNT(*) AS c FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='students' AND COLUMN_NAME='status'",
            (DB_NAME,)
        )
        if (cur.fetchone() or {}).get("c", 0) > 0:
            cur.execute("UPDATE students SET status=%s WHERE id=%s", (action, sid))

# ---------- LISTAGEM (REGISTOS DE HOJE) ----------
def fetch_today_checkins():
    """
    Devolve os registos de hoje como lista de dicts:
      [{
        'timestamp': datetime, 'name': str, 'student_number': int,
        'action': 'Entrada'|'Saída', 'device_name': str|None
      }, ...]
    """
    with _connect() as conn, conn.cursor() as cur:
        # detetar se checkins.device_name existe
        cur.execute(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='checkins'", (DB_NAME,)
        )
        cols = {row["COLUMN_NAME"] for row in cur.fetchall()}

        select_fields = "c.timestamp, s.name, s.student_number, c.action"
        if "device_name" in cols:
            select_fields += ", c.device_name"
        else:
            select_fields += ", NULL AS device_name"

        # USAR INTERVALO (sargable) PARA APANHAR "HOJE" COM ÍNDICE
        sql = f"""
            SELECT {select_fields}
            FROM checkins c
            JOIN students s ON s.id = c.student_id
            WHERE c.timestamp >= CURRENT_DATE()
              AND c.timestamp <  CURRENT_DATE() + INTERVAL 1 DAY
            ORDER BY c.timestamp DESC
        """
        cur.execute(sql)
        return cur.fetchall()

# --- Compat: write_checkin delega para log_event (aceita timestamp opcional) ---
def write_checkin(student_number: int, student_name: str, action: str, ts=None, device_name: str | None = None) -> None:
    # se vier timestamp, usa-o; senão, NOW()
    if ts is None:
        # manter política: não criar aluno automaticamente
        return log_event(student_number, action, device_name)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM students WHERE student_number=%s", (student_number,))
        r = cur.fetchone()
        if r is None:
            raise ValueError(f"Aluno {student_number} não existe na BD")
        sid = int(r["id"])

        # device_name opcional
        cur.execute(
            "SELECT COUNT(*) AS c FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='checkins' AND COLUMN_NAME='device_name'", (DB_NAME,)
        )
        has_device = (cur.fetchone() or {}).get("c", 0) > 0
        if has_device:
            cur.execute(
                "INSERT INTO checkins (student_id, timestamp, action, device_name) VALUES (%s, %s, %s, %s)",
                (sid, ts, action, device_name or DEVICE)
            )
        else:
            cur.execute(
                "INSERT INTO checkins (student_id, timestamp, action) VALUES (%s, %s, %s)",
                (sid, ts, action)
            )
        # atualizar status se existir
        cur.execute(
            "SELECT COUNT(*) AS c FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='students' AND COLUMN_NAME='status'", (DB_NAME,)
        )
        if (cur.fetchone() or {}).get("c", 0) > 0:
            cur.execute("UPDATE students SET status=%s WHERE id=%s", (action, sid))

# ---------- LISTA/EDIÇÃO/REMOÇÃO DE ALUNOS ----------
def fetch_all_students(query: str | None = None, limit: int = 1000, offset: int = 0):
    """
    Devolve lista de alunos a partir da BD.
    Campos devolvidos garantidos: id, student_number, name, email1, email2 (email* pode vir None se não existir a coluna).
    """
    with _connect() as conn, conn.cursor() as cur:
        # ver colunas disponíveis
        cur.execute(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='students'", (DB_NAME,)
        )
        cols = {row["COLUMN_NAME"] for row in cur.fetchall()}
        # SELECT dinâmico conforme há email1/email2
        select_fields = ["id", "student_number", "name"]
        if "email1" in cols: select_fields.append("email1")
        else:                select_fields.append("NULL AS email1")
        if "email2" in cols: select_fields.append("email2")
        else:                select_fields.append("NULL AS email2")
        sql = f"SELECT {', '.join(select_fields)} FROM students"
        params = []
        if query:
            q = f"%{query}%"
            where = []
            where.append("CAST(student_number AS CHAR) LIKE %s")
            where.append("name LIKE %s")
            if "email1" in cols: where.append("email1 LIKE %s")
            if "email2" in cols: where.append("email2 LIKE %s")
            sql += " WHERE " + " OR ".join(where)
            params = [q, q]
            if "email1" in cols: params.append(q)
            if "email2" in cols: params.append(q)
        sql += " ORDER BY name ASC, student_number ASC LIMIT %s OFFSET %s"
        params += [limit, offset]
        cur.execute(sql, params)
        return cur.fetchall()

def update_student_fields(student_number: int,
                          name: str | None = None,
                          email1: str | None = None,
                          email2: str | None = None) -> int:
    """
    Atualiza campos do aluno (por student_number). Só atualiza os campos não-None e que existirem na tabela.
    Devolve número de linhas afetadas.
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='students'", (DB_NAME,)
        )
        cols = {row["COLUMN_NAME"] for row in cur.fetchall()}
        sets, vals = [], []
        if name is not None and "name" in cols:
            sets.append("name=%s"); vals.append(name)
        if email1 is not None and "email1" in cols:
            sets.append("email1=%s"); vals.append(email1)
        if email2 is not None and "email2" in cols:
            sets.append("email2=%s"); vals.append(email2)
        if not sets:
            return 0
        vals.append(student_number)
        cur.execute(f"UPDATE students SET {', '.join(sets)} WHERE student_number=%s", vals)
        return cur.rowcount

def delete_student(student_number: int) -> int:
    """
    Apaga o aluno e (se necessário) os seus registos de checkins.
    Se houver FK com ON DELETE CASCADE, a remoção de checkins é automática; caso contrário, removemos manualmente.
    Devolve número de linhas removidas na tabela students (0 ou 1).
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM students WHERE student_number=%s", (student_number,))
        row = cur.fetchone()
        if not row:
            return 0
        sid = int(row["id"])
        # tentar remover checkins (se não estiver em cascade, isto é necessário; se estiver, não faz mal)
        try:
            cur.execute("DELETE FROM checkins WHERE student_id=%s", (sid,))
        except Exception:
            pass
        # remover o aluno
        cur.execute("DELETE FROM students WHERE id=%s", (sid,))
        return cur.rowcount

# ---------- APAGAR REGISTO (checkin) ----------
def delete_checkin(checkin_id: int) -> int:
    """Apaga um registo (linha) da tabela checkins pelo seu id."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM checkins WHERE id=%s", (checkin_id,))
        return cur.rowcount
