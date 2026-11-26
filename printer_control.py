#!/usr/bin/env python3
"""Control commands for Bambu Lab printers over LAN MQTT (written for the X2D).

Builds the JSON messages that Bambu Studio publishes to device/<serial>/request
and reads the matching state out of the printer's report. Nothing here talks to
the network: printer_gui.py sends the messages.

The printer accepts these commands over LAN only in Developer Mode.

Message formats follow BambuStudio 02.09.01.70 (open source parts only):
  pause / resume / stop   src/slic3r/GUI/DeviceManager.cpp   command_task_pause/_resume/_abort
  chamber light           src/slic3r/GUI/DeviceCore/DevLampCtrl.cpp
  bed temperature         DeviceManager.cpp command_set_bed (set_bed_temp, or M140 on older firmware)
  nozzle temperature      DeviceManager.cpp command_set_nozzle_new (or M104 on single-hotend printers)
  chamber temperature     src/slic3r/GUI/DeviceCore/DevChamberCtrl.cpp (set_ctt)
  printing speed          DeviceManager.cpp command_set_printing_speed
  air duct mode           src/slic3r/GUI/DeviceCore/DevFan.cpp command_control_air_duct
  prompt sound, buzzer    DevPrintOptions.cpp command_set_prompt_sound, DeviceManager.cpp command_stop_buzzer
"""
from __future__ import annotations

import itertools

from monitor import AIRDUCT_MODES, bits

SPEED_LEVELS = {1: "Silent", 2: "Standard", 3: "Sport", 4: "Ludicrous"}
PRINTING_STATES = ("RUNNING", "PAUSE", "SLICING", "PREPARE")  # MachineObject::is_in_printing_status
AIRDUCT_EXHAUST = 2          # Bambu Studio hides this mode (DevFan.cpp, STUDIO-12796)
# Limits when the printer does not report its own: X2D values from resources/printers/N6.json.
DEFAULT_LIMITS = {"nozzle": 300, "bed": 120, "chamber": 65}
CHAMBER_HEATING_FROM = 40    # support_chamber_temp_switch_heating

_seq = itertools.count(20000)


def _print(command: str, **fields) -> dict:
    return {"print": {"command": command, "sequence_id": str(next(_seq)), **fields}}


def pause() -> dict:
    return _print("pause", param="")


def resume() -> dict:
    return _print("resume", param="")


def stop() -> dict:
    return _print("stop", param="")


def gcode(line: str) -> dict:
    return _print("gcode_line", param=line if line.endswith("\n") else line + "\n")


def chamber_light(on: bool) -> list:
    """Bambu Studio sends both light nodes; printers ignore the one they do not have."""
    return [{"system": {"command": "ledctrl", "led_node": node, "sequence_id": str(next(_seq)),
                        "led_mode": "on" if on else "off", "led_on_time": 500, "led_off_time": 500,
                        "loop_times": 1, "interval_time": 1000}}
            for node in ("chamber_light", "chamber_light2")]


def bed_temp(temp: int, mqtt_command: bool) -> dict:
    return _print("set_bed_temp", temp=int(temp)) if mqtt_command else gcode(f"M140 S{int(temp)}")


def nozzle_temp(temp: int, extruder_id: int | None) -> dict:
    """extruder_id is the device id (X2D: 0 = Aux, 1 = Main); None = single-hotend printer."""
    if extruder_id is None:
        return gcode(f"M104 S{int(temp)}")
    return _print("set_nozzle_temp", extruder_index=int(extruder_id), target_temp=int(temp))


def chamber_temp(temp: int) -> dict:
    return _print("set_ctt", ctt_val=int(temp))


def speed(level: int) -> dict:
    return _print("print_speed", param=str(int(level)))


def airduct(mode_id: int, submode: int = -1) -> dict:
    return _print("set_airduct", modeId=int(mode_id), submode=int(submode))


def prompt_sound(on: bool) -> dict:
    return _print("print_option", sound_enable=bool(on))


def stop_buzzer() -> dict:
    return _print("buzzer_ctrl", mode=0)


def _hex(v) -> int:
    try:
        return int(v, 16) if isinstance(v, str) else int(v or 0)
    except ValueError:
        return 0


def decode_controls(p: dict) -> dict:
    """What the controls need from the merged 'print' object."""
    dev = p.get("device") or {}
    fun, cfg = _hex(p.get("fun")), _hex(p.get("cfg"))
    out: dict = {
        "state": p.get("gcode_state"),
        "printing": p.get("gcode_state") in PRINTING_STATES,
        "speed": p.get("spd_lvl") if p.get("spd_lvl") in SPEED_LEVELS else None,
        "bed_mqtt": bool(bits(fun, 39)),               # m_support_mqtt_bet_ctrl
        "sound_supported": bool(bits(fun, 8)) or p.get("support_prompt_sound") is True,
        "sound_on": bool(bits(cfg, 22)),
        "light": None,
        "has_chamber": "ctc" in dev,
        "limits": dict(DEFAULT_LIMITS),
    }
    for item in p.get("lights_report") or []:
        if isinstance(item, dict) and item.get("node") == "chamber_light":
            out["light"] = item.get("mode") in ("on", "flashing")
    for key, name in (("nozzle_temp_range", "nozzle"), ("bed_temp_range", "bed")):
        rng = p.get(key)
        if isinstance(rng, list) and len(rng) > 1:
            out["limits"][name] = int(rng[1])

    # hotends: device ids for set_nozzle_temp; a single hotend uses M104
    info = (dev.get("extruder") or {}).get("info") or []
    ids = sorted(e["id"] for e in info if isinstance(e, dict) and "id" in e)
    out["extruder_ids"] = ids if len(ids) > 1 else []

    duct = dev.get("airduct") or {}
    out["duct_mode"] = duct.get("modeCur")
    out["duct_modes"] = [m["modeId"] for m in duct.get("modeList") or []
                         if isinstance(m, dict) and "modeId" in m and m["modeId"] != AIRDUCT_EXHAUST]
    return out


def airduct_name(mode_id) -> str:
    return str(AIRDUCT_MODES.get(mode_id, mode_id)).capitalize()


def describe(msg: dict) -> str:
    """Short text for the event log."""
    body = msg.get("print") or msg.get("system") or {}
    cmd = body.get("command", "?")
    extra = {k: v for k, v in body.items() if k not in ("command", "sequence_id", "led_on_time",
                                                        "led_off_time", "loop_times", "interval_time")}
    if cmd == "gcode_line":
        return "G-code " + str(body.get("param", "")).strip()
    return cmd + ("" if not extra or extra == {"param": ""} else " " + ", ".join(f"{k}={v}" for k, v in extra.items()))
