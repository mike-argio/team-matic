"""team-matic: automate your own Microsoft Teams chats from scripts and AI agents.

One entry point. Every command runs once and exits; nothing is installed as a service.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

import teams_bridge as bridge
import teams_send

ROOT = Path(__file__).resolve().parent
READ = ("sync", "list", "stats", "incoming", "read", "whoami")
SEND = ("plan", "show", "run", "cancel", "reconcile", "abandon")
OWN = ("doctor", "cdp", "init", "install-skill")
DEBUG_VARIABLE = "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"
SKILL = ROOT / "skills" / "team-matic"

USAGE = """team-matic: automate your own Microsoft Teams chats

  Setup      doctor | cdp status|open|close | init | install-skill [--target DIR]
  Read       list | incoming | read --chat ID | stats | whoami   (live; add --offline for a saved copy)
             sync   saves an offline copy (optional)
  Send       plan | show | run | cancel | reconcile | abandon

Start with:  python teammatic.py doctor
Details:     python teammatic.py <command> --help
"""


def listening() -> bool:
    with contextlib.closing(socket.socket()) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", bridge.cdp_port())) == 0


def teams_running() -> bool:
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq ms-teams.exe", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, errors="replace").stdout  # OEM code page output
    return "ms-teams.exe" in out.lower()


def restart_teams(debug: bool) -> None:
    """Restart Teams with or without the debug port. The setting lives only in that one process."""
    launcher = shutil.which("ms-teams.exe")
    if not launcher:
        raise RuntimeError("ms-teams.exe not found on PATH. Install the new Microsoft Teams client for Windows.")
    subprocess.run(["taskkill", "/IM", "ms-teams.exe", "/F"], capture_output=True)
    for _ in range(40):
        if not teams_running() and not listening():
            break
        time.sleep(0.5)
    env = {k: v for k, v in os.environ.items() if k.upper() != DEBUG_VARIABLE}
    if debug:
        env[DEBUG_VARIABLE] = f"--remote-debugging-port={bridge.cdp_port()}"
    subprocess.Popen([launcher], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=subprocess.DETACHED_PROCESS)


def cdp(action: str) -> dict:
    port = bridge.cdp_port()
    if action == "open":
        if not listening():
            restart_teams(debug=True)
            for _ in range(80):
                if listening():
                    break
                time.sleep(0.5)
        bridge.verify_port_ownership()
        return {"ok": True, "port": port, "state": "open",
                "warning": "Any program on this computer can now control your Teams session. "
                           "Run `cdp close` when you are done."}
    if action == "close":
        if listening():
            bridge.verify_port_ownership()  # Never kill Teams to close a port that something else owns.
            restart_teams(debug=False)
            time.sleep(3)
        if listening():
            raise RuntimeError(f"Port {port} is still open. Quit Teams from the tray icon and start it again.")
        return {"ok": True, "port": port, "state": "closed"}
    if not listening():
        return {"ok": True, "port": port, "state": "closed"}
    bridge.verify_port_ownership()
    return {"ok": True, "port": port, "state": "open"}


def doctor() -> dict:
    """Everything a new user needs to know before the first sync, as pass/fail checks with a fix."""
    checks = []

    def check(name, ok, detail, fix=None):
        checks.append({"check": name, "ok": bool(ok), "detail": detail, **({"fix": fix} if fix and not ok else {})})

    check("windows", sys.platform == "win32", sys.platform, "team-matic drives the Windows Teams client.")
    check("python", sys.version_info >= (3, 10), sys.version.split()[0], "Install Python 3.10 or newer.")
    node = shutil.which("node")
    version = subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip() if node else ""
    major = int(version[1:].split(".")[0]) if version[1:2].isdigit() else 0
    check("node", major >= 22, version or "missing", "Install Node.js 22 or newer (built-in WebSocket).")
    check("teams_client", shutil.which("ms-teams.exe"), shutil.which("ms-teams.exe") or "missing",
          "Install the new Microsoft Teams client for Windows.")
    check("teams_running", teams_running(), "ms-teams.exe", "Start Teams and sign in.")

    port_open = listening()
    owned = False
    if port_open:
        with contextlib.suppress(Exception):
            bridge.verify_port_ownership()
            owned = True
    check("debug_port", port_open and owned,
          "open, owned by Teams" if owned else "open, NOT owned by Teams" if port_open else "closed",
          "Needed to read live and to send: `python teammatic.py cdp open` (restarts Teams). "
          "Without it only `--offline` reads of a copy saved by `sync` work.")
    if owned and node:
        result = subprocess.run([node, str(ROOT / "scripts" / "probe.mjs")], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=60)
        if result.returncode:
            check("teams_ui", False, result.stderr.strip()[-300:], "Open the Chat view in the main Teams window.")
        else:
            seen = json.loads(result.stdout)
            fields = seen.get("record_fields") or {}
            check("teams_ui", seen["chat_rows"] > 0 and seen["message_caches"] == 1, seen,
                  "Open the Chat view; sign in to exactly one account.")
            check("message_format", fields.get("creator") == "string" and fields.get("content") == "string",
                  fields, "This Teams version stores messages differently; please open an issue with this output.")
            check("composer", seen["editors"] == 1 and seen["send_buttons"] >= 1 and seen["selected_rows"] == 1,
                  {k: seen[k] for k in ("editors", "send_buttons", "selected_rows")},
                  "Needed only for send: select one chat so its message box is visible.")

    own = bridge.whoami()["own_creator"]
    check("account", own, own or "not detected yet",
          "Read once with the port open (for example `list`), or set it with `whoami --set <creator>`.")
    check("offline_copy", True, str(bridge.DB) if bridge.DB.is_file() else "none (reads are live; `sync` saves a copy)")

    for name in (".sync.lock", ".send.lock"):
        path = bridge.DATA / name
        if path.exists():
            owner = bridge.lock_owner(path)
            alive = owner is not None and bridge.process_alive(owner)
            check(name, False, f"held by pid {owner}, {'running' if alive else 'not running'}",
                  "Wait for that operation." if alive else "Stale; the next command replaces it automatically.")
    outbox = bridge.DATA / "outbox.sqlite3"
    if outbox.is_file():
        with contextlib.closing(sqlite3.connect(outbox.as_uri() + "?mode=ro", uri=True)) as db:
            stuck = db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('attempting','uncertain')").fetchone()[0]
        check("outbox", stuck == 0, f"{stuck} unresolved deliveries",
              "Check the chat in Teams, then `sync` and `reconcile --job ID --message-id ID`, or `abandon --job ID`.")

    blocking = [c["check"] for c in checks if not c["ok"] and c["check"] not in ("debug_port", "composer", "account")]
    return {"ok": not blocking, "data_dir": str(bridge.DATA), "checks": checks}


def init() -> dict:
    """Write a disabled send policy for the owner to review. Never overwrites."""
    path = bridge.DATA / "send-policy.json"
    if path.exists():
        return {"ok": True, "policy": str(path), "created": False}
    own = bridge.whoami()["own_creator"]
    policy = {
        "enabled": False,
        "allowed_chat_ids": ["<chat_id from: python teammatic.py list>"],
        "own_creator": own or "<port open, then: python teammatic.py whoami>",
        "min_interval_seconds": 30,
        "max_batch_size": 10,
        "daily_limit": 50,
        "agent_may_send": False,
        "allow_navigation": False,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(policy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "policy": str(path), "created": True,
            "next": "You (not an agent) list the chats you allow and set enabled to true."}


def install_skill(target: str, update: bool) -> dict:
    """Copy only the runtime and skill, never private state, to persistent locations."""
    destination = Path(target).expanduser().resolve() / "team-matic"
    if destination.exists() and not update:
        raise RuntimeError(f"{destination} already exists. Inspect it, then pass --update to replace the skill files.")
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    engine = (local / "team-matic" / "engine").resolve()
    runtime = ("teammatic.py", "teammatic_format.py", "teams_bridge.py", "teams_send.py",
               "scripts/cdp-client.mjs", "scripts/collect-cache.mjs", "scripts/probe.mjs",
               "scripts/send-message.mjs", "scripts/focus-monitor.py", "skills/team-matic/SKILL.md",
               "skills/team-matic/scripts/run.py")
    if engine != ROOT:
        engine.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=engine) as staging:
            for name in runtime:
                staged = Path(staging) / name
                staged.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / name, staged)
            for name in runtime:
                final = engine / name
                final.parent.mkdir(parents=True, exist_ok=True)
                (Path(staging) / name).replace(final)
    (destination / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SKILL / "SKILL.md", destination / "SKILL.md")
    shutil.copyfile(SKILL / "scripts" / "run.py", destination / "scripts" / "run.py")
    (destination / "engine_root.txt").write_text(str(engine) + "\n", encoding="utf-8")
    return {"ok": True, "skill": str(destination), "engine": str(engine)}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    if argv[0] in READ:
        return bridge.main(argv)
    if argv[0] in SEND:
        return teams_send.main(argv)
    parser = argparse.ArgumentParser(prog="teammatic")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Check this computer and Teams version; says what to fix")
    port = sub.add_parser("cdp", help="Local debug port of Teams: status, open (restarts Teams), close")
    port.add_argument("action", choices=("status", "open", "close"))
    sub.add_parser("init", help="Create a disabled send policy for the owner to edit")
    skill = sub.add_parser("install-skill", help="Install the agent skill into a skills directory")
    skill.add_argument("--target", default="~/.claude/skills", help="Skills directory (default: ~/.claude/skills)")
    skill.add_argument("--update", action="store_true", help="Replace an existing team-matic skill")
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            out = doctor()
        elif args.command == "cdp":
            out = cdp(args.action)
        elif args.command == "init":
            out = init()
        else:
            out = install_skill(args.target, args.update)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if out.get("ok") else 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
