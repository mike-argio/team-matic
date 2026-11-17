# Changelog

## 0.1.0 (2026-11-17)

First public release. Experimental.

- Live reads from the Teams desktop client (`list`, `incoming`, `read`, `stats`, named cursors) with no message
  copy on disk; optional offline copy with `sync` and `--offline`.
- Owner-approved sending: allowlist policy, offline `plan` with SHA256, `run` with matching confirmation,
  duplicate protection, stop on uncertain delivery, `reconcile`, `abandon`, `--draft-only`.
- Markdown formatting, titles and tables checked in the draft before the Send click; mail merge templates.
- Account check before sending; the client's temporary local id is never accepted as proof of delivery.
- Owner switches in the policy: `agent_may_send` (an agent may run plans itself) and `allow_navigation`.
- A recipient that Teams refuses (`COMPOSE_BLOCKED_BY_TEAMS`) stays `pending`; the rest of the batch continues.
- `doctor`, `cdp open` / `cdp close`, agent skill installer, `npx github:mike-argio/team-matic`.
- Verified live on one personal account, Windows 11, Polish UI.
