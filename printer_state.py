"""Shared incremental report state for the graphical and terminal monitors."""

from __future__ import annotations

import copy


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
