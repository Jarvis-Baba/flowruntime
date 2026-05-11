#!/usr/bin/env python3
"""Signal Bus -- OC<->CC structured coordination protocol.

Single append-only JSONL file. Each agent maintains its own read cursor.
No shared mutable state, no overwrites. Crash recovery = replay the log.

Schema fields:
  id, ts, type, topic, from, to, ball_with, priority, summary, evidence, expires_at
"""

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

SIGNALS_FILE = Path.home() / "cc-workspace" / "signals.jsonl"
CC_CURSOR = Path.home() / "cc-workspace" / "cc.cursor"
OC_CURSOR = Path.home() / "cc-workspace" / "oc.cursor"

VALID_TYPES = {"task", "status", "alert", "handoff"}
VALID_PRIORITIES = {"low", "normal", "high", "critical"}
VALID_BALLS = {"CC", "OC", "闪闪"}

# ── write ────────────────────────────────────────────────────────────

def write_signal(**kwargs) -> dict:
    """Append a signal to the bus. Returns the signal dict."""
    if "from_" in kwargs:
        kwargs["from"] = kwargs.pop("from_")
    now = datetime.now(timezone.utc)
    signal = {
        "id": kwargs.get("id", f"sig_{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"),
        "ts": kwargs.get("ts", now.strftime("%Y-%m-%dT%H:%M:%SZ")),
        "type": kwargs["type"],
        "topic": kwargs["topic"],
        "from": kwargs["from"],
        "to": kwargs["to"],
        "ball_with": kwargs["ball_with"],
        "priority": kwargs.get("priority", "normal"),
        "summary": kwargs["summary"],
        "evidence": kwargs.get("evidence", []),
        "expires_at": kwargs.get("expires_at"),
    }
    assert signal["type"] in VALID_TYPES, f"bad type: {signal['type']}"
    assert signal["priority"] in VALID_PRIORITIES, f"bad priority: {signal['priority']}"
    assert signal["ball_with"] in VALID_BALLS, f"bad ball_with: {signal['ball_with']}"

    SIGNALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SIGNALS_FILE, "a") as f:
        f.write(json.dumps(signal, ensure_ascii=False) + "\n")
    return signal

# ── read ─────────────────────────────────────────────────────────────

def _line_count() -> int:
    if not SIGNALS_FILE.exists():
        return 0
    return sum(1 for _ in open(SIGNALS_FILE))

def _parse_lines(skip: int = 0) -> list[dict]:
    if not SIGNALS_FILE.exists():
        return []
    signals = []
    with open(SIGNALS_FILE) as f:
        for i, line in enumerate(f):
            if i < skip:
                continue
            try:
                signals.append(json.loads(line.strip()))
            except json.JSONDecodeError:
                continue
    return signals

def read_new(cursor_file: Path, ball_filter: str | None = None) -> list[dict]:
    """Read signals since cursor position. Updates cursor atomically after read."""
    cursor = 0
    if cursor_file.exists():
        try:
            cursor = int(cursor_file.read_text().strip() or 0)
        except ValueError:
            cursor = 0

    total = _line_count()
    if cursor >= total:
        return []

    signals = _parse_lines(skip=cursor)
    if ball_filter:
        signals = [s for s in signals if s.get("ball_with") == ball_filter]

    cursor_file.write_text(str(total))
    return signals

def replay(ball_filter: str | None = None, n: int = 20, topic: str | None = None) -> list[dict]:
    """Replay recent signals, optionally filtered by ball holder and topic."""
    all_signals = _parse_lines()
    if ball_filter:
        all_signals = [s for s in all_signals if s.get("ball_with") == ball_filter]
    if topic:
        all_signals = [s for s in all_signals if s.get("topic") == topic]
    return all_signals[-n:]

def pending(ball_filter: str) -> list[dict]:
    """Signals where ball is with someone and not expired."""
    now = datetime.now(timezone.utc)
    signals = _parse_lines()
    pending = []
    for s in signals:
        if s.get("ball_with") != ball_filter:
            continue
        expires = s.get("expires_at")
        if expires:
            try:
                et = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                if et < now:
                    continue
            except (ValueError, TypeError):
                pass
        pending.append(s)
    return pending

# ── format helpers ────────────────────────────────────────────────────

def format_signals(signals: list[dict], max_n: int = 10) -> str:
    """Format signals for console display."""
    if not signals:
        return "(no new signals)"

    lines = []
    for s in signals[-max_n:]:
        prio_icon = {"critical": "!!", "high": "! ", "normal": "  ", "low": ".."}.get(s.get("priority"), "  ")
        topic = s.get("topic", "?")
        summary = s.get("summary", "")
        ball = s.get("ball_with", "?")
        lines.append(f"[{s['type']}|{prio_icon}|{ball}] {topic}: {summary}")
    return "\n".join(lines)

# ── CLI ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Signal Bus CLI")
    sp = p.add_subparsers(dest="cmd")

    # write
    wp = sp.add_parser("write")
    wp.add_argument("--type", required=True)
    wp.add_argument("--topic", required=True)
    wp.add_argument("--from", required=True, dest="from_")
    wp.add_argument("--to", required=True)
    wp.add_argument("--ball-with", required=True)
    wp.add_argument("--priority", default="normal")
    wp.add_argument("--summary", required=True)
    wp.add_argument("--evidence", nargs="*", default=[])
    wp.add_argument("--expires-at", default=None)

    # read
    rp = sp.add_parser("read")
    rp.add_argument("--ball-with", default=None)
    rp.add_argument("--max", type=int, default=10, dest="max_n")

    # replay
    rep = sp.add_parser("replay")
    rep.add_argument("--ball-with", default=None)
    rep.add_argument("--topic", default=None)
    rep.add_argument("--n", type=int, default=20)

    # pending
    pp = sp.add_parser("pending")
    pp.add_argument("--ball-with", default="CC")

    args = p.parse_args()

    if args.cmd == "write":
        sig = write_signal(
            type=args.type, topic=args.topic, from_=args.from_, to=args.to,
            ball_with=args.ball_with, priority=args.priority,
            summary=args.summary, evidence=args.evidence,
            expires_at=args.expires_at,
        )
        print(json.dumps(sig, ensure_ascii=False, indent=2))

    elif args.cmd == "read":
        sigs = read_new(CC_CURSOR, ball_filter=args.ball_with)
        print(format_signals(sigs, max_n=args.max_n))

    elif args.cmd == "replay":
        sigs = replay(ball_filter=args.ball_with, n=args.n, topic=args.topic)
        for s in sigs:
            print(json.dumps(s, ensure_ascii=False))

    elif args.cmd == "pending":
        sigs = pending(ball_filter=args.ball_with)
        print(format_signals(sigs, max_n=50))

    else:
        p.print_help()


# ── OC consumption helper ────────────────────────────────────────────

def oc_consume(handler=None) -> list[dict]:
    """Read new signals for OC and advance cursor. Returns consumed signals.
    Optionally call handler(signal) for each consumed signal.
    """
    signals = read_new(OC_CURSOR, ball_filter="OC")
    if handler:
        for s in signals:
            handler(s)
    return signals
