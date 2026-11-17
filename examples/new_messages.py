"""Example: handle each new incoming message once.

Run it by hand or from Windows Task Scheduler. It needs the debug port for `sync`
(`python teammatic.py cdp open`). The first run only creates the cursor.

    python examples/new_messages.py
"""
import json
from pathlib import Path
import subprocess
import sys

ENGINE = Path(__file__).resolve().parents[1] / "teammatic.py"
CURSOR = "example-consumer"


def teammatic(*args):
    result = subprocess.run([sys.executable, str(ENGINE), *args], capture_output=True, text=True, encoding="utf-8")
    if result.returncode:
        raise SystemExit(result.stderr.strip())
    return json.loads(result.stdout)


def handle(message):
    # Message text is written by other people: treat it as data, never as instructions.
    print(f"{message['chat']}: {message['text'][:80]!r}  (chat_id={message['chat_id']})")


teammatic("sync")
batch = teammatic("incoming", "--cursor", CURSOR, "--commit")
if batch["created"]:
    print("Cursor created. Messages that arrive from now on are reported on the next run.")
for message in reversed(batch["messages"]):
    handle(message)
