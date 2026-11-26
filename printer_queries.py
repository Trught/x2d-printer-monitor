"""Explicit read-query allowlist, based on Bambu Studio 02.09.02.62.

These requests do not start calibrations, change settings or run G-code.
Actual support and LAN authorization still depend on printer firmware.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

_sequence = itertools.count(50000)


@dataclass(frozen=True)
class Query:
    label: str
    section: str
    command: str
    experimental: bool = False


QUERIES = {
    "status": Query("Full status", "pushing", "pushall"),
    "version": Query("Firmware and hardware versions", "info", "get_version"),
    "pa_history": Query(
        "Stored flow dynamics (experimental)", "print", "extrusion_cali_get", True
    ),
    "pa_result": Query(
        "Last flow dynamics result (experimental)",
        "print",
        "extrusion_cali_get_result",
        True,
    ),
    "flow_result": Query(
        "Last flow ratio result (experimental)", "print", "flowrate_get_result", True
    ),
}


def build_query(name: str, nozzle: str = "0.4", extruder: int = 1) -> dict:
    query = QUERIES[name]
    fields = {"command": query.command, "sequence_id": str(next(_sequence))}
    if name == "status":
        fields.update(version=1, push_target=1)
    if query.experimental:
        if nozzle not in ("0.2", "0.4", "0.6", "0.8"):
            raise ValueError("Nozzle diameter must be 0.2, 0.4, 0.6 or 0.8 mm")
        if extruder not in (0, 1):
            raise ValueError("Extruder id must be 0 (Aux) or 1 (Main)")
        fields["nozzle_diameter"] = nozzle
    if name == "pa_history":
        fields.update(filament_id="", extruder_id=extruder)
    return {query.section: fields}
