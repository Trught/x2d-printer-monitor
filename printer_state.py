"""Shared incremental report state for the graphical and terminal monitors."""

from __future__ import annotations

import copy
import math

TELEMETRY_TIMEOUT = 15.0
_OBJECTS = {
    ("print", "device"), ("print", "ams"), ("print", "vt_tray"),
    ("print", "device", "extruder"), ("print", "device", "nozzle"),
    ("print", "device", "airduct"), ("print", "device", "bed"),
    ("print", "device", "bed", "info"), ("print", "device", "ctc"),
    ("print", "device", "ctc", "info"),
}
_OBJECT_LISTS = {
    ("print", "hms"), ("print", "lights_report"), ("print", "vir_slot"),
    ("print", "ams", "ams"), ("print", "ams", "vir_slot"),
    ("print", "ams", "ams", "*", "tray"),
    ("print", "device", "extruder", "info"),
    ("print", "device", "nozzle", "info"),
    ("print", "device", "airduct", "parts"),
    ("print", "device", "airduct", "modeList"),
}


def validate_message(message: dict) -> None:
    """Reject malformed JSON/known scalar fields before they enter live state.

    Unknown fields remain available to the Inspector. Decoder-specific nested
    shapes are additionally checked by decoding a candidate state before commit.
    """
    if not isinstance(message, dict):
        raise ValueError("Report must be a JSON object")
    pending = [(message, ())]
    count = 0
    while pending:
        value, path = pending.pop()
        count += 1
        if len(path) > 48 or count > 100000:
            raise ValueError("Report is too deeply nested or too large")
        if value is not None and path in _OBJECTS and not isinstance(value, dict):
            raise ValueError(".".join(path) + " must be an object")
        if value is not None and path in _OBJECT_LISTS:
            if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
                raise ValueError(".".join(path) + " must be a list of objects")
        if isinstance(value, dict):
            if not all(isinstance(key, str) for key in value):
                raise ValueError("Report object keys must be strings")
            if path[-1:] == ("*",):
                for key in ("tray_type", "tray_sub_brands"):
                    if key in value and value[key] is not None and not isinstance(value[key], str):
                        raise ValueError(".".join(path + (key,)) + " must be text")
            pending.extend((item, path + (key,)) for key, item in value.items())
        elif isinstance(value, list):
            pending.extend((item, path + ("*",)) for item in value)
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Report contains a non-finite number")
        elif value is not None and not isinstance(value, (str, int, float, bool)):
            raise ValueError("Report contains a non-JSON value")
    for section in ("print", "info", "system", "pushing", "camera"):
        if section in message and not isinstance(message[section], dict):
            raise ValueError(f"{section} must be an object")
    p = message.get("print", {})
    for key in ("gcode_state", "subtask_name", "gcode_file", "wifi_signal"):
        if key in p and p[key] is not None and not isinstance(p[key], str):
            raise ValueError(f"print.{key} must be text")
    for key in ("mc_percent", "layer_num", "total_layer_num", "mc_remaining_time",
                "nozzle_temper", "nozzle_target_temper", "bed_temper", "bed_target_temper",
                "chamber_temper", "chamber_target_temper"):
        value = p.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError(f"print.{key} must be a number")
        if value is not None:
            try:
                finite = math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                raise ValueError(f"print.{key} must be finite")


def merged_copy(dst: dict, src: dict, _path: tuple = ()) -> dict:
    """Copy only changed branches, so failed decoding cannot poison live state."""
    result = dict(dst)
    for key, value in src.items():
        path = _path + (key,)
        device_path = path[1:] if path[:1] == ("print",) else path
        current = dst.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            result[key] = merged_copy(current, value, path)
        elif device_path in _DELTA_LISTS and _id_list(value) and _id_list(current):
            items = list(current)
            by_id = {item["id"]: i for i, item in enumerate(items)}
            for item in value:
                index = by_id.get(item["id"])
                if index is None:
                    by_id[item["id"]] = len(items)
                    items.append(copy.deepcopy(item))
                else:
                    items[index] = merged_copy(items[index], item, path)
            result[key] = items
        else:
            result[key] = copy.deepcopy(value)
    return result


# These device lists contain deltas for individual hardware components. AMS,
# alerts and other lists are snapshots: replacing them also removes stale items.
_DELTA_LISTS = {
    ("device", "extruder", "info"),
    ("device", "nozzle", "info"),
    ("device", "airduct", "parts"),
}


def _id_list(value) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(
            isinstance(item, dict) and isinstance(item.get("id"), (int, str))
            for item in value
        )
    )


def deep_merge(dst: dict, src: dict, _path: tuple = ()) -> None:
    """Merge objects, retain hardware deltas by id, and replace snapshot lists.

    Accept either an entire MQTT message or its ``print`` object. Never retain
    references into the incoming message, which may be reused by a caller.
    """
    for key, value in src.items():
        path = _path + (key,)
        device_path = path[1:] if path[:1] == ("print",) else path
        current = dst.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            deep_merge(current, value, path)
        elif device_path in _DELTA_LISTS and _id_list(value) and _id_list(current):
            by_id = {item["id"]: item for item in current}
            for item in value:
                if item["id"] in by_id:
                    deep_merge(by_id[item["id"]], item, path)
                else:
                    added = copy.deepcopy(item)
                    current.append(added)
                    by_id[item["id"]] = added
        else:
            dst[key] = copy.deepcopy(value)
