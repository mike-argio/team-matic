# Contributing to team-matic

Thanks for helping. The most valuable contributions right now:

1. **Compatibility reports.** Does team-matic work with your Teams version, a work or school account, or a
   non-Polish UI? Open a [compatibility report](https://github.com/mike-argio/team-matic/issues/new?template=compatibility_report.yml).
   Results go into [`docs/VERIFICATION.md`](docs/VERIFICATION.md).
2. **Bug reports** with the redacted `doctor` output.
3. **Fixes and small features** that keep the safety rules below.

## Before you open a pull request

- Read [`AGENTS.md`](AGENTS.md). It is the contract for anyone, human or agent, who changes the code.
- Keep it dependency free: Python standard library and Node.js built-ins only.
- Run the offline tests. Both must pass:

  ```powershell
  python -X utf8 -W error::ResourceWarning -m unittest discover -s tests
  node tests/test-send-adapter.mjs
  ```

- A new message format needs validation, rendering, a draft check, an offline test and a row in
  [`docs/FORMATTING.md`](docs/FORMATTING.md).
- Guards fail closed. Never add a path that retries an `uncertain` send or skips the owner's approval.
- If you tested against a real Teams client, say which version and account type in the pull request.

## Never paste private data

Issues, pull requests, tests and screenshots are public. Remove real names, chat ids (`19:...`, `48:...`),
account ids (`8:...`), message text and anything under `data/`. Use placeholders like `<chat_id>`.

## Security problems

Do not open a public issue. See [`SECURITY.md`](SECURITY.md).

By contributing you agree that your work is released under the [MIT License](LICENSE).
