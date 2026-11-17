"""Run a subprocess while monitoring Windows foreground for any Teams focus takeover."""
import argparse, ctypes, json, subprocess, time
from ctypes import wintypes

parser = argparse.ArgumentParser()
parser.add_argument("command", nargs=argparse.REMAINDER)
args = parser.parse_args()
if not args.command:
    raise SystemExit("provide command")
u = ctypes.windll.user32
u.GetForegroundWindow.restype = wintypes.HWND
u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
u.GetWindowThreadProcessId.restype = wintypes.DWORD
def fg():
    handle = u.GetForegroundWindow()
    pid = wintypes.DWORD()
    u.GetWindowThreadProcessId(handle, ctypes.byref(pid))
    return int(handle or 0), int(pid.value)
raw = subprocess.check_output(["powershell.exe","-NoProfile","-Command","Get-Process ms-teams | Select-Object -ExpandProperty Id"], text=True)
team_pids = {int(x.strip()) for x in raw.splitlines() if x.strip().isdigit()}
before=fg()
p = subprocess.Popen(args.command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
events = 0
samples = 0
first_pid = None
start=time.monotonic()
while p.poll() is None and time.monotonic() - start < 90:
    handle, pid=fg()
    samples += 1
    if pid in team_pids:
        events += 1
        if first_pid is None: first_pid=pid
    time.sleep(0.025)
try:
    output,_=p.communicate(timeout=8)
except subprocess.TimeoutExpired:
    p.kill();output,_=p.communicate()
print(output)
print("FOCUS_MONITOR="+json.dumps({"initial":before,"final":fg(),"samples":samples,"teams_focused_samples":events,"teams_focused_pid":first_pid,"returncode":p.returncode}))
