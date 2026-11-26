"""Observe every reported JSON field, including fields with unknown semantics."""

from __future__ import annotations

import copy
import fnmatch
import json
from dataclasses import dataclass

FIELD_HINTS = {
    "/print/device/extruder/info/*/info": "Extruder flags: bits 1 filament, 2 buffer filament, 3 nozzle present (Studio source)",
    "/print/device/extruder/info/*/temp": "Packed temperature: low 16 bits current, high 16 bits target (°C)",
    "/print/device/extruder/info/*/stat": "Low byte filament-change step; high 16 bits RFID state (Studio source)",
    "/print/device/extruder/state": "Hotend count, active/target hotend and switching flags (Studio source)",
    "/print/device/bed_temp": "Packed bed temperature: current / target (°C)",
    "/print/device/ctc/info/temp": "Packed chamber temperature: current / target (°C)",
    "/print/ams/ams/*/tray/*/k": "Reported filament flow-dynamics K value; not a bed/vibration correction",
    "/print/ams/ams/*/tray/*/cali_idx": "Selected filament calibration index",
    "/print/filaments/*/k_value": "Flow-dynamics calibration K value from query reply",
    "/print/filaments/*/n_coef": "Flow-dynamics calibration N coefficient from query reply",
    "/print/filaments/*/flow_ratio": "Flow-ratio calibration result from query reply",
    "/print/home_flag": "Device/home flags; preserve raw bits, interpretation depends on model",
    "/print/stat": "Hex device flags; includes door state (model-dependent)",
}
_SECRET_KEYS = {"access_code", "password", "token", "access_token", "refresh_token"}


def flatten(value, path=""):
    if isinstance(value, dict) and value:
        for key, item in value.items():
            component = str(key).replace("~", "~0").replace("/", "~1")
            child = path + "/" + component
            if str(key).lower() in _SECRET_KEYS:
                yield child, "[redacted]"
            else:
                yield from flatten(item, child)
    elif isinstance(value, list) and value:
        for index, item in enumerate(value):
            yield from flatten(item, path + f"/{index}")
    else:
        yield path or "/", value


@dataclass
class Field:
    value: object
    previous: object
    seen: float
    changes: int = 0


class ReportInspector:
    MAX_FIELDS = 10000

    def __init__(self):
        self.fields: dict[str, Field] = {}
        self.replies: dict[str, dict] = {}
        self.revision = 0
        self.truncated = False

    def observe(self, message: dict, now: float):
        for path, value in flatten(message):
            field = self.fields.get(path)
            if field is None:
                if len(self.fields) >= self.MAX_FIELDS:
                    self.truncated = True
                    continue
                self.fields[path] = Field(copy.deepcopy(value), None, now)
            else:
                if field.value != value:
                    field.previous, field.value = field.value, copy.deepcopy(value)
                    field.changes += 1
                field.seen = now
        for section, body in message.items():
            if isinstance(body, dict) and body.get("command") not in (
                None,
                "push_status",
            ):
                # Store the latest reply separately; never merge it into live print state.
                command = str(body["command"])
                if len(self.replies) < 100 or command in self.replies:
                    self.replies[command] = {
                        "received": now,
                        "message": dict(flatten({section: body})),
                    }
        self.revision += 1

    @staticmethod
    def hint(path):
        return next(
            (
                hint
                for pattern, hint in FIELD_HINTS.items()
                if fnmatch.fnmatchcase(path, pattern)
            ),
            "Unknown field; raw value, no verified unit or interpretation",
        )

    def snapshot(self):
        return {
            "format": "bambu-observed-fields-v1",
            "truncated": self.truncated,
            "fields": {
                path: {
                    "value": copy.deepcopy(field.value),
                    "previous": copy.deepcopy(field.previous),
                    "last_received": field.seen,
                    "changes": field.changes,
                    "hint": self.hint(path),
                }
                for path, field in sorted(self.fields.items())
            },
            "replies": copy.deepcopy(self.replies),
        }

    def export(self, path):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                self.snapshot(), handle, ensure_ascii=False, indent=2, allow_nan=False
            )
