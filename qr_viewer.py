# qr_viewer.py
import io
import os
from PIL import Image, ImageTk
import tkinter as tk
from tkinter import messagebox

def fetch_qr_bytes(cursor, student_number: int) -> bytes | None:
    cursor.execute("SELECT qr_code FROM students WHERE student_number=%s", (student_number,))
    row = cursor.fetchone()
    return row[0] if row and row[0] else None

def open_qr_window(db_connection, student_number: int, title_prefix: str = "QR do aluno"):
    try:
        with db_connection.cursor() as cur:
            data = fetch_qr_bytes(cur, student_number)
    except Exception as e:
        messagebox.showerror("Erro", f"Falha ao obter QR da BD:\n{e}")
        return

    if not data:
        messagebox.showinfo("Sem QR", "Não existe QR guardado na base de dados para este aluno.")
        return

    # Criar janela
    win = tk.Toplevel()
    win.title(f"{title_prefix} {student_number}")
    win.geometry("+100+80")  # posição confortável no ecrã

    # Converter bytes → imagem
    try:
        img = Image.open(io.BytesIO(data))
    except Exception as e:
        messagebox.showerror("Erro", f"Dados de imagem inválidos:\n{e}")
        win.destroy()
        return

    # Reduzir se for muito grande (para caber bem)
    MAX_SIDE = 600
    w, h = img.size
    scale = min(1.0, MAX_SIDE / max(w, h))
    if scale < 1.0:
        img = img.resize((int(w*scale), int(h*scale)), Image.LANCZOS)

    tk_img = ImageTk.PhotoImage(img)

    # Guardar referência para não ser coletada
    win._tk_img = tk_img  # type: ignore[attr-defined]

    lbl = tk.Label(win, image=tk_img)
    lbl.pack(padx=12, pady=12)

    # Botões úteis (Guardar como… / Fechar)
    btns = tk.Frame(win)
    btns.pack(pady=(0, 12))

    def save_as():
        from tkinter.filedialog import asksaveasfilename
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

    tk.Button(btns, text="Guardar como…", command=save_as).pack(side=tk.LEFT, padx=6)
    tk.Button(btns, text="Fechar", command=win.destroy).pack(side=tk.LEFT, padx=6)

    # Foco para a janela
    win.transient(win.master)
    win.grab_set()
    win.focus_set()
