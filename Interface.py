# interface.py
# Refactor: class-based Tk app + version in title + window icon
# + Update UI with progress bar, logs, and auto relaunch after update (only if APPLIED_UPDATE=1)
# -------------------------------------------------------------------


import os
import sys
import time
import json
import threading
import subprocess
import queue
import re
from datetime import datetime, date
from pathlib import Path
import importlib
import smtplib
from toast import ToastManager
import worker
import checkin
import pandas as pd
import tkinter as tk
from tkinter import messagebox
from tkinter import ttk
import tkinter.font as tkFont
from PIL import Image, ImageTk
import serial, serial.tools.list_ports
from dotenv import load_dotenv
from generate_qr import gerar_qr_para_id, enviar_qr_por_email
from worker import enqueue, init as worker_init
from email.mime.text import MIMEText
from email.utils import formataddr
from db import get_student_by_number

from version import __version__                     # <-- VERSION IN TITLE
from paths import get_paths, ensure_file
from generate_qr import gerar_qr_para_id, enviar_qr_por_email
from checkin import (
    log_checkin,
    load_scan_cache,
    reset_unfinished_entries,
    flush_pending_rows,
    
)

from paths import get_paths as _get_paths_hint
_APP_DIR_HINT, _ = _get_paths_hint()
if _APP_DIR_HINT not in sys.path:
    sys.path.insert(0, _APP_DIR_HINT)


# BD: listar registos de hoje (com fallback se BD não estiver disponível)
try:
    from db import fetch_today_checkins
except Exception:
    def fetch_today_checkins():
        raise RuntimeError("DB not available")


# BD (gravação de alunos + QR em BLOB)
try:
    from db import upsert_student, save_qr_image
except Exception:
    # se a BD não estiver configurada, a UI continua a funcionar
    def upsert_student(*args, **kwargs):  # type: ignore
        return 0
    def save_qr_image(*args, **kwargs):  # type: ignore
        pass

# BD (listar/editar/apagar alunos + alterar número)
try:
    from db import fetch_all_students, update_student_fields, delete_student
except Exception as e:
    print("[BD] Import falhou:", e)
    def fetch_all_students(*a, **k): raise RuntimeError(f"DB not available: {e}")  # type: ignore
    def update_student_fields(*a, **k): raise RuntimeError(f"DB not available: {e}")  # type: ignore
    def delete_student(*a, **k): raise RuntimeError(f"DB not available: {e}")  # type: ignore





# --------------------------------------------------------------------------------------
# Utility: safe import of students.py dict by path
# --------------------------------------------------------------------------------------
def load_students_from_file(students_path: str) -> dict:
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("students_data", students_path)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore
        return dict(mod.students)
    except Exception as e:
        print(f"[ERRO] Falha a carregar students.py de {students_path}: {e}")
        return {}

# --------------------------------------------------------------------------------------
# Update Dialog (progress + logs)
# --------------------------------------------------------------------------------------
class UpdateDialog(tk.Toplevel):
    """
    Janela modal para mostrar procura/aplicação de atualizações.
    - Mostra progresso (indeterminado -> determinado quando houver percentagens).
    - Mostra logs (stdout do updater).
    - Ao terminar, relança a app apenas se o updater imprimir 'APPLIED_UPDATE=1'.
    """
    def __init__(self, parent, app_dir: str, data_dir: str, on_finished_callback):
        super().__init__(parent)
        self.parent = parent
        self.app_dir = app_dir
        self.data_dir = data_dir
        self.on_finished_callback = on_finished_callback
        self.title("Atualizações")
        self.transient(parent)
        self.grab_set()
        
        

        self.protocol("WM_DELETE_WINDOW", self._on_close_attempt)

        # UI
        pad = {"padx": 12, "pady": 6}
        tk.Label(self, text="A verificar/instalar atualizações…", font=("Arial", 12, "bold")).grid(row=0, column=0, sticky="w", **pad)

        self.progress = ttk.Progressbar(self, mode="indeterminate", length=380, maximum=100)
        self.progress.grid(row=1, column=0, sticky="ew", **pad)
        self.progress.start(10)

        self.status_var = tk.StringVar(value="A procurar atualizações…")
        tk.Label(self, textvariable=self.status_var, font=("Arial", 10)).grid(row=2, column=0, sticky="w", **pad)

        self.txt = tk.Text(self, height=10, width=60, wrap="none", state="disabled", bg="white")
        self.txt.grid(row=3, column=0, sticky="nsew", padx=12)
        self.grid_rowconfigure(3, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.btn_close = tk.Button(self, text="Fechar", state="disabled", command=self._on_close_clicked)
        self.btn_close.grid(row=4, column=0, pady=(2, 10))

        # Centro na janela principal
        self.after(10, self._center)

        # Estado interno
        self._q = queue.Queue()
        self._stop = False
        self._applied_update = False

        # Async runner
        self._thread = threading.Thread(target=self._run_updater, daemon=True)
        self._thread.start()
        self._poll_queue()

    def _center(self):
        self.update_idletasks()
        ww, wh = self.winfo_width(), self.winfo_height()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        x = (sw // 2) - (ww // 2)
        y = (sh // 2) - (wh // 2)
        self.geometry(f"{ww}x{wh}+{x}+{y}")

    def _append_log(self, line: str):
        self.txt.config(state="normal")
        self.txt.insert("end", line + "\n")
        self.txt.see("end")
        self.txt.config(state="disabled")

    def _set_progress(self, value: int):
        try:
            if self.progress["mode"] != "determinate":
                self.progress.stop()
                self.progress.config(mode="determinate")
            self.progress["value"] = max(0, min(100, value))
        except Exception:
            pass

    def _poll_queue(self):
        try:
            while True:
                item = self._q.get_nowait()
                typ = item[0]
                if typ == "log":
                    line = item[1]
                    self._append_log(line)

                    # marcar se o updater informou que aplicou update
                    if "APPLIED_UPDATE=1" in line:
                        self._applied_update = True

                    # tentar detectar percentagens no stdout (ex.: "PROGRESS: 37" ou "... 37%")
                    m = re.search(r'(\d{1,3})\s*%', line)
                    if not m:
                        m = re.search(r'PROGRESS[:\s]+(\d{1,3})', line, re.I)
                    if m:
                        self._set_progress(int(m.group(1)))
                        self.status_var.set("A descarregar/instalar atualização…")
                    # heurística para estado
                    if re.search(r'no\s+update|up\s*to\s*date|sem\s+atualiza', line, re.I):
                        self.status_var.set("Sem atualizações.")
                    if re.search(r'found|update\s+available|atualiza', line, re.I):
                        self.status_var.set("Atualização encontrada.")

                elif typ == "status":
                    self.status_var.set(item[1])

                elif typ == "done":
                    rc = item[1]
                    self.progress.stop()
                    if rc == 0:
                        if self._applied_update:
                            self._set_progress(100)
                            self.status_var.set("Atualização concluída.")
                            self._append_log("[✔] Atualização concluída.")
                            self.btn_close.config(state="normal")
                            # relançar após pequeno delay
                            self.after(600, self._relaunch_app)
                        else:
                            # Sem update — só permitir fechar
                            if self.progress["mode"] != "determinate":
                                self.progress.config(mode="determinate")
                            self._set_progress(100)
                            self.status_var.set("Sem atualizações disponíveis.")
                            self.btn_close.config(state="normal")
                    else:
                        self.status_var.set("Falha ao atualizar. Consulte os logs.")
                        self._append_log(f"[!] Exit code: {rc}")
                        self.btn_close.config(state="normal")

                elif typ == "error":
                    self.progress.stop()
                    self.status_var.set(item[1])
                    self._append_log("[!] " + item[1])
                    self.btn_close.config(state="normal")
        except queue.Empty:
            if not self._stop:
                self.after(60, self._poll_queue)

    def _run_updater(self):
        """
        Corre {APP_DIR}/updater_install.exe e lê stdout.
        Convenciona-se que o updater imprime:
          - STATUS: <msg>
          - PROGRESS: <0..100>
          - APPLIED_UPDATE=1 (se instalou)
        """
        try:
            exe = Path(self.app_dir) / "updater_install.exe"
            if not exe.exists():
                self._q.put(("error", f"updater_install.exe não encontrado em {exe}"))
                return

            cmd = [str(exe)]
            self._q.put(("log", f"> {' '.join(cmd)}"))

            proc = subprocess.Popen(
                cmd,
                cwd=self.app_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace"
            )

            for line in proc.stdout:
                line = line.rstrip("\r\n")
                self._q.put(("log", line))

            rc = proc.wait()
            self._q.put(("done", rc))
        except Exception as e:
            self._q.put(("error", f"Erro a executar updater: {e}"))

    def _relaunch_app(self):
        try:
            # Reabrir a aplicação
            if getattr(sys, "frozen", False):
                # Executável PyInstaller
                exe = sys.executable
                args = sys.argv[1:]
                subprocess.Popen([exe] + args, close_fds=True)
            else:
                # Script em desenvolvimento
                script = Path(__file__).resolve()
                subprocess.Popen([sys.executable, str(script)], close_fds=True)
        except Exception as e:
            self._append_log(f"[!] Falhou relançar: {e}")
        finally:
            # Encerrar a instância atual
            try:
                self.on_finished_callback()
            except Exception:
                os._exit(0)

    def _on_close_attempt(self):
        # Evitar fechar a meio de update
        if self.btn_close["state"] == "normal":
            self.destroy()
        else:
            messagebox.showinfo("Aguarde", "A atualização está a decorrer. Por favor, aguarde.")

    def _on_close_clicked(self):
        self.destroy()


# --------------------------------------------------------------------------------------
# Main Application
# --------------------------------------------------------------------------------------
class CheckinApp:
    def __init__(self):
        # -------- Paths / working dir ----------
        self.APP_DIR, self.DATA_DIR = get_paths()
        os.makedirs(self.DATA_DIR, exist_ok=True)
        os.chdir(self.DATA_DIR)  # all relative files go to DATA_DIR

        self.EMAIL_HTML   = os.path.join(self.APP_DIR, "email.html")
        self.FUNDO_IMG    = os.path.join(self.APP_DIR, "fundo.jpg")

        self.STUDENTS_FILE = os.path.join(self.DATA_DIR, "students.py")
        self.REGISTOS_DIR  = os.path.join(self.DATA_DIR, "registos")
        self.QRCODES_DIR   = os.path.join(self.DATA_DIR, "qrcodes")
        self.ENV_FILE      = os.path.join(self.DATA_DIR, ".env")

        os.makedirs(self.REGISTOS_DIR, exist_ok=True)
        os.makedirs(self.QRCODES_DIR, exist_ok=True)

        load_dotenv(self.ENV_FILE)  # read SCANNER_PORT / SCANNER_BAUD

        # -------- Data ----------
        self.students = load_students_from_file(self.STUDENTS_FILE)
        self.registo_path = os.path.join(self.REGISTOS_DIR, f"registo_{date.today()}.csv")
        if not os.path.exists(self.registo_path):
            df = pd.DataFrame(columns=["ID", "Nome", "Data", "Hora", "Ação"])
            df.to_csv(self.registo_path, index=False)

        # -------- UI ----------
        self.root = tk.Tk()
        self._ui_last_scan = {}  # student_id -> monotonic timestamp
        worker_init(self.root)
        
        self.tm = ToastManager(self.root)   # gestor de toasts leve/rápido
      

        
        
        
        # VERSION IN TITLE  -----------------------------------------------------------
        self.root.title(f"Registo de Entradas e Saídas - ASFormação – v{__version__}")

        self.root.geometry("600x400")
        self.root.after(10, self._center_root)

        # WINDOW ICON (expects assets/checkin.ico inside APP_DIR or alongside code)
        self._apply_icon()

        self._install_fonts()
        self._build_layout()
        self._build_menubar()  # <<<<<<<<<<<<<<  NOVO: barra de menu no topo
        self._wire_events()

        # -------- Startup housekeeping ----------
        load_scan_cache()
        reset_unfinished_entries()
        flush_pending_rows()

        # -------- Check for updates (UI + progress) ----------
        self.root.after(200, self._check_updates_on_start)

        # -------- Serial thread ----------
        # Só arranca o leitor ~1.5s depois para não "competir" com a janela de update
        self.root.after(1500, lambda: threading.Thread(target=self._iniciar_leitor_serial, daemon=True).start())

       
       
    # --- ADICIONAR dentro da classe CheckinApp ---

    def _fetch_qr_bytes(self, student_number: int) -> bytes | None:
        """Lê o BLOB qr_code da tabela students para o aluno dado."""
        try:
            import pymysql
            # lê do .env (já foi carregado em __init__)
            host = os.getenv("DB_HOST", "127.0.0.1")
            port = int(os.getenv("DB_PORT", "3306") or "3306")
            user = os.getenv("DB_USER", "checkin_db")
            pwd  = os.getenv("DB_PASSWORD", "checkin_pass")
            db   = os.getenv("DB_NAME", "checkin_db")
            conn = pymysql.connect(host=host, port=port, user=user, password=pwd, database=db,
                                   charset="utf8mb4", autocommit=True)
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT qr_code FROM students WHERE student_number=%s", (student_number,))
                    row = cur.fetchone()
                    if not row or not row[0]:
                        return None
                    return row[0]
            finally:
                conn.close()
        except Exception as e:
            messagebox.showerror("BD", f"Falha a obter QR da base de dados:\n{e}")
            return None

    def _open_qr_window(self, student_number: int, title_prefix: str = "QR do aluno"):
        """Abre uma janela com o QR guardado na BD para o aluno dado."""
        data = self._fetch_qr_bytes(student_number)
        if not data:
            from tkinter import messagebox
            messagebox.showinfo("Sem QR", "Não existe QR guardado na base de dados para este aluno.")
            return

        import io
        from PIL import Image, ImageTk
        import tkinter as tk

        win = tk.Toplevel(self.root)
        win.title(f"{title_prefix} {student_number}")
        win.geometry("+120+100")

        try:
            img = Image.open(io.BytesIO(data))
        except Exception as e:
            from tkinter import messagebox
            messagebox.showerror("Erro", f"Dados de imagem inválidos:\n{e}")
            win.destroy()
            return

        MAX_SIDE = 600
        w, h = img.size
        scale = min(1.0, MAX_SIDE / max(w, h))
        if scale < 1.0:
            img = img.resize((int(w*scale), int(h*scale)), Image.LANCZOS)
        tk_img = ImageTk.PhotoImage(img)
        win._tk_img = tk_img  # evitar GC

        lbl = tk.Label(win, image=tk_img)
        lbl.pack(padx=12, pady=12)

        def save_as():
            from tkinter.filedialog import asksaveasfilename
            from tkinter import messagebox
            path = asksaveasfilename(
                title="Guardar QR como…",
                defaultextension=".png",
                initialfile=f"QR_{student_number}.png",
                filetypes=[("PNG", "*.png")]
            )
            if path:
                try:
                    with open(path, "wb") as f:
                        f.write(data)
                    messagebox.showinfo("Guardado", f"QR guardado em:\n{path}")
                except Exception as e:
                    messagebox.showerror("Erro", f"Não foi possível guardar o ficheiro:\n{e}")

        bar = tk.Frame(win); bar.pack(pady=(0,12))
        tk.Button(bar, text="Guardar como…", command=save_as).pack(side="left", padx=6)
        tk.Button(bar, text="Fechar", command=win.destroy).pack(side="left", padx=6)

        win.transient(self.root)
        win.grab_set()
        win.focus_set()
    
       
       
       
        # ---------------------- .env helpers ----------------------
    def _read_env_dict(self) -> dict:
        env = {}
        try:
            if os.path.exists(self.ENV_FILE):
                with open(self.ENV_FILE, "r", encoding="utf-8") as f:
                    for line in f:
                        s = line.strip()
                        if not s or s.startswith("#"):
                            continue
                        if "=" in s:
                            k, v = s.split("=", 1)
                            env[k.strip()] = v.strip()
        except Exception as e:
            messagebox.showerror("Config", f"Não foi possível ler o .env:\n{e}")
        return env

    def _write_env_keys(self, updates: dict):
        """Atualiza/insere pares KEY=VALUE no .env preservando o resto. Faz backup .env.bak."""
        try:
            lines = []
            seen = set()
            if os.path.exists(self.ENV_FILE):
                with open(self.ENV_FILE, "r", encoding="utf-8") as f:
                    lines = f.readlines()

            # substitui linhas existentes
            for i, line in enumerate(lines):
                if "=" in line and not line.lstrip().startswith("#"):
                    k = line.split("=", 1)[0].strip()
                    if k in updates:
                        lines[i] = f"{k}={updates[k]}\n"
                        seen.add(k)

            # acrescenta as que faltam
            extra = [k for k in updates.keys() if k not in seen]
            if extra:
                if lines and not lines[-1].endswith("\n"):
                    lines[-1] += "\n"
                lines.append("\n# Atualizado pela aplicação\n")
                for k in extra:
                    lines.append(f"{k}={updates[k]}\n")

            # backup e grava
            try:
                if os.path.exists(self.ENV_FILE):
                    bak = self.ENV_FILE + ".bak"
                    try:
                        os.replace(self.ENV_FILE, bak)
                    except Exception:
                        pass
            except Exception:
                pass

            with open(self.ENV_FILE, "w", encoding="utf-8") as f:
                f.writelines(lines or [f"{k}={v}\n" for k, v in updates.items()])

            # recarrega .env e atualiza módulos em runtime
            load_dotenv(self.ENV_FILE, override=True)
            self._apply_runtime_env(updates)

        except Exception as e:
            messagebox.showerror("Config", f"Falha a gravar o .env:\n{e}")


    def _apply_runtime_env(self, env: dict):
            """Propaga alterações recentes do .env aos módulos em uso (db e checkin)."""
            # checkin (SMTP usados nos emails de check-in)
            try:
                import checkin as _chk
                if "SMTP_SERVER" in env: _chk.SMTP_SERVER = env["SMTP_SERVER"]
                if "SMTP_PORT"   in env: _chk.SMTP_PORT   = int(env["SMTP_PORT"] or "465")
                if "SMTP_USER"   in env: _chk.SMTP_USER   = env["SMTP_USER"]
                if "SMTP_PASS"   in env: _chk.SMTP_PASS   = env["SMTP_PASS"]
            except Exception:
                pass

            # db (valores usados por _connect())
            try:
                import db as _db
                if "DB_HOST"      in env: _db.DB_HOST  = env["DB_HOST"]
                if "DB_PORT"      in env: _db.DB_PORT  = int(env["DB_PORT"] or "3306")
                if "DB_USER"      in env: _db.DB_USER  = env["DB_USER"]
                if "DB_PASSWORD"  in env: _db.DB_PASS  = env["DB_PASSWORD"]
                if "DB_NAME"      in env: _db.DB_NAME  = env["DB_NAME"]
                if "MACHINE_NAME" in env: _db.DEVICE   = env["MACHINE_NAME"] or None
            except Exception:
                pass   

    def _tools_db(self):
        env = self._read_env_dict()
        win = tk.Toplevel(self.root)
        win.title("Ferramentas • Base de dados (.env)")
        win.transient(self.root); win.grab_set()

        pad = {"padx": 10, "pady": 6}
        frm = tk.Frame(win); frm.pack(fill="both", expand=True, **pad)

        def _val(key, default=""):
            return env.get(key, default)

        v_host = tk.StringVar(value=_val("DB_HOST", "127.0.0.1"))
        v_port = tk.StringVar(value=_val("DB_PORT", "3306"))
        v_user = tk.StringVar(value=_val("DB_USER", "checkin_user"))
        v_pass = tk.StringVar(value=_val("DB_PASSWORD", "checkin_pass"))
        v_name = tk.StringVar(value=_val("DB_NAME", "checkin_db"))
        v_dev  = tk.StringVar(value=_val("MACHINE_NAME", ""))

        row=0
        for lbl, var in [("Host", v_host), ("Porta", v_port), ("Utilizador", v_user),
                         ("Password", v_pass), ("Base de dados", v_name), ("Nome da máquina (opcional)", v_dev)]:
            tk.Label(frm, text=lbl, font=("Arial", 11)).grid(row=row, column=0, sticky="e", **pad)
            show = "" if lbl!="Password" else "*"
            ent = tk.Entry(frm, textvariable=var, font=("Arial", 11), width=34, show=show)
            ent.grid(row=row, column=1, sticky="w", **pad)
            if lbl=="Password":
                def toggle_show(e=ent):
                    e.config(show=(" " if e.cget("show") == "*" else "*").strip())
                tk.Button(frm, text="👁", command=toggle_show, width=3).grid(row=row, column=2, sticky="w")
            row+=1

        msg = tk.Label(frm, text="", fg="green", font=("Arial", 10))
        msg.grid(row=row, column=0, columnspan=3, sticky="w", padx=10); row+=1

        def _test():
            try:
                import pymysql
                conn = pymysql.connect(
                    host=v_host.get().strip(),
                    port=int(v_port.get().strip() or "3306"),
                    user=v_user.get().strip(),
                    password=v_pass.get(),
                    database=v_name.get().strip(),
                    connect_timeout=4,
                )
                conn.close()
                messagebox.showinfo("Ligação", "Ligação à base de dados OK.")
            except Exception as e:
                messagebox.showerror("Ligação", f"Falha na ligação:\n{e}")

        def _save():
            try:
                port = int(v_port.get().strip() or "3306")
                if not v_host.get().strip() or not v_name.get().strip():
                    messagebox.showwarning("Campos", "Host e Base de dados são obrigatórios.")
                    return
                updates = {
                    "DB_HOST": v_host.get().strip(),
                    "DB_PORT": str(port),
                    "DB_USER": v_user.get().strip(),
                    "DB_PASSWORD": v_pass.get(),
                    "DB_NAME": v_name.get().strip(),
                    "MACHINE_NAME": v_dev.get().strip(),
                }
                self._write_env_keys(updates)
                msg.config(text="Guardado no .env. (Novas ligações usarão estas definições.)", fg="green")
            except Exception as e:
                messagebox.showerror("Guardar", f"Não foi possível guardar:\n{e}")

        bar = tk.Frame(win); bar.pack(fill="x", pady=(0,8))
        tk.Button(bar, text="Testar ligação", command=_test).pack(side="left", padx=10)
        tk.Button(bar, text="Guardar", command=_save).pack(side="right", padx=10)
        tk.Button(bar, text="Fechar", command=win.destroy).pack(side="right")

        # centro
        win.update_idletasks()
        ww, wh = win.winfo_width(), win.winfo_height()
        sw, sh = win.winfo_screenwidth(), self.root.winfo_screenheight()
        x = (sw//2 - ww//2); y = (sh//2 - wh//2)
        win.geometry(f"+{x}+{y}")

    def _tools_email(self):
        env = self._read_env_dict()
        win = tk.Toplevel(self.root)
        win.title("Ferramentas • Configuração de email (.env)")
        win.transient(self.root); win.grab_set()

        pad = {"padx": 10, "pady": 6}
        frm = tk.Frame(win); frm.pack(fill="both", expand=True, **pad)

        def _val(key, default=""):
            return env.get(key, default)

        v_srv  = tk.StringVar(value=_val("SMTP_SERVER", ""))
        v_port = tk.StringVar(value=_val("SMTP_PORT", "465"))
        v_user = tk.StringVar(value=_val("SMTP_USER", ""))
        v_pass = tk.StringVar(value=_val("SMTP_PASS", ""))

        row=0
        for lbl, var in [("Servidor (SSL)", v_srv), ("Porta", v_port), ("Utilizador (From)", v_user), ("Password", v_pass)]:
            tk.Label(frm, text=lbl, font=("Arial", 11)).grid(row=row, column=0, sticky="e", **pad)
            show = "" if lbl!="Password" else "*"
            ent = tk.Entry(frm, textvariable=var, font=("Arial", 11), width=34, show=show)
            ent.grid(row=row, column=1, sticky="w", **pad)
            if lbl=="Password":
                def toggle_show(e=ent):
                    e.config(show=(" " if e.cget("show") == "*" else "*").strip())
                tk.Button(frm, text="👁", command=toggle_show, width=3).grid(row=row, column=2, sticky="w")
            row+=1

        tk.Label(frm, text="Enviar teste para (opcional):", font=("Arial", 11)).grid(row=row, column=0, sticky="e", **pad)
        v_test_to = tk.StringVar(value="")
        tk.Entry(frm, textvariable=v_test_to, font=("Arial", 11), width=34).grid(row=row, column=1, sticky="w", **pad)
        row+=1

        msg = tk.Label(frm, text="", fg="green", font=("Arial", 10))
        msg.grid(row=row, column=0, columnspan=3, sticky="w", padx=10); row+=1

        def _save():
            try:
                port = int(v_port.get().strip() or "465")
                updates = {
                    "SMTP_SERVER": v_srv.get().strip(),
                    "SMTP_PORT": str(port),
                    "SMTP_USER": v_user.get().strip(),
                    "SMTP_PASS": v_pass.get(),
                }
                self._write_env_keys(updates)
                msg.config(text="Guardado no .env. (Novos emails usarão estas definições.)", fg="green")
            except Exception as e:
                messagebox.showerror("Guardar", f"Não foi possível guardar:\n{e}")

        def _send_test():
            to = v_test_to.get().strip()
            if not to:
                messagebox.showwarning("Teste", "Indique o destinatário para o email de teste."); return
            try:
                srv  = v_srv.get().strip()
                port = int(v_port.get().strip() or "465")
                usr  = v_user.get().strip()
                pwd  = v_pass.get()

                with smtplib.SMTP_SSL(srv, port, timeout=10) as server:
                    if usr:
                        server.login(usr, pwd)
                    msg = MIMEText("Email de teste – ASFormação (config .env).", "plain", "utf-8")
                    msg["From"] = formataddr(("ASFormação", usr or ""))
                    msg["To"] = to
                    msg["Subject"] = "Teste SMTP"

                    server.sendmail(usr or "", [to], msg.as_string())

                messagebox.showinfo("Teste", "Email de teste enviado (SSL).")
            except Exception as e:
                messagebox.showerror("Teste", f"Falha ao enviar teste:\n{e}")

        bar = tk.Frame(win); bar.pack(fill="x", pady=(0,8))
        tk.Button(bar, text="Enviar teste", command=_send_test).pack(side="left", padx=10)
        tk.Button(bar, text="Guardar", command=_save).pack(side="right", padx=10)
        tk.Button(bar, text="Fechar", command=win.destroy).pack(side="right")

        # centro
        win.update_idletasks()
        ww, wh = win.winfo_width(), win.winfo_height()
        sw, sh = win.winfo_screenwidth(), self.root.winfo_screenheight()
        x = (sw//2 - ww//2); y = (sh//2 - wh//2)
        win.geometry(f"+{x}+{y}")




    def _ver_lista_completa(self):
        """Lista completa a partir da BD, com editar/guardar e apagar por linha."""
        win = tk.Toplevel(self.root)
        win.title("Lista completa de alunos (BD)")
        win.transient(self.root); win.grab_set()
        win.geometry("950x520")

        # Barra de pesquisa
        top = tk.Frame(win); top.pack(fill="x", padx=10, pady=8)
        tk.Label(top, text="Pesquisar (ID, nome ou email):", font=("Arial", 11)).pack(side="left")
        ent_q = tk.Entry(top, font=("Arial", 11), width=40); ent_q.pack(side="left", padx=8)
        count_var = tk.StringVar(value="")
        tk.Label(top, textvariable=count_var, font=("Arial", 10), fg="#555").pack(side="right")

        # Área scrollável
        outer = tk.Frame(win, bd=1, relief="sunken"); outer.pack(fill="both", expand=True, padx=10, pady=(0,10))
        canvas = tk.Canvas(outer, highlightthickness=0)
        vsb = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y"); canvas.pack(side="left", fill="both", expand=True)
        rows_holder = tk.Frame(canvas)
        canvas.create_window((0,0), window=rows_holder, anchor="nw")

        # Cabeçalho
        hdr = tk.Frame(rows_holder, bg="#f5f5f5")
        hdr.pack(fill="x")
        tk.Label(hdr, text="Nº",      width=8,  bg="#f5f5f5",
                font=("Arial", 10, "bold"), anchor="center"
                ).grid(row=0, column=0, sticky="nsew", padx=0, pady=6)

        tk.Label(hdr, text="Nome",    width=28, bg="#f5f5f5",
                font=("Arial", 10, "bold"), anchor="center"
                ).grid(row=0, column=1, sticky="nsew")

        tk.Label(hdr, text="Email 1", width=28, bg="#f5f5f5",
                font=("Arial", 10, "bold"), anchor="center"
                ).grid(row=0, column=2, sticky="nsew")

        tk.Label(hdr, text="Email 2", width=28, bg="#f5f5f5",
                font=("Arial", 10, "bold"), anchor="center"
                ).grid(row=0, column=3, sticky="nsew", padx=0)

        tk.Label(hdr, text="Ações",   width=16, bg="#f5f5f5",
                font=("Arial", 10, "bold"), anchor="center"
                ).grid(row=0, column=4, sticky="nsew", padx=0)




        rows_frame = tk.Frame(rows_holder); rows_frame.pack(fill="both", expand=True)

        def _refresh_scrollregion(_=None):
            rows_holder.update_idletasks()
            canvas.configure(scrollregion=canvas.bbox("all"))
        rows_holder.bind("<Configure>", _refresh_scrollregion)

        # Fonte de dados: sempre BD
        current_rows = []  # lista de dicts vindos da BD

        def _load(query: str | None = None):
            nonlocal current_rows
            try:
                current_rows = fetch_all_students(query=query, limit=2000, offset=0) or []
            except Exception as e:
                messagebox.showerror("BD", f"Falha ao obter alunos:\n{e}")
                current_rows = []
            _rebuild_rows()

        def _rebuild_rows():
            for w in rows_frame.winfo_children():
                w.destroy()

            for row in current_rows:
                sid  = str(row.get("student_number") or "")
                nome = row.get("name") or ""
                e1   = row.get("email1") or ""
                e2   = row.get("email2") or ""

                fr = tk.Frame(rows_frame); fr.pack(fill="x", padx=6, pady=4)

                tk.Label(fr, text=sid, width=8, anchor="w", font=("Arial", 10)).grid(row=0, column=0, padx=4, sticky="w")



                vnome = tk.StringVar(value=nome)
                ve1   = tk.StringVar(value=e1)
                ve2   = tk.StringVar(value=e2)

                tk.Entry(fr, textvariable=vnome, width=28, font=("Arial", 10)).grid(row=0, column=1, padx=4, sticky="w")
                tk.Entry(fr, textvariable=ve1,   width=28, font=("Arial", 10)).grid(row=0, column=2, padx=4, sticky="w")
                tk.Entry(fr, textvariable=ve2,   width=28, font=("Arial", 10)).grid(row=0, column=3, padx=4, sticky="w")

            
                btn_save = tk.Button(fr, text="Guardar", font=("Arial", 10))
                btn_del  = tk.Button(fr, text="Apagar",  font=("Arial", 10))
               # --- ADICIONAR dentro do loop de construção de linhas, por aluno ---
                btn_qr = tk.Button(fr, text="Ver QR", font=("Arial", 10),
                                command=lambda s=int(sid): self._open_qr_window(s))
                btn_qr.grid(row=0, column=4, padx=(6,2), pady=2, sticky="w")

                def _do_save(s, vn, v1, v2, b):
                    name   = vn.get().strip()
                    email1 = v1.get().strip()
                    email2 = v2.get().strip()

                    if not name:
                        messagebox.showwarning("Campos", "O nome é obrigatório."); return
                    for val, lbl in [(email1, "Email 1"), (email2, "Email 2")]:
                        if val and ("@" not in val or "." not in val.split("@")[-1]):
                            messagebox.showwarning("Campos", f"{lbl} inválido."); return

                    try:
                        update_student_fields(int(s), name=name,
                                            email1=(email1 or None),
                                            email2=(email2 or None))
                        b.config(text="Guardado", state="disabled")
                        win.after(700, lambda: (_load(ent_q.get().strip() or None), b.config(text="Guardar", state="normal")))
                    except Exception as e:
                        messagebox.showerror("BD", f"Não foi possível guardar:\n{e}")


                def _do_delete(s=sid, f=fr):
                    if not messagebox.askyesno("Confirmar", f"Apagar o aluno ID {s}?\nOs registos associados podem ser removidos."):
                        return
                    try:
                        delete_student(int(s))
                        f.destroy()
                        # Atualiza contagem
                        _update_count(-1)
                    except Exception as e:
                        messagebox.showerror("BD", f"Não foi possível apagar:\n{e}")

                btn_save.config(command=lambda s=sid, vn=vnome, v1=ve1, v2=ve2, b=btn_save: _do_save(s, vn, v1, v2, b))
                btn_del.config(command=_do_delete)

                btn_save.grid(row=0, column=5, padx=(6,2), pady=2, sticky="w")
                btn_del.grid(row=0, column=6, padx=(2,6),  pady=2, sticky="w")

            count_var.set(f"{len(current_rows)} aluno(s)")
            _refresh_scrollregion()

        def _update_count(delta=0):
            try:
                n = len(current_rows) + delta
            except Exception:
                n = len(current_rows)
            count_var.set(f"{n} aluno(s)")

        # Pesquisa
        def _apply_filter(*_):
            q = ent_q.get().strip()
            _load(query=q or None)

        ent_q.bind("<KeyRelease>", _apply_filter)

        # Inicialização
        _load()
        def _center():
            win.update_idletasks()
            ww, wh = win.winfo_width(), win.winfo_height()
            sw, sh = win.winfo_screenwidth(), self.root.winfo_screenheight()
            x = (sw // 2) - (ww // 2); y = (sh // 2) - (wh // 2)
            win.geometry(f"{ww}x{wh}+{x}+{y}")
        _center()
        win.bind("<Escape>", lambda e: win.destroy())

    # ---------------------- Update flow ----------------------
    def _check_updates_on_start(self):
        """
        Abre o diálogo que corre o updater. Se instalar com sucesso,
        o diálogo relança a app e fecha esta instância.
        """
        updater_path = Path(self.APP_DIR) / "updater_install.exe"
        if not updater_path.exists():
            # Sem updater — não faz nada
            return

        def on_finished():
            # encerra a app atual (para não ficar 2 instâncias)
            try:
                self.root.destroy()
            except Exception:
                os._exit(0)

        dlg = UpdateDialog(self.root, self.APP_DIR, self.DATA_DIR, on_finished_callback=on_finished)
        dlg.lift()
        dlg.focus_force()

    # ---------------------- UI scaffolding ----------------------
    def _center_root(self):
        self.root.update_idletasks()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        x, y = (sw // 2) - (w // 2), (sh // 2) - (h // 2)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _apply_icon(self):
        # Try {APP_DIR}/assets/checkin.ico, then {APP_DIR}/checkin.ico
        candidates = [
            Path(self.APP_DIR) / "assets" / "checkin.ico",
            Path(self.APP_DIR) / "checkin.ico",
            Path(__file__).resolve().parent / "assets" / "checkin.ico",
        ]
        for p in candidates:
            if p.exists():
                try:
                    self.root.iconbitmap(p)
                except Exception as e:
                    print(f"[i] Falhou aplicar ícone ({p}): {e}")
                break

    def _install_fonts(self):
        try:
            self.roboto_font = tkFont.Font(family="Roboto", size=18, weight="bold")
        except:
            self.roboto_font = tkFont.Font(family="Arial", size=18, weight="bold")

    def _build_layout(self):
        # Background image (if exists)
        if os.path.exists(self.FUNDO_IMG):
            self._original_bg = Image.open(self.FUNDO_IMG)
            self.label_fundo = tk.Label(self.root)
            self.label_fundo.place(x=0, y=0, relwidth=1, relheight=1)
            self.label_fundo.lower()
            self.root.bind("<Configure>", self._atualizar_fundo)

        # (REMOVIDO) Menu button / frame lateral — substituído por menubar no topo
        # self.menu_open = False
        # self.btn_menu = ...
        # self.frame_menu = ...

        # Registos panel (igual)
        self.registo_frame = tk.Frame(self.root, bg="#FFFFFF", highlightbackground="#00A49A", highlightthickness=1)
        self.registo_frame.pack_propagate(False)
        bar = tk.Frame(self.registo_frame, bg="#f5f5f5"); bar.pack(fill="x")
        tk.Label(bar, text="Registos de hoje", font=("Arial", 12, "bold"),
                 bg="#f5f5f5").pack(side="left", padx=8, pady=6)
        tk.Button(bar, text="✖", command=lambda: self.registo_frame.place_forget(),
                  bd=0, bg="#f5f5f5", activebackground="#eaeaea").pack(side="right", padx=6, pady=4)

        content = tk.Frame(self.registo_frame, bg="white"); content.pack(fill="both", expand=True, padx=8, pady=8)
        scroll = tk.Scrollbar(content); scroll.pack(side="right", fill="y")
        self.txt_registos = tk.Text(content, wrap="none", font=("Courier", 12), bg="white", state="disabled")
        self.txt_registos.pack(side="left", fill="both", expand=True)
        self.txt_registos.config(yscrollcommand=scroll.set); scroll.config(command=self.txt_registos.yview)

        # Feedback
        self.feedback_label = tk.Label(self.root, text="", font=("Arial", 14), bg="white", fg="green")
        self.feedback_label.place_forget()

        # Last-read label
        self.lido_var = tk.StringVar(value="Leitor pronto")
        self.lbl_lido = tk.Label(self.root, textvariable=self.lido_var, font=("Arial", 18, "bold"),
                                 bg="white", fg="black")
        self.lbl_lido.place(relx=0.5, rely=0.8, anchor="center")

    def _build_menubar(self):
        """Barra de menu no topo (substitui o hambúrguer lateral)."""
        menubar = tk.Menu(self.root)

        m_alunos = tk.Menu(menubar, tearoff=0)
        m_alunos.add_command(label="➕ Adicionar aluno…", command=self._adicionar_aluno)
        m_alunos.add_command(label="Ver lista completa…", command=self._ver_lista_completa)
        menubar.add_cascade(label="Alunos", menu=m_alunos)

        m_reg = tk.Menu(menubar, tearoff=0)
        m_reg.add_command(label="📋 Ver registos de hoje", command=self._mostrar_registos)
        m_reg.add_command(label="🔄 Atualizar lista", command=self._atualizar_lista)
        menubar.add_cascade(label="Registos", menu=m_reg)

        # --- NOVO: Ferramentas ---
        m_tools = tk.Menu(menubar, tearoff=0)
        m_tools.add_command(label="Base de dados…", command=self._tools_db)
        m_tools.add_command(label="Configuração de email…", command=self._tools_email)
        menubar.add_cascade(label="Ferramentas", menu=m_tools)

        m_sys = tk.Menu(menubar, tearoff=0)
        m_sys.add_command(label="⚙️ Verificar atualizações…", command=self._check_updates_on_start)
        m_sys.add_separator()
        m_sys.add_command(label="Sair", command=self.root.destroy)
        menubar.add_cascade(label="Sistema", menu=m_sys)

        self.root.config(menu=menubar)

    def _wire_events(self):
        self.root.bind("<Escape>", lambda e: self.registo_frame.place_forget())

    # ---------------------- Background image ----------------------
    def _atualizar_fundo(self, event=None):
        if not hasattr(self, "_original_bg"):
            return
        resized = self._original_bg.resize((self.root.winfo_width(), self.root.winfo_height()), Image.LANCZOS)
        fundo_photo = ImageTk.PhotoImage(resized)
        self.label_fundo.config(image=fundo_photo)
        self.label_fundo.image = fundo_photo

    # ---------------------- Registos ----------------------
    def _set_registos_text(self, lines):
        self.txt_registos.config(state="normal")
        self.txt_registos.delete("1.0", "end")
        self.txt_registos.insert("end", "\n".join(lines))
        self.txt_registos.config(state="disabled")

    def _mostrar_registos(self):
        self._atualizar_lista()
        self.registo_frame.place(relx=0.5, rely=0.25, anchor="n", width=560, height=240)
        self.registo_frame.lift()

    def _atualizar_lista(self):
        try:
            linhas = []

            # 1) Tenta ir à BD (mais fiável)
            try:
                rows = fetch_today_checkins()  # [{timestamp, name, student_number, action, device_name}, ...]
                if rows:
                    # Ordenar por timestamp desc (por segurança)
                    rows.sort(key=lambda r: r.get("timestamp"), reverse=True)
                    for r in rows:
                        ts = r.get("timestamp")
                        # Formatar hora HH:MM:SS mesmo que venha datetime/str
                        try:
                            hora = ts.strftime("%H:%M:%S")
                        except Exception:
                            hora = str(ts)[11:19]
                        nome = r.get("name") or ""
                        num  = r.get("student_number")
                        acc  = r.get("action") or r.get("acao") or r.get("Ação") or ""
                        linhas.append(f"{hora} - {nome} ({num}) - {acc}")
            except Exception as e:
                # Se a BD falhar, continuamos para o CSV
                pass

            # 2) Se não vier nada da BD, usar CSV (como antes)
            if not linhas:
                # Recalcula o caminho do CSV de hoje sempre que atualizas
                reg_dir = os.path.join(self.DATA_DIR, "registos")
                reg_path = os.path.join(reg_dir, f"registo_{date.today()}.csv")

                if not os.path.exists(reg_path):
                    # fallback: tentar o CSV mais recente na pasta
                    if os.path.isdir(reg_dir):
                        try:
                            candidates = [f for f in os.listdir(reg_dir) if f.startswith("registo_") and f.endswith(".csv")]
                            if candidates:
                                latest = max(candidates)  # nomes YYYY-MM-DD ordenam bem
                                reg_path = os.path.join(reg_dir, latest)
                        except Exception:
                            pass

                if os.path.exists(reg_path):
                    import pandas as pd
                    df = pd.read_csv(reg_path)
                    # Filtra pelo dia de hoje, se a coluna existir
                    if "Data" in df.columns:
                        df = df[df["Data"] == str(date.today())]
                    # Monta as linhas semelhantes ao original
                    if {"Hora","Nome","Ação"}.issubset(df.columns):
                        for _, row in df.iterrows():
                            linhas.append(f"{row['Hora']} - {row['Nome']} ({row.get('ID','')}) - {row['Ação']}")
                    else:
                        # Se as colunas não corresponderem, mostra algo útil
                        for _, row in df.tail(20).iterrows():
                            linhas.append(" | ".join(str(v) for v in row.values))
                else:
                    linhas = ["Sem registos hoje."]

            self._set_registos_text(linhas or ["Sem registos hoje."])
        except Exception as e:
            self._set_registos_text([f"Erro a carregar registos: {e}"])


    # ---------------------- Feedback helpers ----------------------
    def _mostrar_feedback(self, msg, sucesso=True):
        self.feedback_label.config(text=msg, fg="green" if sucesso else "red")
        self.feedback_label.lift()
        self.feedback_label.place(relx=0.5, rely=0.9, anchor="center")
        self.root.after(5000, lambda: self.feedback_label.place_forget())

    def _show_last_read(self, name, sid="", success=True):
        now = datetime.now().strftime("%H:%M:%S")
        txt = f"Última leitura {now} – {name}" + (f" ({sid})" if sid else "")
        self.lido_var.set(txt)
        self.lbl_lido.config(fg=("green" if success else "red"))
        self.lbl_lido.lift()

    # ---------------------- Students persistence ----------------------
    def _guardar_students(self):
        # normalize to [nome, email1, email2]
        data = {}
        for sid, dados in self.students.items():
            nome   = (dados[0] if len(dados) > 0 else "").strip()
            email1 = (dados[1] if len(dados) > 1 else "").strip()
            email2 = (dados[2] if len(dados) > 2 else "").strip()
            data[sid] = [nome, email1, email2]
        with open(self.STUDENTS_FILE, "w", encoding="utf-8") as f:
            f.write("students = ")
            json.dump(data, f, ensure_ascii=False, indent=4)
            f.write("\n")

    

    # ---------------------- Add student dialog ----------------------
    def _adicionar_aluno(self):
        def submit(event=None):
            nome   = entry_nome.get().strip()
            email1 = entry_email1.get().strip()
            email2 = entry_email2.get().strip()

            if not nome:
                messagebox.showwarning("Campo obrigatório", "Indique o nome do aluno.")
                return

            if email1 and ("@" not in email1 or "." not in email1.split("@")[-1]):
                messagebox.showwarning("Email inválido", "Verifique o email principal.")
                return
            if email2 and ("@" not in email2 or "." not in email2.split("@")[-1]):
                messagebox.showwarning("Email inválido", "Verifique o email secundário.")
                return

            # Duplicates per your logic
            name_l  = nome.lower()
            email_l = email1.lower()
            for _, dados in self.students.items():
                n = (dados[0] if len(dados) > 0 else "").strip().lower()
                e = (dados[1] if len(dados) > 1 else "").strip().lower()
                if email1:
                    if n == name_l and e == email_l:
                        messagebox.showerror("Duplicado",
                                             f"O aluno '{nome}' com o email '{email1}' já existe.")
                        return
                else:
                    if n == name_l and not e:
                        messagebox.showerror("Duplicado",
                                             f"Já existe um aluno chamado '{nome}' sem email principal.")
                        return

            novo_id = str(max(int(k) for k in self.students.keys()) + 1) if self.students else "1001"



            try:
                caminho_qr = gerar_qr_para_id(novo_id, nome)
            except Exception as e:
                messagebox.showerror("Erro", f"Não foi possível gerar o QR:\n{e}")
                return
            
            # --- NOVO: gravar na BD (aluno + QR em BLOB) -------------------------
            try:
                # 1) cria/atualiza aluno (texto)
                upsert_student(
                    student_number=int(novo_id),
                    name=nome,
                    email1=email1 or None,
                    email2=email2 or None,
                    qr_png=None,  # vamos enviar já a seguir
                )
                # 2) guardar BLOB do QR (se a coluna qr_code existir)
                try:
                    with open(caminho_qr, "rb") as f:
                        save_qr_image(int(novo_id), f.read())
                except FileNotFoundError:
                    pass
            except Exception as e:
                print(f"[BD] Falhou gravar aluno/QR na BD: {e}")
            # --------------------------------------------------------------------

            btn.config(state="disabled", text="A enviar...", cursor="watch")
            win.update_idletasks()

            def enviar_async():
                ok, err = True, None
                try:
                    enviar_qr_por_email(caminho_qr, nome)
                except Exception as e:
                    ok, err = False, e

                def finish():
                    btn.config(state="normal", text="Adicionar", cursor="")
                    if ok:
                        messagebox.showinfo(
                            "Aluno Adicionado",
                            f"Aluno {nome} adicionado com o ID {novo_id}.\nQR gerado e email enviado."
                        )
                        win.destroy()
                    else:
                        messagebox.showwarning(
                            "QR gerado (email falhou)",
                            f"O QR foi criado em:\n{caminho_qr}\n\nNão foi possível enviar o email:\n{err}"
                        )
                win.after(0, finish)

            threading.Thread(target=enviar_async, daemon=True).start()

        # Dialog UI
        win = tk.Toplevel(self.root)
        win.title("Adicionar Aluno")
        win.transient(self.root)
        win.grab_set()

        pad = {'padx': 12, 'pady': 6}
        lbl_font = ("Arial", 12)
        ent_font = ("Arial", 12)

        tk.Label(win, text="Nome completo:", font=lbl_font).grid(row=0, column=0, sticky="w", **pad)
        entry_nome = tk.Entry(win, font=ent_font, width=42); entry_nome.grid(row=0, column=1, **pad)

        tk.Label(win, text="Email principal (opcional):", font=lbl_font).grid(row=1, column=0, sticky="w", **pad)
        entry_email1 = tk.Entry(win, font=ent_font, width=42); entry_email1.grid(row=1, column=1, **pad)

        tk.Label(win, text="Email secundário (opcional):", font=lbl_font).grid(row=2, column=0, sticky="w", **pad)
        entry_email2 = tk.Entry(win, font=ent_font, width=42); entry_email2.grid(row=2, column=1, **pad)

        btn = tk.Button(win, text="Adicionar", font=("Arial", 12, "bold"), command=submit)
        btn.grid(row=3, column=0, columnspan=2, pady=14)

        entry_nome.focus_set()
        win.bind("<Return>", submit)
        win.bind("<Escape>", lambda e: win.destroy())

        def center_window():
            win.update_idletasks()
            ww, wh = win.winfo_width(), win.winfo_height()
            sw, sh = win.winfo_screenwidth(), self.root.winfo_screenheight()
            x = (sw // 2) - (ww // 2)
            y = (sh // 2) - (wh // 2)
            win.geometry(f"{ww}x{wh}+{x}+{y}")
        win.after(10, center_window); win.after(100, center_window)

        self.root.wait_window(win)

    # ---------------------- Check-in + feedback ----------------------

    def _registar(self, student_id: str):
        """Instant UI, UI-side debounce, heavy work in the background."""
        if not hasattr(self, "_ui_last_scan"):
            self._ui_last_scan = {}

        # --- Debounce UI (mesmo aluno dentro de X segundos) ---
        COOLDOWN = 10
        now = time.monotonic()
        last = self._ui_last_scan.get(student_id, 0.0)
        if now - last < COOLDOWN:
            self._mostrar_feedback("Registo ignorado (duplicado)", sucesso=False)
            self._show_last_read("Duplicado", student_id, False)
            return
        self._ui_last_scan[student_id] = now

        # Prever ação (Entrada/Saída) com base no último estado conhecido
        prev = getattr(checkin, "last_scan_times", {}).get(student_id)
        tipo_guess = "Saída" if (prev and prev.get("last_tipo") == "Entrada") else "Entrada"

        # Tentar obter o nome da BD (rápido). Se falhar, mostrar "Aluno <ID>".
        try:
            digits = "".join(ch for ch in str(student_id) if ch.isdigit())
            nome = None
            if digits:
                row = get_student_by_number(int(digits))
                if row and isinstance(row, dict):
                    nome = row.get("name")
            if not nome:
                nome = f"Aluno {digits or student_id}"
        except Exception:
            nome = f"Aluno {student_id}"

        # Feedback imediato (como pediste): "Entrada…\nNome"
        self._mostrar_feedback(f"{tipo_guess}…\n{nome}", sucesso=True)
        self._show_last_read(nome, student_id, True)

        # Trabalho real (DB/Sheets/CSV/email) no worker thread
        enqueue(log_checkin, student_id, on_done=self._after_checkin)





            
            
    def _after_checkin(self, result):
        """
        Runs on the Tk thread after background log_checkin finishes.
        `result` is either (nome, tipo) or None if it was ignored as duplicate.
        """
        try:
            if result:
                nome, tipo = result
                # refine the optimistic message with the actual action
                self._mostrar_feedback(f"{tipo} registada:\n{nome}", sucesso=True)
                self._show_last_read(nome, "", True)
            else:
                # duplicate or ignored
                self._mostrar_feedback("Registo ignorado (duplicado)", sucesso=False)
            # refresh the 'Registos de hoje' pane from disk
            self._atualizar_lista()
        except Exception:
            # never let UI crash on callback
            pass



    # ---------------------- Serial scanner ----------------------
    def _list_serial_ports(self):
        print("[i] Available serial ports:")
        for p in serial.tools.list_ports.comports():
            print(f"  - {p.device}: {p.description}")

    def _iniciar_leitor_serial(self):
        port = os.getenv("SCANNER_PORT", "COM3")
        baud = int(os.getenv("SCANNER_BAUD", "9600"))

        while True:
            try:
                self.root.after(0, lambda: self._show_last_read("Connecting scanner…", success=False))
                print(f"[i] Opening serial {port} @ {baud}")
                ser = serial.Serial(port=port, baudrate=baud, timeout=0.2)
                self.root.after(0, lambda: self._show_last_read("Scanner pronto", success=True))
            except Exception as e:
                print(f"[ERRO] Could not open {port}: {e}")
                print("[i] Available ports:"); self._list_serial_ports()
                time.sleep(3); continue

            buffer = ""
            try:
                while True:
                    chunk = ser.read(ser.in_waiting or 1)
                    if not chunk:
                        continue
                    buffer += chunk.decode("utf-8", errors="ignore")

                    while True:
                        idx_r = buffer.find("\r"); idx_n = buffer.find("\n")
                        idx = min(i for i in (idx_r, idx_n) if i != -1) if (idx_r != -1 or idx_n != -1) else -1
                        if idx == -1:
                            break
                        line, buffer = buffer[:idx], buffer[idx+1:]
                        code = line.strip()
                        if not code:
                            continue

                        def handle(s=code):
                            self._registar(s)

                        self.root.after(0, handle)
            except Exception as e:
                print(f"[ERRO] Serial read error: {e}")
                try: ser.close()
                except: pass
                self.root.after(0, lambda: self._show_last_read("Scanner disconnected – retrying…", success=False))
                time.sleep(2)

    # ---------------------- Public API ----------------------
    def run(self):
        self.root.mainloop()

# --------------------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------------------
if __name__ == "__main__":
    app = CheckinApp()
    app.run()
