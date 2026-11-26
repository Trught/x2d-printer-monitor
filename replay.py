#!/usr/bin/env python3
"""Replay a monitor JSONL recording offline, without MQTT or a printer."""
from __future__ import annotations

import argparse
import json
import math

import printer_control as pc
from bambu_monitor import sensor_summary
from monitor import decode
from printer_report import SwitchTimer, decode_extras
from printer_state import TELEMETRY_TIMEOUT, merged_copy, validate_message


def replay_lines(lines):
    state, timer, errors = {}, SwitchTimer(), []
    count = skipped = 0
    previous = None
    previous_clock = None
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            if not isinstance(entry, dict):
                raise ValueError("Recording entry must be an object")
            if entry.get("event") == "telemetry_gap":
                state, previous, previous_clock = {}, None, None
                timer.interrupt()
                continue
            message = entry.get("msg", entry.get("merged_state"))
            validate_message(message)
            now = float(entry["t"])
            clock = "monotonic" if "monotonic" in entry else "t"
            elapsed = float(entry[clock])
            if not math.isfinite(now) or not math.isfinite(elapsed):
                raise ValueError("Recording timestamp must be finite")
            if "merged_state" in entry or (previous is not None and
                    (clock != previous_clock or elapsed < previous or elapsed - previous > TELEMETRY_TIMEOUT)):
                state = {}
                timer.interrupt()
            # Replies belong in diagnostics, never in merged live status.
            if any(isinstance(body, dict) and body.get("command") not in (None, "push_status")
                   for body in message.values()) or not message.get("print"):
                continue
            candidate = merged_copy(state, message)
            p = candidate["print"]
            decode(p)
            extras = decode_extras(p)
            pc.decode_controls(p)
            sensor_summary(candidate)
            state = candidate
            timer.feed(now, extras, layer=p.get("layer_num"), monotonic=elapsed)
            previous, previous_clock = elapsed, clock
            count += 1
        except (ValueError, TypeError, AttributeError, KeyError, IndexError, OverflowError, RecursionError) as exc:
            skipped += 1
            if len(errors) < 100:
                errors.append({"line": number, "error": str(exc)})
    return {"reports": count, "skipped": skipped, "switches": timer.records,
            "statistics": timer.stats(), "last_state": state, "errors": errors}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", help="JSONL file from Record raw")
    args = parser.parse_args(argv)
    try:
        with open(args.recording, encoding="utf-8") as handle:
            result = replay_lines(handle)
    except (OSError, UnicodeError) as exc:
        parser.exit(1, f"Cannot read recording: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return int(bool(result["skipped"]))


if __name__ == "__main__":
    raise SystemExit(main())
