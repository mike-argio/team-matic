"""team-matic outbox: owner-approved sending. Planning is offline; running needs the local debug port."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time

import teams_bridge as bridge
from teams_bridge import exclusive_lock
from teammatic_format import MAX_CELL, MAX_COLS, MAX_ROWS, MAX_TEXT, MAX_TITLE, format_problems, render, squash

ROOT = Path(__file__).resolve().parent
POLICY_KEYS = {"enabled", "allowed_chat_ids", "own_creator", "min_interval_seconds", "max_batch_size"}
MESSAGE_KEYS = {"chat_id", "text", "title", "table", "table_header", "format", "occurrence"}


class NotSent(RuntimeError):
    """The adapter stopped before clicking Send. Nothing was delivered."""


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def policy_check(policy):
    if not isinstance(policy, dict) or set(policy) - {"daily_limit"} != POLICY_KEYS:
        raise ValueError("Policy requires exactly: " + ", ".join(sorted(POLICY_KEYS)) + " (optional: daily_limit)")
    ids = policy["allowed_chat_ids"]
    if not isinstance(ids, list) or not ids or any(not isinstance(x, str) or not x for x in ids):
        raise ValueError("Explicit nonempty allowed_chat_ids required")
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate allowed_chat_ids")
    if type(policy["enabled"]) is not bool or not isinstance(policy["own_creator"], str) or not policy["own_creator"]:
        raise ValueError("Invalid enabled/own_creator")
    for key, minimum in (("min_interval_seconds", 30), ("max_batch_size", 1), ("daily_limit", 1)):
        if key in policy and (type(policy[key]) is not int or policy[key] < minimum):
            raise ValueError(f"{key} must be an integer >= {minimum}")


def normalize_message(value):
    if not isinstance(value, dict) or set(value) - MESSAGE_KEYS:
        raise ValueError("Message accepts only: " + ", ".join(sorted(MESSAGE_KEYS)))
    out = {}
    for key in ("chat_id", "text", "title", "occurrence"):
        v = value.get(key, "")
        if not isinstance(v, str) or "\r" in v or "\x00" in v:
            raise ValueError(f"Invalid {key}: use UTF-8 and LF newlines")
        out[key] = v.strip()
    if not out["chat_id"] or not out["text"] or len(out["text"]) > MAX_TEXT:
        raise ValueError(f"chat_id and text (1..{MAX_TEXT} characters) required")
    if "\n" in out["title"] or len(out["title"]) > MAX_TITLE:
        raise ValueError(f"title must be a single line, max {MAX_TITLE} characters")
    if "\n" in out["occurrence"] or len(out["occurrence"]) > 64:
        raise ValueError("occurrence must be a single line, max 64 characters")
    out["format"] = value.get("format", "markdown")
    if out["format"] not in ("markdown", "plain"):
        raise ValueError("format must be markdown or plain")
    out["table_header"] = value.get("table_header", True)
    if type(out["table_header"]) is not bool:
        raise ValueError("table_header must be true or false")
    table = value.get("table", [])
    if not isinstance(table, list) or len(table) > MAX_ROWS:
        raise ValueError(f"table must have <={MAX_ROWS} rows")
    if table:
        width = len(table[0]) if isinstance(table[0], list) else 0
        if not 1 <= width <= MAX_COLS:
            raise ValueError(f"table must have 1..{MAX_COLS} columns")
        for row in table:
            if not isinstance(row, list) or len(row) != width:
                raise ValueError("table rows must have equal width")
            if any(not isinstance(x, str) or not x.strip() or len(x) > MAX_CELL
                   or any(c in x for c in "\r\n\x00\t") for x in row):
                raise ValueError(f"table cells: nonempty single-line strings, max {MAX_CELL} characters")
    out["table"] = [[x.strip() for x in row] for row in table]
    render(out)  # Reject content the formatter cannot compose, before the owner sees a plan.
    return out


def expand(document):
    """Accept {messages: [...]} or a mail-merge {template: {...}, recipients: [...]}."""
    if isinstance(document, dict) and set(document) == {"messages"} and isinstance(document["messages"], list):
        return document["messages"]
    if (not isinstance(document, dict) or set(document) != {"template", "recipients"}
            or not isinstance(document["template"], dict) or not isinstance(document["recipients"], list)
            or "chat_id" in document["template"]):
        raise ValueError("Input must be {messages: [...]} or {template: {...without chat_id}, recipients: [...]}")
    messages = []
    for recipient in document["recipients"]:
        if (not isinstance(recipient, dict) or set(recipient) - {"chat_id", "vars"}
                or not isinstance(recipient.get("vars", {}), dict)):
            raise ValueError("Each recipient is {chat_id, vars: {name: value}}")
        values = recipient.get("vars", {})

        def fill(text):
            def one(match):
                if match[1] not in values or isinstance(values[match[1]], (dict, list)):
                    raise ValueError(f"Missing template variable '{match[1]}' for {recipient.get('chat_id')}")
                return str(values[match[1]])
            return re.sub(r"\{\{\s*(\w+)\s*\}\}", one, text)

        message = {}
        for key, item in document["template"].items():
            if isinstance(item, str):
                message[key] = fill(item)
            elif key == "table" and isinstance(item, list):
                message[key] = [[fill(c) if isinstance(c, str) else c for c in row] if isinstance(row, list) else row
                                for row in item]
            else:
                message[key] = item
        messages.append({**message, "chat_id": recipient.get("chat_id", "")})
    return messages


def expected_text(message):
    return render(message)["plain"]


def verify_delivery(payload, raw):
    """Proof that this exact text was persisted. Formatting is reported separately."""
    if squash(bridge.strip_html(raw)) != squash(expected_text(payload)):
        raise RuntimeError("Persisted message text mismatch")


def verify_content(payload, raw):
    verify_delivery(payload, raw)
    missing = format_problems(render(payload), raw)
    if missing:
        raise RuntimeError("Persisted formatting missing: " + ", ".join(missing))


class Outbox:
    def __init__(self, path=None, cache=None):
        self.path = Path(path or bridge.DATA / "outbox.sqlite3").resolve()
        self.cache = Path(cache or bridge.DB).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS batches(
          id TEXT PRIMARY KEY, policy TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs(
          id INTEGER PRIMARY KEY, batch TEXT NOT NULL, chat_id TEXT NOT NULL,
          title TEXT NOT NULL, payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'pending', attempted REAL,
          message_id TEXT, error TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS ix_outbox_dedup
          ON jobs(chat_id,payload_hash) WHERE state!='cancelled';
        """)

    def close(self):
        self.db.close()

    def plan(self, document, policy):
        policy_check(policy)
        if not policy["enabled"]:
            raise ValueError("Sending disabled: owner must enable policy before creating a queued plan")
        messages = [normalize_message(x) for x in expand(document)]
        if not 1 <= len(messages) <= policy["max_batch_size"]:
            raise ValueError("Batch size exceeds policy or is empty")
        if any(m["chat_id"] not in policy["allowed_chat_ids"] for m in messages):
            raise ValueError("Recipient not allowlisted")
        with contextlib.closing(sqlite3.connect(self.cache.as_uri() + "?mode=ro", uri=True)) as cache:
            titles = dict(cache.execute("SELECT chat_id,title FROM chats"))
        if any(m["chat_id"] not in titles for m in messages):
            raise ValueError("Unknown chat_id: sync/list first")
        # Displayed recipient labels are part of the approved digest.
        batch = digest({"messages": messages, "policy": policy,
                        "titles": [titles[m["chat_id"]] for m in messages]})
        if self.db.execute("SELECT 1 FROM batches WHERE id=?", (batch,)).fetchone():
            return self.show(batch)
        try:
            with self.db:
                self.db.execute("INSERT INTO batches VALUES(?,?,?)", (batch, canonical(policy), time.time()))
                for m in messages:
                    self.db.execute("""INSERT INTO jobs(batch,chat_id,title,payload,payload_hash)
                      VALUES(?,?,?,?,?)""", (batch, m["chat_id"], titles[m["chat_id"]], canonical(m),
                                            digest({k: v for k, v in m.items() if k != "chat_id"})))
        except sqlite3.IntegrityError:
            raise ValueError("Duplicate: the same content is already queued or sent to one of these chats. "
                             "For an intended repeat set a distinct `occurrence`, e.g. the date.") from None
        return self.show(batch)

    def show(self, batch):
        row = self.db.execute("SELECT * FROM batches WHERE id=?", (batch,)).fetchone()
        if not row:
            raise ValueError("Unknown batch")
        jobs = []
        for j in self.db.execute("SELECT * FROM jobs WHERE batch=? ORDER BY id", (batch,)):
            item = dict(j)
            item["payload"] = json.loads(item["payload"])
            if item["chat_id"] != item["payload"]["chat_id"]:
                raise ValueError("Stored recipient mismatch")
            jobs.append(item)
        policy = json.loads(row["policy"])
        if digest({"messages": [j["payload"] for j in jobs], "policy": policy,
                   "titles": [j["title"] for j in jobs]}) != batch:
            raise ValueError("Stored plan changed: approval digest mismatch")
        for item in jobs:
            item["preview"] = expected_text(item["payload"])
        return {"batch": batch, "confirm": batch, "policy": policy, "jobs": jobs}

    def cancel(self, batch):
        with exclusive_lock(self.path.parent / ".send.lock"):
            self.show(batch)
            with self.db:
                self.db.execute("UPDATE jobs SET state='cancelled' WHERE batch=? AND state IN ('pending','drafted')",
                                (batch,))
        return self.show(batch)

    def abandon(self, job_id):
        """Owner decision after checking Teams by hand: this unresolved job was not delivered."""
        with exclusive_lock(self.path.parent / ".send.lock"):
            job = self.db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job or job["state"] not in ("attempting", "uncertain"):
                raise ValueError("Job must be attempting or uncertain")
            with self.db:
                self.db.execute("UPDATE jobs SET state='cancelled',error=? WHERE id=?",
                                ("abandoned by owner after manual check: not delivered", job_id))
        return {"ok": True, "job": job_id, "state": "cancelled"}

    def reconcile(self, job_id, message_id):
        """Record proven delivery after an unresolved or drafted job; never resets a job for retry."""
        with exclusive_lock(self.path.parent / ".send.lock"):
            job = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job or job["state"] not in ("attempting", "uncertain", "drafted"):
                raise ValueError("Job must be attempting, uncertain or drafted")
            policy = self.show(job["batch"])["policy"]
            with contextlib.closing(sqlite3.connect(self.cache.as_uri() + "?mode=ro", uri=True)) as cache:
                cache.row_factory = sqlite3.Row
                m = cache.execute("SELECT * FROM messages WHERE chat_id=? AND message_id=?",
                                  (job["chat_id"], message_id)).fetchone()
            payload = json.loads(job["payload"])
            if (not m or m["deleted"] or m["direction"] != "outgoing" or m["creator"] != policy["own_creator"]
                    or m["time_ms"] < (job["attempted"] - 5) * 1000
                    or squash(bridge.strip_html(m["html"])) != squash(expected_text(payload))):
                raise ValueError("Message does not prove this delivery; run sync and inspect history")
            with self.db:
                self.db.execute("UPDATE jobs SET state='sent',message_id=?,error=NULL WHERE id=?", (message_id, job_id))
            return {"ok": True, "job": job_id, "message_id": message_id, "state": "sent"}

    def run(self, batch, confirmation, policy, backend, allow_navigation=False, clock=time.time, sleep=time.sleep,
            policy_loader=None, draft_only=False):
        policy_check(policy)
        view = self.show(batch)
        if confirmation != batch:
            raise ValueError("Exact SHA256 confirmation required")
        if canonical(policy) != canonical(view["policy"]) or not policy["enabled"]:
            raise ValueError("Policy changed or sending disabled: create a new plan")
        if any(j["state"] in ("attempting", "uncertain") for j in view["jobs"]):
            raise ValueError("Uncertain send: inspect persisted history manually; NEVER blind retry")
        if any(j["state"] == "cancelled" for j in view["jobs"]):
            raise ValueError("Batch cancelled; create a new plan")
        with exclusive_lock(self.path.parent / ".send.lock"):
            # A different batch may have crashed after clicking send.
            if self.db.execute("SELECT 1 FROM jobs WHERE state IN ('attempting','uncertain')").fetchone():
                raise ValueError("Outbox has unresolved delivery; manual reconciliation required")
            results = []
            for job in self.show(batch)["jobs"]:
                if job["state"] in ("sent", "drafted"):
                    continue
                if job["chat_id"] not in policy["allowed_chat_ids"]:
                    raise ValueError("Recipient revoked")
                if "daily_limit" in policy and self.db.execute(
                        "SELECT COUNT(*) FROM jobs WHERE attempted>? AND state!='pending'",
                        (clock() - 86400,)).fetchone()[0] >= policy["daily_limit"]:
                    raise ValueError("daily_limit reached; remaining messages stay pending")
                last = self.db.execute("SELECT MAX(attempted) FROM jobs").fetchone()[0]
                if last is not None:
                    sleep(max(0, last + policy["min_interval_seconds"] - clock()))
                if policy_loader is not None and canonical(policy_loader()) != canonical(policy):
                    raise ValueError("Policy changed during run; remaining messages not sent")
                with self.db:
                    self.db.execute("UPDATE jobs SET state='attempting',attempted=?,error=NULL WHERE id=?",
                                    (clock(), job["id"]))
                try:
                    response = backend({**job["payload"], **render(job["payload"]),
                                        "own_creator": policy["own_creator"],
                                        "allow_navigation": allow_navigation, "draft_only": draft_only})
                    if draft_only:
                        if response.get("ok") is not True or response.get("drafted") is not True:
                            raise RuntimeError("Draft not confirmed")
                        with self.db:
                            self.db.execute("UPDATE jobs SET state='drafted' WHERE id=?", (job["id"],))
                        results.append({"job": job["id"], "state": "drafted"})
                        break  # One composer holds one draft; the human sends it.
                    m = response.get("message", {})
                    if (response.get("ok") is not True or not m.get("id")
                            or m.get("creator") != policy["own_creator"] or m.get("deleted")):
                        raise RuntimeError("Persisted message verification failed")
                    verify_delivery(job["payload"], m.get("html", ""))
                    missing = format_problems(render(job["payload"]), m.get("html", ""))
                    warning = "delivered, but formatting not found in stored message: " + ", ".join(missing) if missing else None
                    with self.db:
                        self.db.execute("UPDATE jobs SET state='sent',message_id=?,error=? WHERE id=?",
                                        (str(m["id"]), warning, job["id"]))
                    results.append({"job": job["id"], "state": "sent", "message_id": str(m["id"]),
                                    **({"warning": warning} if warning else {})})
                except NotSent as exc:
                    # Stopped before the click: safe to try again after the cause is fixed.
                    with self.db:
                        self.db.execute("UPDATE jobs SET state='pending',error=? WHERE id=?",
                                        (str(exc)[:1000], job["id"]))
                    raise
                except BaseException as exc:
                    with self.db:
                        self.db.execute("UPDATE jobs SET state='uncertain',error=? WHERE id=?",
                                        (str(exc)[:1000], job["id"]))
                    raise
            return {"ok": True, "batch": batch, "results": results}


def live_backend(payload):
    bridge.verify_port_ownership()
    with contextlib.closing(sqlite3.connect(bridge.DB.as_uri() + "?mode=ro", uri=True)) as cache:
        row = cache.execute("""SELECT chat_id,message_id,html FROM messages
          WHERE creator=? AND direction='outgoing' AND deleted=0 AND html!=''
          ORDER BY time_ms DESC LIMIT 1""", (payload["own_creator"],)).fetchone()
    if not row:
        raise NotSent("No cached outgoing message proves own_creator; run sync and check `whoami`")
    payload = {**payload, "anchor": dict(zip(("chat_id", "id", "html"), row))}
    result = subprocess.run(["node", str(ROOT / "scripts" / "send-message.mjs")],
                            input=canonical(payload), capture_output=True, text=True, encoding="utf-8", timeout=120,
                            env={**os.environ, "TEAMS_SEND_APPROVED": "YES"})
    if result.returncode:
        try:
            failure = json.loads(result.stderr.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise RuntimeError(result.stderr.strip() or "Send helper failed") from None
        if failure.get("clicked") is False:
            raise NotSent(failure.get("error", "Stopped before send"))
        raise RuntimeError(failure.get("error", "Send helper failed"))
    return json.loads(result.stdout)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="teammatic", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="Offline validation, queue and full preview; no Teams interaction")
    plan.add_argument("--input", required=True)
    plan.add_argument("--policy", required=True)
    show = sub.add_parser("show", help="Status, content and errors of a batch")
    show.add_argument("--batch", required=True)
    cancel = sub.add_parser("cancel", help="Cancel pending and drafted jobs; keep delivery history")
    cancel.add_argument("--batch", required=True)
    reconcile = sub.add_parser("reconcile", help="Record a delivery proven by synced history; never retries")
    reconcile.add_argument("--job", type=int, required=True)
    reconcile.add_argument("--message-id", required=True)
    abandon = sub.add_parser("abandon", help="Owner only: mark an unresolved job as not delivered after a manual check")
    abandon.add_argument("--job", type=int, required=True)
    run = sub.add_parser("run", help="Send an owner-approved batch through the local Teams client")
    run.add_argument("--batch", required=True)
    run.add_argument("--confirm", required=True, help="Full SHA256 returned by plan; owner approval required")
    run.add_argument("--policy", required=True)
    run.add_argument("--allow-navigation", action="store_true", help="Permit chat switching; may take Windows focus")
    run.add_argument("--draft-only", action="store_true",
                     help="Compose the next message in Teams and stop; a human reviews and presses Send")
    args = parser.parse_args(argv)
    box = None
    try:
        box = Outbox()
        if args.command == "plan":
            out = box.plan(load(args.input), load(args.policy))
        elif args.command == "show":
            out = box.show(args.batch)
        elif args.command == "cancel":
            out = box.cancel(args.batch)
        elif args.command == "reconcile":
            out = box.reconcile(args.job, args.message_id)
        elif args.command == "abandon":
            out = box.abandon(args.job)
        else:
            bridge.verify_port_ownership()  # Obvious port failure must not poison the queue.
            # Do not race a cache reader or another queue worker.
            with exclusive_lock(bridge.DATA / ".sync.lock"):
                out = box.run(args.batch, args.confirm, load(args.policy), live_backend, args.allow_navigation,
                              policy_loader=lambda: load(args.policy), draft_only=args.draft_only)
        print(json.dumps(out, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        if box:
            box.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
