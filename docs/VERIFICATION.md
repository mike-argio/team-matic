# Verification status

What was checked against a real Teams client, and what only by offline tests. Offline tests prove the logic
and the guards; they cannot prove that a Teams version still has the same interface elements.

Environment of the live checks: Windows 11, Teams client `26288.2004.5269.3420`, personal account
(`teams.live.com`), Polish UI, November 2026.

| Capability | Live | Offline tests | Notes |
|---|---|---|---|
| Read the chat list and messages live, no copy on disk | yes | yes | About 4,700 messages in 23 chats; only `state.json` written |
| Offline copy (`sync`, then `--offline`) | yes | yes | Repeated syncs created no duplicates |
| Sync without taking window focus | yes | n/a | Foreground monitor: 0 Teams activations in 3,546 samples while minimized |
| New message in a chat that is not open | yes | yes | Picked up by the next sync |
| Own-account detection without configuration | yes | yes | Client flag present on every message; detected account equals the known one |
| `doctor` compatibility probe, `cdp open` and `cdp close` | yes | n/a | Port listener gone after `cdp close` |
| Plain text send | yes | yes | Supervised batch of two messages through the queue |
| Switching to another chat (`--allow-navigation`) | yes | yes | Takes focus briefly |
| Formatted send through the paste path | yes | yes | Bold, italic, strike, code, link, lists, quote, headings, code block, title, table |
| `--draft-only` | yes | yes | Draft left in the composer, job state `drafted` |
| Mail merge to separate 1:1 chats (no group) | yes | yes | One template, three chats, one message per chat; chat count unchanged |
| Batch run by an agent under `agent_may_send` | yes | yes | Owner switch in the policy; no per-batch confirmation |
| Recipient refused by Teams | yes | yes | `COMPOSE_BLOCKED_BY_TEAMS`: job stays `pending`, the batch continues |
| `occurrence`, limits, recovery states | n/a | yes | Queue logic only; no Teams involved |
| Work or school account | no | n/a | Detected by address; untested |
| English and other UI languages | no | yes | No localized text is used to find elements |
| Large unattended campaigns | no | n/a | Untested beyond three recipients; Teams may restrict senders |

"pending" means implemented and guarded, but not yet confirmed on a real client. The guard for formatted
sends is fail-closed: if the composed draft does not contain the approved text and formats, nothing is sent.

## Live log

- 2026-11-17: reader and queue verified as listed above with the previous, account-specific collector.
- 2026-11-17: read-only run of the current release by an agent that knew only the installed skill:
  `doctor`, `cdp open`, `doctor` (all 12 checks pass, `isSentByCurrentUser` present), `sync`
  (21 visible chats, 4,705 cached messages, no duplicates), `whoami`, `stats`, `list`,
  `incoming --cursor --commit`, `cdp close`, `cdp status` (closed). Every command exited 0.
  Auto-detection on the fresh snapshot returned the same account as the stored one (`client-flag`).
  No message was sent.
- 2026-11-17: live-read release and send check, only in the account's own notes chat: one plain and one
  formatted message sent and confirmed in history with their server message ids, one `--draft-only` job left
  as a draft. Every format rendered; headings show as bold lines, not larger text. Found and fixed: a plain
  send briefly reported the client's local echo id (`-1`) as proof of delivery. Ids starting with `-` are
  now rejected in the adapter and in the queue, with an offline test. Port closed afterwards.
- 2026-11-17: serial send, requested by the owner: one mail merge template to three separate 1:1 chats (own
  notes, a colleague, the owner's second account), policy with `agent_may_send` and `allow_navigation`, run by
  the agent. Two messages sent and found in history with server ids; the chat count stayed at 21, so no group
  was created. Teams refused the third chat ("wait until the user replies"), an external-contact restriction.
  Found and fixed: that refusal surfaced as a generic `UNSAFE_TARGET_OR_DRAFT` and stopped the whole batch; it is
  now `COMPOSE_BLOCKED_BY_TEAMS`, the job stays `pending` and the batch continues. A blocked or drafted chat that
  happened to be selected also stopped sends to other chats; only the destination composer is checked now.
  `doctor` crashed in Python UTF-8 mode on localized `tasklist` output; fixed. The earlier `--draft-only` draft
  was later sent by hand and recorded with `reconcile`. Port closed afterwards.
