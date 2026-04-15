from db import log_event
from datetime import datetime
from zoneinfo import ZoneInfo

student_number = 1071
ts = datetime.now(ZoneInfo("Europe/Lisbon")).replace(tzinfo=None)

print("Timestamp que vai ser enviado:", ts)
log_event(student_number, "Entrada", device_name="TESTE LOCAL", ts=ts)
print("Done")