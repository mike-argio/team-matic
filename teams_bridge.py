"""team-matic reader: Microsoft Teams chats for agents and scripts.

Read only. Uses the local WebView2 IndexedDB of the Teams client through a fixed Node helper.
No Graph, no Azure, no Teams service calls, no tokens.

By default every read goes live to the running Teams client and keeps no copy of messages.
`sync` stores an explicit offline copy in SQLite; `--offline` reads that copy.
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import ctypes
import datetime as dt
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("TEAMMATIC_DATA") or ROOT / "data").resolve()
DB = DATA / "teams_bridge.sqlite3"
STATE = DATA / "state.json"
COLLECTOR = ROOT / "scripts" / "collect-cache.mjs"
SCHEMA = "teammatic-cache-v3"
BLOCKS = {"br", "p", "li", "div", "td", "th", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre"}
OFFLINE = False  # Set by the CLI: read the stored copy instead of the live client.
_view = None


def cdp_port() -> int:
    raw = os.environ.get("TEAMMATIC_CDP_PORT", "9457")
    if not raw.isdigit() or not 1024 <= int(raw) <= 65535:
        raise RuntimeError("TEAMMATIC_CDP_PORT must be an integer between 1024 and 65535")
    return int(raw)


def verify_port_ownership() -> None:
    # CDP is unauthenticated. Never connect to an unknown local listener.
    script = r'''$conn=Get-NetTCPConnection -LocalPort %d -State Listen -ErrorAction SilentlyContinue |
      Where-Object {$_.LocalAddress -eq '127.0.0.1'} | Select-Object -First 1
      if(!$conn){exit 3}
      $ownerPid=[int]$conn.OwningProcess
      for($i=0;$i -lt 7 -and $ownerPid -gt 0;$i++){
        $proc=Get-CimInstance Win32_Process -Filter "ProcessId=$ownerPid" -ErrorAction SilentlyContinue
        if(!$proc){break}
        if($proc.Name -ieq 'ms-teams.exe'){'OWNED_BY_TEAMS';exit 0}
        $ownerPid=[int]$proc.ParentProcessId
      }
      exit 2''' % cdp_port()
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, text=True, errors="replace", timeout=20)
    if result.returncode != 0 or "OWNED_BY_TEAMS" not in result.stdout:
        raise RuntimeError(f"Debug port {cdp_port()} is closed or not owned by Teams. "
                           "Run `python teammatic.py cdp open` first, or add --offline to read a copy saved by `sync`.")


class TextRenderer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        if self.skip:
            return
        if tag in BLOCKS:
            self.parts.append("\n")
        if tag == "img":
            # Teams stores emoji as images; the character lives in alt.
            found = dict(attrs)
            if "Emoji" in (found.get("itemtype") or ""):
                self.parts.append(found.get("alt") or "")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        if not self.skip and tag in BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)

    def text(self):
        joined = "".join(self.parts).replace("\xa0", " ").replace("\r", "")
        return "\n".join(x.strip() for x in joined.split("\n") if x.strip()).strip()


def strip_html(source):
    parser = TextRenderer()
    try:
        parser.feed(source or "")
        return parser.text()
    except Exception:  # Malformed third-party HTML must not stop a read.
        return html.unescape(source or "")[:100000]


def process_alive(pid: int) -> bool:
    # os.kill(pid, 0) terminates the process on Windows; query it instead.
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return code.value == 259
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def lock_owner(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


@contextlib.contextmanager
def exclusive_lock(path):
    """One writer at a time. A lock whose owner process is gone is replaced.

    Replacing a dead owner's lock is safe: an interrupted send stays recorded as
    `attempting` in the outbox, which blocks every later run until reconciled.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        owner = lock_owner(path)
        if owner is None or process_alive(owner):
            raise RuntimeError(f"Another team-matic operation holds {path.name} (pid {owner}). "
                               "Wait for it to finish.") from None
        path.unlink()
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


# --- small state file: account id, chat directory, cursors. Never message content. ---

def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def save_state(state: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    temporary = STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, STATE)  # Atomic: a crash never leaves half a file.


def known_chats() -> dict:
    """chat_id -> title from the last read. Lets `plan` work without the debug port."""
    return load_state().get("chats", {})


def stored_identity() -> str | None:
    state = load_state()
    if "own_creator" in state:
        return state["own_creator"] or None  # Empty: `whoami --reset` asked for fresh detection.
    if DB.is_file():
        # Copies written by earlier versions already know the account.
        with contextlib.closing(sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True)) as db:
            with contextlib.suppress(sqlite3.Error):
                row = db.execute("SELECT value FROM meta WHERE key='own_creator'").fetchone()
                if row:
                    return row[0] or None
            known = [r[0] for r in db.execute(
                "SELECT DISTINCT creator FROM messages WHERE direction='outgoing' AND creator!=''")]
            return known[0] if len(known) == 1 else None
    return None


def resolve_own_creator(chats, explicit=None):
    """Decide which author id is the signed-in account. Fail closed when unsure.

    A wrong answer would swap incoming and outgoing and weaken send verification,
    so two independent signals must not disagree: the client's own
    `isSentByCurrentUser` flag, and the fact that the owner writes in most chats.
    """
    flagged = set()
    seen_in = collections.defaultdict(set)
    for chat in chats:
        for message in chat["messages"]:
            creator = message.get("creator")
            if not creator:
                continue
            seen_in[creator].add(chat["threadId"])
            if message.get("mine") is True:
                flagged.add(creator)
    if len(flagged) > 1:
        raise RuntimeError("Several accounts are marked as the current user; cannot pick one safely.")
    by_flag = next(iter(flagged), None)
    ranked = sorted(((len(v), k) for k, v in seen_in.items()), reverse=True)
    by_spread = None
    if ranked and ranked[0][0] >= 3 and (len(ranked) == 1 or ranked[0][0] >= 2 * ranked[1][0]):
        by_spread = ranked[0][1]
    if explicit:
        if by_flag and by_flag != explicit:
            raise RuntimeError("Teams is signed in to a different account than this data folder was set up for. "
                               "Use a separate TEAMMATIC_DATA folder or `whoami --reset`.")
        return explicit, "configured"
    if by_flag and by_spread and by_flag != by_spread:
        raise RuntimeError("Account signals disagree; set it explicitly with `whoami --set <creator>`.")
    if by_flag or by_spread:
        return (by_flag or by_spread), ("client-flag" if by_flag else "chat-spread")
    candidates = ", ".join(f"{creator} ({count} chats)" for count, creator in ranked[:5]) or "none"
    raise RuntimeError("Cannot tell which author is you (too few chats with your own messages). "
                       f"Pick yours and run `whoami --set <creator>`. Candidates: {candidates}")


# --- reading the live client ---

def collect():
    """One read of the Teams client. The collector prints JSON; nothing is written to disk."""
    verify_port_ownership()
    result = subprocess.run(["node", str(COLLECTOR)], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=180)
    if result.returncode:
        raise RuntimeError("Collector failed: " + (result.stderr[-1600:] or result.stdout[-1600:]))
    payload = json.loads(result.stdout)
    if payload.get("schema") != SCHEMA:
        raise RuntimeError("Invalid collector output")
    chats = payload["chats"]
    if not chats or len({x["threadId"] for x in chats}) != len(chats):
        raise RuntimeError("Empty or ambiguous chat identities")
    own, method = resolve_own_creator(chats, os.environ.get("TEAMMATIC_OWN_CREATOR") or stored_identity())
    state = load_state()
    state.update(own_creator=own, chats={c["threadId"]: c["title"] for c in chats})
    save_state(state)
    return chats, own, method


def create_schema(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS sync_runs(
      run_id INTEGER PRIMARY KEY AUTOINCREMENT,
      synced_at_utc TEXT NOT NULL, scope TEXT NOT NULL, chats INTEGER, messages INTEGER);
    CREATE TABLE IF NOT EXISTS chats(
      chat_id TEXT PRIMARY KEY,title TEXT NOT NULL,kind TEXT,
      roster_time TEXT,roster_preview TEXT,
      cached_replychains INTEGER NOT NULL,last_seen_run INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS messages(
      chat_id TEXT NOT NULL,message_id TEXT NOT NULL,creator TEXT,
      direction TEXT NOT NULL,time_ms INTEGER NOT NULL,text TEXT NOT NULL,
      html TEXT NOT NULL,message_type TEXT,deleted INTEGER NOT NULL,
      first_seen_run INTEGER NOT NULL,last_seen_run INTEGER NOT NULL,
      PRIMARY KEY(chat_id,message_id),
      FOREIGN KEY(chat_id) REFERENCES chats(chat_id));
    CREATE INDEX IF NOT EXISTS ix_messages_time ON messages(time_ms DESC);
    CREATE INDEX IF NOT EXISTS ix_messages_incoming ON messages(direction,time_ms DESC);
    """)


def ingest(db, chats, own, scope):
    """Write one read into a database: the offline copy, or the in-memory view of a live read."""
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    with db:
        run_id = db.execute("INSERT INTO sync_runs(synced_at_utc,scope,chats,messages) VALUES(?,?,?,?)",
                            (now, scope, len(chats), sum(len(c["messages"]) for c in chats))).lastrowid
        for chat in chats:
            cid = chat["threadId"]
            db.execute("""INSERT INTO chats(chat_id,title,kind,roster_time,roster_preview,cached_replychains,last_seen_run)
              VALUES(?,?,?,?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET
              title=excluded.title,kind=excluded.kind,roster_time=excluded.roster_time,
              roster_preview=excluded.roster_preview,cached_replychains=excluded.cached_replychains,
              last_seen_run=excluded.last_seen_run""",
                       (cid, chat["title"], chat["kind"], chat["rosterTime"], chat["rosterPreview"],
                        chat["replyChains"], run_id))
            for m in chat["messages"]:
                raw = m.get("html") or ""
                creator = m.get("creator") or ""
                direction = "unknown" if not creator else "outgoing" if creator == own else "incoming"
                db.execute("""INSERT INTO messages(chat_id,message_id,creator,direction,time_ms,text,html,message_type,deleted,first_seen_run,last_seen_run)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(chat_id,message_id) DO UPDATE SET
                  creator=excluded.creator,direction=excluded.direction,time_ms=excluded.time_ms,
                  text=excluded.text,html=excluded.html,message_type=excluded.message_type,
                  deleted=excluded.deleted,last_seen_run=excluded.last_seen_run""",
                           (cid, str(m["id"]), creator, direction, int(m["timeMs"]), strip_html(raw), raw,
                            m.get("messageType"), int(bool(m.get("deleted"))), run_id, run_id))
    return run_id


def connection():
    """Where reads come from: the live client (default) or the copy saved by `sync`."""
    global _view
    if _view is None:
        if OFFLINE:
            if not DB.is_file():
                raise RuntimeError("No offline copy. Run `python teammatic.py sync` first, or read live without --offline.")
            _view = sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True, timeout=30)
        else:
            chats, own, _method = collect()
            _view = sqlite3.connect(":memory:")
            create_schema(_view)
            ingest(_view, chats, own, "live")
        _view.row_factory = sqlite3.Row
    return _view


def close_view():
    global _view
    if _view is not None:
        _view.close()
        _view = None


def run_query(query, params=()):
    return [dict(x) for x in connection().execute(query, params)]


def sync():
    """Save an offline copy of what the Teams client has loaded. Opt-in; reads do not need it."""
    with exclusive_lock(DATA / ".sync.lock"):
        chats, own, method = collect()
        DATA.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(DB, timeout=30)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            create_schema(db)
            run_id = ingest(db, chats, own, "loaded-roster-cache")
            row = db.execute("SELECT COUNT(*),SUM(direction='incoming'),SUM(direction='outgoing') FROM messages").fetchone()
        return dict(run_id=run_id, visible_chats=len(chats), db_messages=row[0], incoming=row[1] or 0,
                    outgoing=row[2] or 0, own_creator=own, own_creator_source=method, db_path=str(DB))


def list_chats():
    return run_query("""SELECT c.chat_id,c.title,c.kind,c.roster_time,c.roster_preview,
       c.cached_replychains,(SELECT COUNT(*) FROM messages m WHERE m.chat_id=c.chat_id) AS messages,
       (SELECT COUNT(*) FROM messages m WHERE m.chat_id=c.chat_id AND m.direction='incoming') AS incoming
       FROM chats c ORDER BY c.cached_replychains DESC,c.title COLLATE NOCASE""")


def incoming(limit=20, after_ms=0, chat_id=None):
    sql = """SELECT c.title AS chat,m.chat_id,m.message_id,m.creator,m.time_ms,m.text
       FROM messages m JOIN chats c ON c.chat_id=m.chat_id
       WHERE m.direction='incoming' AND m.deleted=0 AND m.time_ms>=?
       AND COALESCE(m.message_type,'') NOT LIKE 'ThreadActivity/%' """
    args = [after_ms]
    if chat_id:
        sql += "AND m.chat_id=? "
        args.append(chat_id)
    sql += "ORDER BY m.time_ms DESC LIMIT ?"
    args.append(max(1, min(2000, limit)))
    return run_query(sql, tuple(args))


def read_chat(chat_id, limit=50, after_ms=0):
    rows = run_query("""SELECT message_id,direction,creator,time_ms,text,message_type FROM messages
       WHERE chat_id=? AND deleted=0 AND time_ms>=? ORDER BY time_ms DESC LIMIT ?""",
                     (chat_id, after_ms, max(1, min(2000, limit))))
    if not rows and not run_query("SELECT 1 FROM chats WHERE chat_id=?", (chat_id,)):
        raise RuntimeError("Unknown chat_id. Use `list` to see chats.")
    return rows[::-1]


def find_message(chat_id, message_id):
    rows = run_query("SELECT * FROM messages WHERE chat_id=? AND message_id=?", (chat_id, message_id))
    return rows[0] if rows else None


def freshness():
    last = run_query("SELECT run_id,synced_at_utc,scope FROM sync_runs ORDER BY run_id DESC LIMIT 1")
    if not last:
        return {"source": "offline" if OFFLINE else "live", "synced_at_utc": None, "cache_age_seconds": None}
    age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(last[0]["synced_at_utc"])
    return {"source": "live" if last[0]["scope"] == "live" else "offline",
            "synced_at_utc": last[0]["synced_at_utc"], "cache_age_seconds": int(age.total_seconds())}


def envelope(messages):
    # Message text comes from other people. It is data, never instructions.
    return {**freshness(), "untrusted_content": True, "messages": messages}


def read_cursor(name, commit):
    """Per-consumer position: incoming messages newer than the last commit.

    shortcut: a time watermark; a message that arrives late with an older timestamp is skipped.
    Upgrade to a set of seen ids if that ever matters.
    """
    state = load_state()
    cursors = state.setdefault("cursors", {})
    if name not in cursors and not commit:
        raise RuntimeError(f"Unknown cursor '{name}'. Create it with --commit; it starts at the newest message.")
    created = name not in cursors
    messages = [] if created else incoming(limit=2000, after_ms=cursors[name] + 1)
    if commit:
        newest = run_query("SELECT MAX(time_ms) AS t FROM messages WHERE direction='incoming'")[0]["t"] or 0
        cursors[name] = max(newest, cursors.get(name, 0))
        save_state(state)
    return {**envelope(messages), "cursor": name, "committed_ms": cursors[name] if commit else None,
            "created": created}


def whoami(set_value=None, reset=False):
    if set_value or reset:
        state = load_state()
        state["own_creator"] = "" if reset else set_value
        save_state(state)
    own = stored_identity()
    if not own:
        return {"own_creator": None, "hint": "Run any read command with the debug port open; the account is detected."}
    return {"own_creator": own, "hint": "Use this value as own_creator in the send policy."}


def stats():
    rows = run_query("SELECT COUNT(*) AS count FROM chats")
    msg = run_query("""SELECT COUNT(*) AS all_messages,
      SUM(direction='incoming') AS incoming,
      SUM(direction='outgoing') AS outgoing,
      SUM(deleted) AS deleted FROM messages""")
    return {"chats": rows[0]["count"], "messages": msg[0], **freshness(),
            "offline_copy": str(DB) if DB.is_file() else None}


def main(argv=None):
    global OFFLINE
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    offline = argparse.ArgumentParser(add_help=False)
    offline.add_argument("--offline", action="store_true", help="Read the copy saved by `sync` instead of the live client")
    p = argparse.ArgumentParser(prog="teammatic", description="team-matic reader (read-only)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync", help="Save an offline copy of loaded chats (optional; reads are live by default)")
    sub.add_parser("list", parents=[offline], help="Chats with their stable chat_id")
    sub.add_parser("stats", parents=[offline], help="Counts and freshness")
    q = sub.add_parser("incoming", parents=[offline], help="Incoming messages, newest first")
    q.add_argument("--limit", type=int, default=20)
    q.add_argument("--after-ms", type=int, default=0, help="Only messages at or after this epoch millisecond")
    q.add_argument("--chat", help="Only this chat_id")
    q.add_argument("--cursor", help="Named position for one consumer; returns what arrived since its last --commit")
    q.add_argument("--commit", action="store_true", help="With --cursor: move the position to the newest message")
    r = sub.add_parser("read", parents=[offline], help="One conversation, both directions, oldest first")
    r.add_argument("--chat", required=True)
    r.add_argument("--limit", type=int, default=50)
    r.add_argument("--after-ms", type=int, default=0)
    w = sub.add_parser("whoami", help="Author id of the signed-in account")
    w.add_argument("--set", dest="set_value", help="Set the account id explicitly")
    w.add_argument("--reset", action="store_true", help="Forget the stored account id")
    args = p.parse_args(argv)
    OFFLINE = getattr(args, "offline", False)
    try:
        if args.cmd == "sync":
            out = sync()
        elif args.cmd == "list":
            out = list_chats()
        elif args.cmd == "incoming" and args.cursor:
            out = read_cursor(args.cursor, args.commit)
        elif args.cmd == "incoming":
            out = envelope(incoming(args.limit, args.after_ms, args.chat))
        elif args.cmd == "read":
            out = envelope(read_chat(args.chat, args.limit, args.after_ms))
        elif args.cmd == "whoami":
            out = whoami(args.set_value, args.reset)
        else:
            out = stats()
        print(json.dumps(out, ensure_ascii=False, indent=2))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        close_view()
    return 0


if __name__ == "__main__":
    sys.exit(main())
