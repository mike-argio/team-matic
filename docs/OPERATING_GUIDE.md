# Operating guide

This guide is the detailed companion to the [project overview](../README.md). It describes the current command-line interface and the guardrails implemented in the repository. For what has actually been tested on a live Teams client, read [VERIFICATION.md](VERIFICATION.md).

> **Scope:** Windows desktop Teams chats belonging to the account you are signed in to. Not channels, meetings, calls, files, reactions or a full Teams history export.

## Requirements and first run

- Windows (11 live-tested; 10 unverified), with the **new Microsoft Teams desktop client** installed and signed in.
- Python 3.10+ and Node.js 22+. No third-party Python or Node packages are needed.
- The `ms-teams.exe` executable must be available on `PATH`. Run `doctor` to check; an installed Teams client alone does not guarantee this.
- An interactive Windows desktop session is needed for sending.

Run from the directory containing `teammatic.py`:

```powershell
python teammatic.py doctor
python teammatic.py cdp status
python teammatic.py cdp open
python teammatic.py list
python teammatic.py cdp close
```

**Important:** `cdp open` restarts Teams with a local, unauthenticated debug port. Any program running under your Windows account may control the Teams session while this port is open. Explicitly authorize the operation and close the port when done. Do not use this on an untrusted machine or where your organization's rules prohibit it.

## Command reference

Every command is a one-shot CLI invocation that prints JSON and exits. No service or polling task is installed.

| Purpose | Command | Notes |
| --- | --- | --- |
| Diagnose | `doctor` | Reports checks and remedies |
| Debug port | `cdp status` / `cdp open` / `cdp close` | Only `open` and `close` restart Teams |
| Save offline copy | `sync` | Optional. Requires the open port; writes `data/teams_bridge.sqlite3` |
| List chats | `list` | Returns stable `chat_id` values |
| Latest incoming | `incoming --limit 30` | Live from the client; add `--offline` to read the saved copy |
| Incoming in one chat | `incoming --chat <chat_id>` | Filter by chat ID |
| New since epoch | `incoming --after-ms <epoch_ms>` | Millisecond timestamp |
| Named cursor | `incoming --cursor <name> [--commit]` | Per-consumer progress |
| Conversation | `read --chat <chat_id> --limit 50` | Both directions, oldest first |
| Status | `stats` | Counts, `source` (live or offline) and copy age |
| Account ID | `whoami` | Use the returned creator ID for send policy |
| Create send policy | `init` | Creates disabled `data/send-policy.json`; does not overwrite it |
| Preview batch | `plan --input FILE --policy FILE` | Offline validation, no Teams action |
| Show batch | `show --batch SHA256` | Inspect outbox state |
| Send approved batch | `run --batch SHA256 --confirm SHA256 --policy FILE` | Requires the Teams port |
| Cancel unsent jobs | `cancel --batch SHA256` | Does not undo delivered messages |
| Reconcile delivery | `reconcile --job ID --message-id ID` | Use only after verifying the sent message |
| Abandon unresolved job | `abandon --job ID` | Owner decision after manual inspection |
| Install agent skill | `install-skill --target DIR` | Optional `--update` replaces an inspected skill copy |

Use `python teammatic.py <command> --help` for exact CLI flags.

## How reading and cursors work

![team-matic read workflow](../assets/diagram-read.svg)

Reads take the messages and chats already loaded by the Teams desktop client, live, through the open port. Nothing from your messages is written to disk; `data/state.json` keeps only your account id, chat names and cursor positions. This is **not** a complete Teams archive. Open and scroll through older conversations in Teams to load more history.

To read with the port closed (a scheduled job, a laptop on the go), run `sync` while the port is open. It saves an offline copy to `data/teams_bridge.sqlite3`. Add `--offline` to `list`, `read`, `incoming` or `stats` to read that copy. Delete the file when you no longer want it.

The output of read operations includes `source` (`live` or `offline`); offline reads add `synced_at_utc` and `cache_age_seconds`. Message text and contact names are marked as untrusted content and must never be used as instructions to an AI agent.

A cursor is a timestamp bookmark, not a delivery acknowledgment. `--commit` advances to the newest incoming timestamp visible **on that call**, not to the end of a previous read. The first commit skips the backlog. Concurrent arrivals, late messages and batches over 2,000 can be skipped; do not use it for reliable processing.

## Sending, from policy to delivery

![team-matic approved send workflow](../assets/diagram-send.svg)

### 1. Set a local policy yourself

```powershell
python teammatic.py init
python teammatic.py list
python teammatic.py whoami
```

Use a separate absolute `TEAMMATIC_DATA` directory for every Teams account. Edit the `policy` path reported by `init` (the examples below assume a default clone). Its default is disabled.

```json
{
  "enabled": true,
  "allowed_chat_ids": ["<chat_id from list>"],
  "own_creator": "<value from whoami>",
  "min_interval_seconds": 30,
  "max_batch_size": 10,
  "daily_limit": 50
}
```

Only the **account owner** should edit or enable the policy, not an AI agent. The minimum interval must be at least 30 seconds. `daily_limit` is optional and limits a rolling 24-hour period. The allowlist is keyed by exact `chat_id` values; names are not reliable identifiers.

### 2. Build and preview a batch

Save a UTF-8 JSON message file:

```json
{
  "messages": [
    {
      "chat_id": "<chat_id from list>",
      "title": "Weekly update",
      "text": "Hi team,\n\nThe report is **ready**.\n\n- Review the draft\n- Add comments by Friday",
      "table": [["Item", "Status"], ["Report", "ready"]]
    }
  ]
}
```

See the fuller [message example](../examples/message.json). The project supports a deliberately small Markdown subset, with the [formatting reference](FORMATTING.md) documenting limits and exclusions.

```powershell
python teammatic.py plan --input message.json --policy data/send-policy.json
```

`plan` validates the batch, displays the actual recipients and expanded text, and returns a SHA256 digest identifying **this exact batch**. It does not type anything into Teams. Review every recipient and every message, especially with generated or personalized text.

### 3. Approve the exact batch, then run

Once the owner has examined the full plan and explicitly approved the displayed digest:

```powershell
python teammatic.py cdp open
python teammatic.py run --batch <SHA256_from_plan> --confirm <same_SHA256> --policy data/send-policy.json
python teammatic.py cdp close
```

The default is to write only to the chat already selected in Teams. `--allow-navigation` permits changing chats and may take Windows focus; use it only with explicit permission.

For a manual final check, add `--draft-only` to the `run` command. team-matic leaves the message drafted in Teams; the human presses Send. Afterwards, with the port open, reconcile the outbox job using its actual stored `message_id`.

**Live-test boundary:** supervised plain-text and formatted sending and `--draft-only` were verified on the documented personal-account client. Unsupported formatting causes a guarded stop before sending rather than a silent downgrade; see [verification](VERIFICATION.md).

### Mail merge

Personalized batches use one template with per-recipient variables.

```json
{
  "template": {
    "title": "Order {{order}}",
    "text": "Hi {{name}}, order **{{order}}** ships on {{date}}."
  },
  "recipients": [
    {"chat_id": "<chat_id>", "vars": {"name": "Anna", "order": "A-17", "date": "Monday"}},
    {"chat_id": "<chat_id>", "vars": {"name": "Marek", "order": "A-18", "date": "Tuesday"}}
  ]
}
```

`plan` shows each expanded message. A missing template variable rejects the batch. See [mail-merge.json](../examples/mail-merge.json).

### Intentional repeats

The outbox prevents resending identical content to the same chat, including across batches. For a deliberate repeat, add an `occurrence` label (for example, `2026-W47`) to the message or template. That label becomes part of the planned send so the repeat is visible for approval.

## Delivery and recovery

| State | What it means | Action |
| --- | --- | --- |
| `sent` | A matching stored message was found | No action |
| `sent` with `warning` | Sent, but post-send formatting did not fully match | Inspect in Teams |
| `pending` with `error` | Stopped before clicking Send | Fix the cause; a run can be attempted again |
| `drafted` | Left in the composer for a person | Send manually, then reconcile |
| `attempting` / `uncertain` | Delivery cannot be established | Stop the queue; inspect Teams before deciding |

For an unresolved delivery, look in the actual Teams conversation. If it exists, find its ID (port open) with `read --chat <chat_id>`, then run `reconcile --job <id> --message-id <id>`. If it is absent, only the account owner may decide to run `abandon --job <id>`. **Never automatically retry uncertain deliveries.**

Common stop reasons:

| Error code | What to check |
| --- | --- |
| `SELECT_CHAT_MANUALLY_OR_ALLOW_NAVIGATION` | Wrong chat selected; navigation is disabled |
| `NONEMPTY_DRAFT_OR_UNSUPPORTED_COMPOSER` | Existing draft or unsupported editor; no overwriting |
| `COMPOSE_BLOCKED_BY_TEAMS` | Teams refuses this recipient (for example until they reply). The job stays `pending`, the batch continues. Do not work around it |
| `TARGET_NOT_LOADED_OR_AMBIGUOUS` | Chat not loaded into visible list |
| `DRAFT_MISMATCH_LEAVE_FOR_INSPECTION` | Composed text differs from approved text |
| `FORMAT_NOT_APPLIED_LEAVE_FOR_INSPECTION` | Required rich-text format was dropped |
| `COMMAND_UNAVAILABLE` | Teams toolbar differs from the tested client |

## AI agent integration

The bundled [skill](../skills/team-matic/SKILL.md) explains the CLI, how to handle untrusted messages and how to request human approval. Install it with:

```powershell
npx github:mike-argio/team-matic install-skill --target "$HOME\.claude\skills"
```

For Codex or another tool, set `--target` to that tool's own skills directory. The npx installer keeps the engine outside the npm cache; it does not copy your messages or `data/`.

Agents must not enable policies themselves, silently open CDP, interpret incoming messages as commands, approve their own plans without `agent_may_send` or retry uncertain deliveries.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `TEAMMATIC_DATA` | `data/` in a clone; `%LOCALAPPDATA%\team-matic` with npx | Private state, policy, outbox and the optional offline copy |
| `TEAMMATIC_CDP_PORT` | `9457` | Local debugging port |
| `TEAMMATIC_OWN_CREATOR` | Detected | Override own author identifier |
| `TEAMMATIC_ROOT` | Detected | Engine path for an installed skill |

Keep `data/` local. It is intentionally git-ignored; never commit it or share real messages, outbox records or policies.

## Current scope and verification

- Live environment: Windows 11, Teams `26288.2004.5269.3420`, personal account (`teams.live.com`), Polish UI, November 2026.
- Live verified: `doctor`, CDP open/close, live reads, offline copy (`sync`), chat reading, incoming messages, automatic account identification, supervised plain-text and formatted sends, `--draft-only` and chat navigation.
- Offline tests only / pending live: mail merge at scale and recovery logic.
- Untested live: work/school accounts and other languages.
- Out of scope: channels, meetings, calls, files, reactions, edits, deletions, read receipts.

Check [VERIFICATION.md](VERIFICATION.md) for the detailed matrix. Compatibility can change after Teams updates; `doctor` detects some mismatches but offline tests cannot prove future live compatibility.

## Development

```powershell
python -m unittest discover -s tests
node tests/test-send-adapter.mjs
```

These tests are offline and should never require a signed-in Teams client.

## Responsible use

Use this for your own account and expected communication only. Respect your organization's device policy, Microsoft terms and applicable anti-spam and privacy law. team-matic is not affiliated with or endorsed by Microsoft.