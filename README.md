# team-matic

Automate your own Microsoft Teams chats from scripts and AI agents. No Azure app, no Graph API,
no admin consent, no bot registration: team-matic drives the Teams desktop client you are already
signed in to, on your own Windows computer.

- **Read**: list chats, read conversations, get each new incoming message exactly once.
- **Send**: formatted messages, tables, personalized batches (mail merge), with a preview you approve first.
- **Agent ready**: one JSON command line and a skill file for Claude Code, Codex, OpenCode and similar tools.

Zero dependencies beyond Python and Node.js. Clone and run.

> Not affiliated with or endorsed by Microsoft. "Microsoft Teams" is a trademark of Microsoft Corporation.

## How it works

```text
your script / AI agent
        |  python teammatic.py <command>      JSON in, JSON out, runs once and exits
        v
   team-matic  ---- local SQLite cache (data/)      read commands work offline
        |
        |  local debug port 127.0.0.1:9457, opened only when you ask
        v
 Teams desktop client (WebView2)  ->  Microsoft Teams, as you
```

Reading uses the message cache the Teams client keeps on your disk. Sending types into the real message
box and presses the real Send button, after checking the recipient, the draft and the result. Nothing talks
to Microsoft servers directly and no token or cookie is read.

## Requirements

- Windows 10 or 11 with the new Microsoft Teams desktop client, signed in
- Python 3.10+
- Node.js 22+

## Quick start

```powershell
git clone https://github.com/mike-argio/team-matic
cd team-matic
python teammatic.py doctor        # checks your setup and tells you what to fix
python teammatic.py cdp open      # restarts Teams with the local debug port (see Safety)
python teammatic.py sync          # copies loaded chats into the local cache
python teammatic.py list          # your chats with their chat_id
python teammatic.py incoming --limit 10
python teammatic.py cdp close     # restarts Teams normally
```

After `sync`, the read commands work with Teams closed and the port closed.

## Reading

| Command | What you get |
|---|---|
| `list` | Cached chats: `chat_id`, title, message counts |
| `incoming --limit 30` | Newest incoming messages across chats |
| `incoming --chat <chat_id>` | Incoming messages of one chat |
| `read --chat <chat_id> --limit 50` | One conversation, both directions, oldest first |
| `incoming --cursor NAME [--commit]` | Messages that arrived since this consumer last committed |
| `stats` | Cache size, last sync time and age |
| `whoami` | Author id of your account |
| `sync` | Refresh the cache (needs the debug port) |

Results carry `synced_at_utc` and `cache_age_seconds`, so a script or agent can tell how fresh the data is.

**Each new message once.** A cursor is a named position for one consumer (a script, an agent):

```powershell
python teammatic.py incoming --cursor helpdesk --commit   # first call: creates the cursor, returns nothing
python teammatic.py sync
python teammatic.py incoming --cursor helpdesk            # what arrived since the last commit
python teammatic.py incoming --cursor helpdesk --commit   # advance after handling
```

[`examples/new_messages.py`](examples/new_messages.py) is a complete script built on this.

## Sending

Sending has three steps, and the middle one belongs to a human.

**1. You allow it.** `python teammatic.py init` writes `data/send-policy.json`, disabled. You edit it:

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

Only listed chats can receive messages. `min_interval_seconds` (30 or more) spaces messages apart,
`max_batch_size` caps one batch, `daily_limit` (optional) caps a rolling 24 hours.

**2. Plan and approve.** Write a message file, then:

```powershell
python teammatic.py plan --input message.json --policy data/send-policy.json
```

`plan` does not touch Teams. It validates the file and prints every recipient, the exact text and a SHA256
of the whole batch. Read it. If an agent prepared it, this is the moment you say yes or no.

**3. Run.**

```powershell
python teammatic.py run --batch <SHA256> --confirm <SHA256> --policy data/send-policy.json
```

Each message is typed into Teams, compared with the approved text, sent with one click and then looked up
in the stored history. The result lists `state` and `message_id` per message.

Options:

- `--draft-only` composes the next message and stops. You read it in Teams and press Send yourself.
- `--allow-navigation` lets team-matic switch chats. Without it only the chat already open in Teams is
  written to. Switching clicks inside Teams and can take keyboard focus for a moment.

### Message file

```json
{
  "messages": [
    {
      "chat_id": "<chat_id from list>",
      "title": "Weekly update",
      "text": "Hi **Anna**,\n\n- design *approved*\n- build ~~Friday~~ Monday\n\nDetails: [plan](https://example.com/plan)",
      "table": [["Item", "Status"], ["Design", "done"]]
    }
  ]
}
```

### Formatting

`text` is a small Markdown subset. Full reference: [docs/FORMATTING.md](docs/FORMATTING.md).

| You write | Result |
|---|---|
| `**bold**` | bold |
| `*italic*` or `_italic_` | italic |
| `~~strike~~` | strikethrough |
| `` `code` `` | inline code |
| `[label](https://example.com)` | link |
| `# Heading`, `## Heading`, `### Heading` | headings |
| `- item` | bullet list |
| `1. item` | numbered list |
| `> text` | quote |
| three backticks on a line, code, three backticks | code block |
| `"title": "..."` | one bold line above the text |
| `"table": [[...], [...]]` | table, first row bold |
| emoji | sent as typed |

Not supported: raw HTML, colors, mentions, attachments, images, nested lists. Colored text is left out on
purpose: on the purple bubble of your own messages, red, blue and green text is hard to read.

### Mail merge

One template, many recipients, each with their own values:

```json
{
  "template": {
    "title": "Order {{order}}",
    "text": "Hi {{name}}, your order **{{order}}** ships on *{{date}}*."
  },
  "recipients": [
    {"chat_id": "<chat_id>", "vars": {"name": "Anna", "order": "A-17", "date": "Monday"}},
    {"chat_id": "<chat_id>", "vars": {"name": "Marek", "order": "A-18", "date": "Tuesday"}}
  ]
}
```

`plan` shows every expanded message. A missing variable stops the plan.
See [`examples/mail-merge.json`](examples/mail-merge.json).

### Repeats

The same content is never sent twice to the same chat, even from a different batch. For an intended
repeat such as a weekly reminder, add `"occurrence": "2026-W47"` (any label). It appears in the plan, so the
repeat is a visible decision.

### When a send does not finish

| State | Meaning | What to do |
|---|---|---|
| `sent` | Found in stored history with your author id and the exact text | Nothing |
| `sent` with `warning` | Delivered, but a requested format is missing in the stored message | Look at it in Teams |
| `pending` with `error` | Stopped before the Send click. Nothing was delivered | Fix the cause, `run` again |
| `drafted` | Composed with `--draft-only` | Send it by hand, then `sync` and `reconcile` |
| `uncertain`, `attempting` | Unknown whether it was delivered. The whole outbox stops | See below |

For an unresolved job: look at the chat in Teams. If the message is there, run `sync`, find its id with
`read --chat <chat_id>` and record it with `reconcile --job <id> --message-id <id>`. If it is not there,
`abandon --job <id>` releases the queue. team-matic never resends on its own.

`cancel --batch <SHA256>` cancels what was not sent. `show --batch <SHA256>` prints the current state.

Common stop reasons:

| Error | Cause |
|---|---|
| `SELECT_CHAT_MANUALLY_OR_ALLOW_NAVIGATION` | The target chat is not the one open in Teams |
| `NONEMPTY_DRAFT_OR_UNSUPPORTED_COMPOSER` | The open chat already has a draft; team-matic never overwrites one |
| `TARGET_NOT_LOADED_OR_AMBIGUOUS` | The chat is not in the loaded part of the chat list; scroll to it |
| `DRAFT_MISMATCH_LEAVE_FOR_INSPECTION` | The composed draft differs from the approved text; it is left for you to see |
| `FORMAT_NOT_APPLIED_LEAVE_FOR_INSPECTION <format>` | Your Teams version dropped that format; nothing was sent |
| `COMMAND_UNAVAILABLE ...` | The composer toolbar differs from the tested version |

## For AI agents

The skill in [`skills/team-matic`](skills/team-matic/SKILL.md) teaches an agent the commands and the rules.
Install it into your agent's skills directory:

```powershell
python teammatic.py install-skill --target $HOME\.claude\skills
```

The installed copy holds only the skill text, a small launcher and a pointer to this folder. Your messages
stay in `data/` here. Agents working inside this repository can read [AGENTS.md](AGENTS.md).

What the design does about the usual agent risks:

- **Prompt injection.** Read results are marked `untrusted_content`. The skill tells the agent that message
  text is data, not instructions. An injected message still cannot make team-matic send: the allowlist and
  your approval sit outside the agent.
- **Wrong recipient.** Chats are addressed by `chat_id`. The approved SHA256 covers recipients, titles, text
  and policy; any change needs a new plan.
- **Runaway sending.** Interval, batch cap, daily cap, no duplicates, and a full stop on the first uncertain
  delivery.

## Automation recipes

- **Daily digest of new messages**: Task Scheduler runs `examples/new_messages.py`; replace `handle()`.
- **Agent inbox**: the agent runs `sync`, then `incoming --cursor <name>`, drafts replies as a message file,
  shows you the `plan`, and sends after you approve.
- **Order or appointment notices**: your system writes a mail-merge file from its data; you approve the plan
  once per batch.
- **Review before sending**: `run --draft-only` puts each message in the Teams message box for a person to
  check and send.

team-matic itself never installs a service or a schedule. If you schedule it, the debug port has to stay
open for `sync` and `run`; read the next section first.

## Safety

- **The debug port is powerful.** While it is open, any program running under your Windows account can read
  and control your Teams session. It listens on `127.0.0.1` only, team-matic opens it only on `cdp open`,
  sets it for that one Teams process (no registry or system setting changes), and refuses to use a port that
  is not owned by `ms-teams.exe`. Close it with `cdp close`. Do not use this on a computer you do not trust.
- **Your messages stay local.** `data/` holds the cache, the outbox and the policy, and is git-ignored.
  Do not commit or upload it.
- **Managed devices.** If your organisation manages this computer or account, check its policy before
  enabling a debug port or automating messages.

## Responsible use

team-matic is for your own account and for people who expect to hear from you. Follow the Microsoft
Services Agreement or your organisation's terms, and the anti-spam and privacy law that applies to you.
The limits in the policy are a floor, not a licence: do not use this tool for unsolicited bulk messages,
and do not try to get around Teams' own sending restrictions.

## Limits and tested scope

- Tested on Windows 11, Teams client `26288.2004.5269.3420`, personal account (`teams.live.com`), Polish UI.
  Work and school accounts use the same client and are detected, but have **not** been tested.
- The cache holds what the Teams client has loaded: chats visible in the chat list and the recent part of
  each conversation. It is not a full export. Scroll a chat in Teams to load more, then `sync`.
- Chats only. No channels, meetings, calls, files, reactions, read receipts, edits or deletions.
- Teams updates can rename the interface elements team-matic relies on. `doctor` checks them; a mismatch
  stops the tool instead of guessing.
- One Windows user, one Teams account per `data/` folder. For a second account set `TEAMMATIC_DATA` to
  another folder.

What has been verified live, and what only by offline tests, is listed in
[docs/VERIFICATION.md](docs/VERIFICATION.md).

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `TEAMMATIC_DATA` | `./data` | Folder for cache, outbox and policy |
| `TEAMMATIC_CDP_PORT` | `9457` | Local debug port |
| `TEAMMATIC_OWN_CREATOR` | detected | Force your author id |
| `TEAMMATIC_ROOT` | found automatically | Engine location for an installed skill |

## Development

```powershell
python -m unittest discover -s tests
node tests/test-send-adapter.mjs
```

All tests are offline: temporary databases and a fake page, no Teams and no network.

## License

MIT. See [LICENSE](LICENSE).
