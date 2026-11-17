# Verification status

What was checked against a real Teams client, and what only by offline tests. Offline tests prove the logic
and the guards; they cannot prove that a Teams version still has the same interface elements.

Environment of the live checks: Windows 11, Teams client `26288.2004.5269.3420`, personal account
(`teams.live.com`), Polish UI, November 2026.

| Capability | Live | Offline tests | Notes |
|---|---|---|---|
| Read the chat list and cached messages | yes | yes | About 4,700 messages in 23 chats; repeated syncs created no duplicates |
| Sync without taking window focus | yes | n/a | Foreground monitor: 0 Teams activations in 3,546 samples while minimized |
| New message in a chat that is not open | yes | yes | Picked up by the next sync |
| Own-account detection without configuration | yes | yes | Client flag present on every message; detected account equals the known one |
| `doctor` compatibility probe, `cdp open` and `cdp close` | yes | n/a | Port listener gone after `cdp close` |
| Plain text send | yes | yes | Supervised batch of two messages through the queue |
| Switching to another chat (`--allow-navigation`) | yes | yes | Takes focus briefly |
| Formatted send through the paste path | pending | yes | Bold, italic, strike, code, link, lists, quote, headings, code block, title, table |
| `--draft-only` | pending | yes | |
| Mail merge, `occurrence`, limits, recovery states | n/a | yes | Queue logic only; no Teams involved |
| Work or school account | no | n/a | Detected by address; untested |
| English and other UI languages | no | yes | No localized text is used to find elements |
| Unattended bulk sending | no | n/a | Not accepted; keep a human approval in the loop |

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
