"""
Immediate halt for all Nookal writes and outbound messages.

Presence of the kill-switch file is enough — content is ignored.
Toggle via CLI (`python -m app.shared.kill_switch on|off|status`) or a
dashboard button that creates/removes the same file.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.shared.config import get_settings
from app.shared.exceptions import KillSwitchActive


def switch_path(path: Path | None = None) -> Path:
    return path or get_settings().paths.kill_switch_file


def is_active(path: Path | None = None) -> bool:
    return switch_path(path).exists()


def activate(path: Path | None = None, reason: str = "") -> Path:
    target = switch_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # File presence is the signal; reason is for whoever opens it under pressure.
    target.write_text(reason.strip() or "kill switch engaged\n", encoding="utf-8")
    return target


def deactivate(path: Path | None = None) -> bool:
    target = switch_path(path)
    if not target.exists():
        return False
    target.unlink()
    return True


def assert_allows(operation: str, path: Path | None = None) -> None:
    """Call at the top of every write/send path."""
    if is_active(path):
        raise KillSwitchActive(operation)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Toggle the automation kill switch")
    parser.add_argument("action", choices=["on", "off", "status"])
    parser.add_argument("--reason", default="", help="Optional note written into the flag file")
    args = parser.parse_args(argv)

    if args.action == "on":
        p = activate(reason=args.reason)
        print(f"kill switch ON — {p}")
        return 0
    if args.action == "off":
        removed = deactivate()
        print("kill switch OFF" if removed else "kill switch already off")
        return 0

    print("ACTIVE" if is_active() else "inactive")
    return 0


if __name__ == "__main__":
    sys.exit(main())
