"""One-shot team-matic CLI for agents. No watcher, no daemon, no background process."""
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve()
COMMANDS = ("doctor", "cdp", "init", "sync", "list", "stats", "incoming", "read", "whoami",
            "plan", "show", "run", "cancel", "reconcile", "abandon")


def engine_root():
    """Engine location: explicit variable, the repository this skill sits in, or the install pointer."""
    if os.environ.get("TEAMMATIC_ROOT"):
        return Path(os.environ["TEAMMATIC_ROOT"]).expanduser().resolve()
    if (HERE.parents[3] / "teammatic.py").is_file():
        return HERE.parents[3]
    pointer = HERE.parents[1] / "engine_root.txt"
    if pointer.is_file():
        return Path(pointer.read_text(encoding="utf-8").strip()).resolve()
    return None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in COMMANDS:
        print("Usage: run.py <" + "|".join(COMMANDS) + "> [options]", file=sys.stderr)
        return 2
    root = engine_root()
    if not root or not (root / "teammatic.py").is_file():
        print("team-matic engine not found. Set TEAMMATIC_ROOT to the cloned repository, "
              "or reinstall with: python teammatic.py install-skill --target <skills dir> --update", file=sys.stderr)
        return 2
    # Reuse the engine. No duplicated logic and no resident process.
    return subprocess.run([sys.executable, str(root / "teammatic.py"), *argv]).returncode


if __name__ == "__main__":
    sys.exit(main())
