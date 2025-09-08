# toast.py
import tkinter as tk
from tkinter import ttk

class Toast:
    def __init__(self, root, *, width=320, pad=12, duration=1400, corner="br"):
        self.root = root
        self.duration = duration
        self.width = width
        self.pad = pad
        self.corner = corner  # 'br', 'tr', 'bl', 'tl'

        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.overrideredirect(1)
        self.win.attributes("-topmost", True)

        self.frame = ttk.Frame(self.win, padding=10)
        self.frame.pack(fill="both", expand=True)

        self.label = ttk.Label(self.frame, text="", anchor="w", justify="left", wraplength=width-24)
        self.label.pack(fill="both", expand=True)

        style = ttk.Style(self.root)
        style.configure("Toast.TFrame", background="#222")
        style.configure("Toast.TLabel", background="#222", foreground="#fff", font=("Segoe UI", 10))
        self.frame.configure(style="Toast.TFrame")
        self.label.configure(style="Toast.TLabel")

        self.hide_id = None

    def _place(self):
        self.win.update_idletasks()
        w = self.win.winfo_width()
        h = self.win.winfo_height()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = self.pad if "l" in self.corner else sw - w - self.pad
        y = self.pad if "t" in self.corner else sh - h - self.pad
        self.win.geometry(f"+{x}+{y}")

    def show(self, msg, duration=None):
        if self.hide_id:
            self.root.after_cancel(self.hide_id)
            self.hide_id = None
        self.label.configure(text=msg)
        self.win.deiconify()
        self._place()
        ms = self.duration if duration is None else duration
        self.hide_id = self.root.after(ms, self.hide)

    def hide(self):
        self.win.withdraw()
        self.hide_id = None


class ToastManager:
    """Fila simples para mostrar toasts em sequência, sem bloquear o Tk."""
    def __init__(self, root):
        self.toast = Toast(root)
        self.queue = []
        self.showing = False
        self.root = root

    def push(self, msg, duration=1200):
        self.queue.append((msg, duration))
        if not self.showing:
            self._next()

    def _next(self):
        if not self.queue:
            self.showing = False
            return
        self.showing = True
        msg, dur = self.queue.pop(0)
        self.toast.show(msg, dur)
        self.root.after(dur + 120, self._next)
