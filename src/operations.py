"""Structured operational events shared by shell wrappers and the dashboard."""

from __future__ import annotations

import argparse
import json
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

OPS_EVENT_PREFIX = "OPS_EVENT "


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat_utc(value: Optional[datetime] = None) -> str:
    dt = value or utc_now()
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_event(event: str, status: str, **fields: Any) -> Dict[str, Any]:
    payload = {
        "ts": isoformat_utc(),
        "event": event,
        "status": status,
        "host": socket.gethostname(),
        "version": 1,
    }
    for key, value in fields.items():
        if value is None:
            continue
        payload[key] = value
    return payload


def format_event_line(event: str, status: str, **fields: Any) -> str:
    return OPS_EVENT_PREFIX + json.dumps(build_event(event, status, **fields), sort_keys=True)


def append_event(log_path: str | Path, event: str, status: str, **fields: Any) -> None:
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(format_event_line(event, status, **fields) + "\n")


def log_structured_event(logger: Any, event: str, status: str, **fields: Any) -> None:
    logger.info("%s", format_event_line(event, status, **fields))


def parse_event_line(line: str) -> Optional[Dict[str, Any]]:
    if OPS_EVENT_PREFIX not in line:
        return None
    payload = line.split(OPS_EVENT_PREFIX, 1)[1].strip()
    if not payload:
        return None
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def load_events(log_dir: str | Path) -> List[Dict[str, Any]]:
    base = Path(log_dir)
    events: List[Dict[str, Any]] = []
    if not base.exists():
        return events
    for path in sorted(base.glob("*.log")):
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line in lines:
            parsed = parse_event_line(line)
            if not parsed:
                continue
            parsed["log_file"] = path.name
            events.append(parsed)
    events.sort(key=lambda row: str(row.get("ts") or ""))
    return events


def _cli_emit(args: argparse.Namespace) -> int:
    fields: Dict[str, Any] = {}
    for item in args.field or []:
        if "=" not in item:
            raise SystemExit(f"expected KEY=VALUE for --field, got: {item}")
        key, value = item.split("=", 1)
        fields[key.strip()] = value
    append_event(args.log, args.event, args.status, **fields)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Structured operations events")
    sub = parser.add_subparsers(dest="command", required=True)

    emit = sub.add_parser("emit", help="append one structured event to a log file")
    emit.add_argument("--log", required=True, help="target log file")
    emit.add_argument("--event", required=True, help="event name")
    emit.add_argument("--status", required=True, help="status value")
    emit.add_argument(
        "--field",
        action="append",
        default=[],
        help="extra event field as KEY=VALUE; can be repeated",
    )
    emit.set_defaults(func=_cli_emit)
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
