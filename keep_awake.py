# keep_awake.py
# Mantém o sistema acordado mesmo com a sessão bloqueada (ecrã pode apagar).
# Usa ES_AWAYMODE_REQUIRED para continuar a correr em "lock screen".

import ctypes
import threading
import time

ES_CONTINUOUS         = 0x80000000
ES_SYSTEM_REQUIRED    = 0x00000001
ES_AWAYMODE_REQUIRED  = 0x00000040  # permite que o ecrã apague, mas mantém CPU/dispositivos

# combinação: manter ativo continuamente, sem suspender; permitir away mode
FLAGS = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED

def _tick():
    kernel32 = ctypes.windll.kernel32
    while True:
        # “renova” o estado a cada 30 segundos
        kernel32.SetThreadExecutionState(FLAGS)
        time.sleep(30)

def start():
    t = threading.Thread(target=_tick, name="KeepAwake", daemon=True)
    t.start()
