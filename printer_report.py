"""Extra report decoding for the Printer Monitor: stage, speed, errors, nozzles,
loaded filament, AMS units and hotend-switch timing.

Everything follows Bambu Studio 02.09.01.70's open-source device code:
  stages          src/slic3r/GUI/DeviceManager.cpp      get_stage_string()
  speed           DeviceManager.cpp                      spd_lvl / spd_mag
  extruder state  DeviceCore/DevExtruderSystem.cpp       ParseV2_0 (cur, target, switch state, snow, stat)
  switch states   DeviceCore/DevExtruderSystem.h         DevExtderSwitchState
  filament steps  DeviceCore/DevDefs.h                   DevFilamentStep
  nozzles         DeviceCore/DevNozzleSystem.cpp         ParseV2_0, s_parse_nozzle_type()
  AMS             DeviceCore/DevFilaSystem.cpp           (info bits, temp, humidity, dry_time, trays)
  HMS / errors    DeviceCore/DevHMS.cpp + Bambu Studio's downloaded hms_<lang>_<serial prefix>.json
"""
from __future__ import annotations

import json
import os
import sys

from monitor import bits, hotend_name

STAGES = {
    0: "Printing", 1: "Auto bed leveling", 2: "Heatbed preheating", 3: "Vibration compensation",
    4: "Changing filament", 5: "M400 pause", 6: "Paused (filament ran out)", 7: "Heating nozzle",
    8: "Calibrating dynamic flow", 9: "Scanning bed surface", 10: "Inspecting first layer",
    11: "Identifying build plate type", 12: "Calibrating Micro Lidar", 13: "Homing toolhead",
    14: "Cleaning nozzle tip", 15: "Checking extruder temperature", 16: "Paused by the user",
    17: "Pause (front cover fall off)", 18: "Calibrating the micro lidar", 19: "Calibrating flow ratio",
    20: "Pause (nozzle temperature malfunction)", 21: "Pause (heatbed temperature malfunction)",
    22: "Filament unloading", 23: "Pause (step loss)", 24: "Filament loading", 25: "Motor noise cancellation",
    26: "Pause (AMS offline)", 27: "Pause (low speed of the heatbreak fan)",
    28: "Pause (chamber temperature control problem)", 29: "Cooling chamber", 30: "Pause (G-code inserted by user)",
    31: "Motor noise showoff", 32: "Pause (nozzle clumping)", 33: "Pause (cutter error)",
    34: "Pause (first layer error)", 35: "Pause (nozzle clog)", 36: "Measuring motion precision",
    37: "Enhancing motion precision", 38: "Measure motion accuracy", 39: "Nozzle offset calibration",
    40: "High temperature auto bed leveling", 41: "Auto check: quick release lever",
    42: "Auto check: door and upper cover", 43: "Laser calibration", 44: "Auto check: platform",
    45: "Confirming BirdsEye camera location", 46: "Calibrating BirdsEye camera",
    47: "Auto bed leveling - phase 1", 48: "Auto bed leveling - phase 2", 49: "Heating chamber",
    50: "Adjusting heatbed temperature", 51: "Printing calibration lines", 52: "Auto check: material",
    53: "Live View camera calibration", 54: "Waiting for heatbed temperature", 55: "Auto check: material position",
    56: "Cutting module offset calibration", 57: "Measuring surface",
    58: "Thermal preconditioning for first layer", 59: "Homing blade holder", 60: "Calibrating camera offset",
    61: "Calibrating blade holder position", 62: "Hotend pick and place test",
    63: "Waiting for chamber temperature to equalize",
}
SPEEDS = {1: "Silent", 2: "Standard", 3: "Sport", 4: "Ludicrous"}
SWITCH_STATES = {0: "idle", 1: "busy", 2: "switching", 3: "switch failed"}
FILAMENT_STEPS = {
    0: "idle", 1: "pause", 2: "heat nozzle", 3: "cut filament", 4: "pull current filament",
    5: "push new filament", 6: "grab new filament", 7: "purge old filament", 8: "check position",
    9: "switch extruder", 10: "switch hotend", 11: "AMS filament cooling", 12: "push switcher filament",
    13: "pull switcher filament", 14: "switcher switch", 15: "batch pull filament",
    16: "nesting print tool", 17: "purge filament",
}
NOZZLE_FLOW = {"S": "Standard", "H": "High Flow", "A": "Standard", "X": "Standard", "E": "High Flow",
               "U": "U-flow", "T": "TPU", "B": "E-flow"}
NOZZLE_MATERIAL = {"00": "stainless steel", "01": "hardened steel", "05": "tungsten carbide",
                   "hardened_steel": "hardened steel", "stainless_steel": "stainless steel",
                   "tungsten_carbide": "tungsten carbide", "brass": "brass"}
AMS_TYPES = {0: "External", 1: "AMS", 2: "AMS Lite", 3: "AMS 2 Pro", 4: "AMS HT", 5: "AMS Lite", 6: "AMS", 7: "AMS"}
DRY_STATUS = {0: "off", 1: "checking", 2: "drying", 3: "cooling", 4: "stopping", 5: "error",
              6: "heater fault", 7: "testing"}


def _int(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(str(v), 16)
        except (TypeError, ValueError):
            return default


def _hexbits(v, start, count):
    """get_flag_bits on Bambu's hex-string fields (DevUtil::get_flag_bits(std::string))."""
    try:
        n = v if isinstance(v, int) else int(str(v), 16)
    except ValueError:
        return None
    return (n >> start) & ((1 << count) - 1)


def slot_name(ams_id: int, slot: int) -> str | None:
    if slot == 0xFF:
        return None  # nothing in the extruder (0xFFFF; the X2D reports 0xFEFF)
    if ams_id in (254, 255):
        return "External spool"
    if ams_id >= 128:
        return f"AMS HT {ams_id - 127}"
    return f"AMS {ams_id + 1} slot {slot + 1}"


def nozzle_text(n: dict) -> str:
    """'0.4 Standard hardened steel' from the nozzle info entry."""
    t = str(n.get("type") or "")
    dia = n.get("diameter")
    try:
        dia = float(dia) if dia not in (None, "") else None
    except ValueError:
        dia = None
    flow = material = ""
    if t.startswith("H") and len(t) >= 4:
        flow = NOZZLE_FLOW.get(t[1], t[1])
        material = NOZZLE_MATERIAL.get(t[2:4], t[2:4])
        if dia is None and "-" in t:
            try:
                dia = float(t.split("-", 1)[1])
            except ValueError:
                pass
    elif t and t != "N/A":
        material = NOZZLE_MATERIAL.get(t, t)
    parts = [f"{dia:g} mm" if dia else "", flow, material]
    return " ".join(p for p in parts if p) or (t or "unknown")


def decode_extras(p: dict) -> dict:
    out: dict = {}
    stg = _int(p.get("stg_cur"))
    if stg is not None and stg not in (-1, 255):
        out["stage"] = STAGES.get(stg, f"stage {stg}")
    spd = _int(p.get("spd_lvl"))
    if spd:
        out["speed"] = SPEEDS.get(spd, f"level {spd}")
        if p.get("spd_mag") is not None:
            out["speed_pct"] = _int(p.get("spd_mag"))
    err = _int(p.get("print_error"), 0) or 0
    out["print_error"] = f"{err:08X}" if err else None
    out["hms"] = [f"{_int(h.get('attr'), 0):08X}{_int(h.get('code'), 0):08X}"
                  for h in (p.get("hms") or []) if isinstance(h, dict)]
    if p.get("wifi_signal"):
        out["wifi"] = p.get("wifi_signal")

    dev = p.get("device") or {}
    ext = dev.get("extruder") or {}
    total = bits(ext["state"], 0, 4) if "state" in ext else len(ext.get("info") or [])
    if "state" in ext:
        st = ext["state"]
        out["ext_cur"], out["ext_tgt"], out["switch_state"] = bits(st, 4, 4), bits(st, 8, 4), bits(st, 12, 3)
    nozzles = {}
    for n in (dev.get("nozzle") or {}).get("info") or []:
        nid = _int(n.get("id"))
        if nid is not None:
            nozzles[nid & 0xF] = n
    hotends = {}
    for e in ext.get("info") or []:
        eid = e.get("id")
        name = hotend_name(eid, total)
        h: dict = {"id": eid}
        nid = e.get("hnow", eid)
        if nid in nozzles:
            h["nozzle"] = nozzle_text(nozzles[nid])
            if nozzles[nid].get("wear") not in (None, ""):
                h["wear"] = nozzles[nid].get("wear")
        if "snow" in e:
            h["loaded"] = slot_name(bits(e["snow"], 8, 8), bits(e["snow"], 0, 8))
            h["loaded_key"] = (bits(e["snow"], 8, 8), bits(e["snow"], 0, 8))
        if "stat" in e:
            step = bits(e["stat"], 0, 8)
            h["step"] = FILAMENT_STEPS.get(step, f"step {step}")
        hotends[name] = h
    out["hotends"] = hotends
    if not hotends and nozzles:  # single-hotend printers without the extruder block
        out["hotends"] = {"Main": {"nozzle": nozzle_text(next(iter(nozzles.values())))}}

    # AMS units and their slots
    units, trays = [], {}
    ams = p.get("ams") or {}
    for a in ams.get("ams") or []:
        aid = _int(a.get("id"), 0)
        info = a.get("info")
        u = {"id": aid, "name": (f"AMS HT {aid - 127}" if aid >= 128 else f"AMS {aid + 1}")}
        if info is not None:
            u["type"] = AMS_TYPES.get(_hexbits(info, 0, 4), "AMS")
            ex = _hexbits(info, 8, 4)
            u["hotend"] = hotend_name(ex, total) if ex in (0, 1) else None
            u["dry"] = DRY_STATUS.get(_hexbits(info, 4, 4), "?")
        if a.get("temp") not in (None, ""):
            try:
                u["temp"] = round(float(a["temp"]), 1)
            except ValueError:
                pass
        if a.get("humidity_raw") not in (None, ""):
            u["humidity_pct"] = _int(a["humidity_raw"])
        if a.get("humidity") not in (None, ""):
            u["humidity_level"] = _int(a["humidity"])  # 0-5, larger = drier (DevFilaSystem.h)
        if a.get("dry_time"):
            u["dry_min"] = _int(a["dry_time"])
        u["slots"] = []
        for t in a.get("tray") or []:
            s = _tray(t, _int(t.get("id"), 0))
            u["slots"].append(s)
            trays[(aid, s["slot"])] = s
        units.append(u)
    # external spools: 255 belongs to extruder 0, 254 to extruder 1 (VIRTUAL_TRAY_MAIN_ID /
    # VIRTUAL_TRAY_DEPUTY_ID, DevExtruderSystem.cpp); they report no remaining amount
    if isinstance(p.get("vt_tray"), dict):
        trays[(255, 0)] = _tray(p["vt_tray"], 0, external=True)
    for v in ams.get("vir_slot") or p.get("vir_slot") or []:
        if isinstance(v, dict):
            trays[(_int(v.get("id"), 255), 0)] = _tray(v, 0, external=True)
    out["ams"] = units
    unit_hotend = {u["id"]: u.get("hotend") for u in units}

    # filaments of the running job: print.mapping[i] = AMS id << 8 | slot (DeviceManager.cpp)
    job = []
    for i, v in enumerate(p.get("mapping") or []):
        v = _int(v)
        if v is None or v == 0xFFFF:
            continue
        key = ((v >> 8) & 0xFF, v & 0xFF)
        if key[0] in (254, 255):
            key = (key[0], 0)
            hot = hotend_name(0 if key[0] == 255 else 1, total)
        else:
            hot = unit_hotend.get(key[0])
        job.append({"filament": i + 1, "key": key, "source": slot_name(*key), "hotend": hot})
        if key in trays:
            trays[key].setdefault("job", []).append(i + 1)
    out["job"] = job

    for name, h in out["hotends"].items():
        key = h.get("loaded_key")
        if key and key[0] in (254, 255) and key[1] != 0xFF:
            key = (key[0], 0)
        if h.get("loaded") and key in trays:
            h["filament"] = trays[key]
        else:  # nothing in the extruder now: show what this print assigned to the hotend
            mine = [j for j in job if j["hotend"] == name]
            if mine:
                h["loaded"] = mine[0]["source"]
                h["filament"] = trays.get(mine[0]["key"], {})
                h["in_extruder"] = False
    return out


def _tray(t: dict, slot: int, external: bool = False) -> dict:
    colour, alpha = _rgba(t.get("tray_color"))
    typ = t.get("tray_type") or ""
    return {"slot": slot, "type": typ, "name": t.get("tray_sub_brands") or typ,
            "color": colour if typ else None, "clear": bool(typ) and alpha == 0,
            "remain": -1 if external else _int(t.get("remain"), -1)}


def _rgba(v):
    """'FFFFFFFF' -> ('#ffffff', 255). Alpha 0 is used for translucent filament."""
    s = str(v or "")
    if len(s) >= 6 and all(c in "0123456789abcdefABCDEF" for c in s[:8]):
        return "#" + s[:6].lower(), (int(s[6:8], 16) if len(s) >= 8 else 255)
    return None, None


# ------------------------------------------------------------------ HMS text

def bambu_data_dirs() -> list:
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return [os.path.join(home, "Library", "Application Support", "BambuStudio")]
    if sys.platform.startswith("win"):
        return [os.path.join(os.environ.get("APPDATA", ""), "BambuStudio")]
    return [os.path.join(home, ".config", "BambuStudio")]


class HmsText:
    """Looks up HMS and print-error codes in Bambu Studio's downloaded HMS files."""

    def __init__(self):
        self.hms: dict = {}
        self.err: dict = {}
        self.loaded_from = None

    def load(self, serial: str, lang: str = "en") -> None:
        prefix = (serial or "")[:3]
        for d in bambu_data_dirs():
            for name in (f"hms_{lang}_{prefix}.json", f"hms_{lang}.json"):
                path = os.path.join(d, "hms", name)
                try:
                    with open(path, encoding="utf-8") as fh:
                        data = json.load(fh).get("data", {})
                except (OSError, ValueError, AttributeError):
                    continue
                for key, target in (("device_hms", self.hms), ("device_error", self.err)):
                    for item in (data.get(key) or {}).get(lang) or []:
                        code = str(item.get("ecode", "")).upper()
                        if code and code not in target:
                            target[code] = item.get("intro", "")
                self.loaded_from = self.loaded_from or path
        return None

    def hms_text(self, ecode: str) -> str:
        return self.hms.get(ecode.upper(), "")

    def error_text(self, ecode: str) -> str:
        return self.err.get(ecode.upper(), "")


# ------------------------------------------------------------------ hotend switch timing

class SwitchTimer:
    """Measures hotend switches from the extruder state of every report.

    A switch starts at the first report whose switch state is busy/switching or whose
    current hotend differs from the last settled one, and ends at the first report that
    is idle again with a valid current hotend. Precision is the report interval
    (usually under a second). The filament-change steps seen during the switch are kept.
    """

    def __init__(self):
        self.settled = None      # last hotend id while idle
        self.start = None
        self.steps: list = []
        self.records: list = []

    def feed(self, t: float, extras: dict, layer=None, names=None):
        cur, sw = extras.get("ext_cur"), extras.get("switch_state")
        if cur is None or sw is None:
            return None
        valid = cur in (0, 1)
        started = sw in (1, 2, 3) or not valid or (self.settled is not None and cur != self.settled)
        if started and self.start is None and self.settled is not None:
            self.start, self.steps, self.layer = t, [], layer
        if self.start is not None:
            for name, h in (extras.get("hotends") or {}).items():
                step = h.get("step")
                if step and step != "idle" and (not self.steps or self.steps[-1] != step):
                    self.steps.append(step)
            if sw == 3:
                self.steps.append("switch failed")
        if sw == 0 and valid:
            rec = None
            if self.start is not None:
                if cur != self.settled:  # busy without a hotend change (e.g. filament load) is not a switch
                    # start == end: the whole switch fell between two reports, so no duration
                    rec = {"start": self.start, "end": t,
                           "duration": (t - self.start) if t > self.start else None,
                           "from": names(self.settled) if names else self.settled,
                           "to": names(cur) if names else cur,
                           "layer": self.layer, "steps": list(self.steps)}
                    self.records.append(rec)
                self.start = None
            self.settled = cur
            return rec
        return None

    def stats(self) -> dict:
        if not self.records:
            return {}
        d = [r["duration"] for r in self.records if r["duration"] is not None]
        gaps = [b["start"] - a["end"] for a, b in zip(self.records, self.records[1:])]
        out = {"count": len(self.records), "timed": len(d),
               "avg_gap": (sum(gaps) / len(gaps)) if gaps else None}
        if d:
            out.update({"last": self.records[-1]["duration"], "avg": sum(d) / len(d),
                        "min": min(d), "max": max(d), "total": sum(d)})
        return out
