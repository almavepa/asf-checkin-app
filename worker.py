# worker.py
import threading, queue, traceback

_Q = queue.Queue()
_UI_AFTER = None

def init(root, *, worker_name="IOWorker"):
    """Chama isto uma vez (no arranque) com o root do Tk."""
    global _UI_AFTER
    _UI_AFTER = root.after
    t = threading.Thread(target=_loop, name=worker_name, daemon=True)
    t.start()

def _loop():
    while True:
        func, args, kwargs, on_done, on_error = _Q.get()
        try:
            res = func(*args, **kwargs)
            if on_done and _UI_AFTER:
                _UI_AFTER(0, lambda r=res: on_done(r))
        except Exception as e:
            tb = traceback.format_exc()
            print("[worker] erro:", e, "\n", tb)
            if on_error and _UI_AFTER:
                _UI_AFTER(0, lambda: on_error(e))
        finally:
            _Q.task_done()

def enqueue(func, *args, on_done=None, on_error=None, **kwargs):
    """Mete uma função pesada na fila. Nunca bloqueia o UI."""
    _Q.put((func, args, kwargs, on_done, on_error))
