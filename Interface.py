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

import pandas as pd
import tkinter as tk
from tkinter import messagebox
from tkinter import ttk
import tkinter.font as tkFont
from PIL import Image, ImageTk
import serial, serial.tools.list_ports
from dotenv import load_dotenv

from version import __version__                     # <-- VERSION IN TITLE
from paths import get_paths, ensure_file
from generate_qr import gerar_qr_para_id, enviar_qr_por_email
from checkin import (
    log_checkin,
    load_scan_cache,
    reset_unfinished_entries,
    flush_pending_rows,
    reload_students as reload_students_in_checkin,
)

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
        # VERSION IN TITLE  -----------------------------------------------------------
        self.root.title(f"Registo de Entradas e Saídas - ASFormação – v{__version__}")

        self.root.geometry("600x400")
        self.root.after(10, self._center_root)

        # WINDOW ICON (expects assets/checkin.ico inside APP_DIR or alongside code)
        self._apply_icon()

        self._install_fonts()
        self._build_layout()
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

        # Menu button
        self.menu_open = False
        self.btn_menu = tk.Button(
            self.root, text="☰", command=self._toggle_menu, font=("Arial", 16),
            fg="#00A49A", bg=self.root["bg"], activebackground=self.root["bg"], bd=0, relief="flat"
        )
        self.btn_menu.place(x=10, y=350)

        # Side menu
        self.frame_menu = tk.Frame(self.root, bg="white", highlightbackground="#00A49A", highlightthickness=1)
        tk.Button(self.frame_menu, text="➕ Adicionar aluno", command=self._adicionar_aluno,
                  font=("Arial", 12), fg="#00A49A", bg="white", bd=0, relief="flat",
                  anchor="w", padx=20).pack(fill="x", pady=(20, 0))
        tk.Button(self.frame_menu, text="📋 Ver registos", command=lambda: self._toggle_menu() | self._toggle_registos(),
                  font=("Arial", 12), fg="#00A49A", bg="white", bd=0, relief="flat",
                  anchor="w", padx=20).pack(fill="x", pady=10)
        tk.Button(self.frame_menu, text="❌ Fechar menu", command=self._toggle_menu,
                  font=("Arial", 12), fg="red", bg="white", bd=0, relief="flat",
                  anchor="w", padx=20).pack(fill="x", pady=(30, 0))

        # Registos panel
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

    # ---------------------- Menu ----------------------
    def _toggle_menu(self):
        if self.menu_open:
            self.frame_menu.place_forget(); self.menu_open = False
        else:
            self.frame_menu.place(x=0, y=0, width=180, relheight=1); self.frame_menu.lift(); self.menu_open = True

    # ---------------------- Registos ----------------------
    def _set_registos_text(self, lines):
        self.txt_registos.config(state="normal")
        self.txt_registos.delete("1.0", "end")
        self.txt_registos.insert("end", "\n".join(lines))
        self.txt_registos.config(state="disabled")

    def _toggle_registos(self):
        if self.registo_frame.winfo_ismapped():
            self.registo_frame.place_forget()
        else:
            self._atualizar_lista()
            self.registo_frame.place(relx=0.5, rely=0.25, anchor="n", width=560, height=240)
            self.registo_frame.lift()

    def _atualizar_lista(self):
        try:
            if not os.path.exists(self.registo_path):
                self._set_registos_text(["Sem registos hoje."])
                return
            df = pd.read_csv(self.registo_path)
            hoje = df[df["Data"] == str(date.today())]
            linhas = [f"{row['Hora']} - {row['Nome']} ({row['Ação']})" for _, row in hoje.iterrows()]
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

    def _reload_students_local(self):
        self.students = load_students_from_file(self.STUDENTS_FILE)
        reload_students_in_checkin()  # keep checkin.py’s in-memory copy in sync

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

            self.students[novo_id] = [nome, email1, email2]
            self._guardar_students()
            self._reload_students_local()

            try:
                caminho_qr = gerar_qr_para_id(novo_id, nome)
            except Exception as e:
                messagebox.showerror("Erro", f"Não foi possível gerar o QR:\n{e}")
                return

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
        resultado = log_checkin(student_id)
        if resultado:
            nome, tipo = resultado
            self._mostrar_feedback(f"{tipo} registada:\n{nome}", sucesso=True)
            self._show_last_read(nome, student_id, True)
        else:
            self._mostrar_feedback("Registo ignorado (duplicado)", sucesso=False)
            self._show_last_read("Duplicado", student_id, False)
        self._atualizar_lista()

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
                            if s in self.students:
                                self._registar(s)
                                self._show_last_read(self.students[s][0], s, True)
                            else:
                                self._mostrar_feedback("QR not recognized!", sucesso=False)
                                self._show_last_read("QR not recognized", s, False)

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
