# Security policy

team-matic controls a signed-in Teams session through a local debug port, so security reports matter.

## Report a vulnerability

Please report privately through
[GitHub private vulnerability reporting](https://github.com/mike-argio/team-matic/security/advisories/new).
Do not open a public issue.

Include what an attacker could do, the steps to reproduce, and your Teams and Windows versions.
Remove real names, chat ids and message text.

## In scope

- A way to send a message without the owner's approval of that exact batch.
- A way to retry an `uncertain` send, or to bypass the send policy or the account check.
- Leaks of message content, ids or session data outside `data/`.
- Running arbitrary JavaScript in Teams through team-matic.

## Known and documented

While `cdp open` is active, any program running under your Windows account can control Teams through the
local debug port. This is a property of the port, documented in the README. Close it with `cdp close`.
