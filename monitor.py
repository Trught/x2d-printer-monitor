#!/usr/bin/env python3
"""Read-only LAN status monitor for Bambu Lab printers (written for the X2D).

Connects to the printer's MQTT broker (TLS, port 8883, user "bblp", password =
LAN access code), subscribes to device/<serial>/report and prints one status
line per interval: print state, layer, active hotend, temperatures and every
fan. Fan changes are printed as separate events, so a firmware reset at a
hotend switch shows up next to the switch.

The only message it publishes is the "pushall" status request. It sends no
control commands.

Field decoding follows BambuStudio 02.09.01.70 (open source parts only):
  fans        src/slic3r/GUI/DeviceCore/DevFan.cpp       (ParseV1_0, ParseV3_0)
  fan names   src/slic3r/GUI/Widgets/FanControl.cpp      (AIR_FUN labels)
  hotends     src/slic3r/GUI/DeviceCore/DevExtruderSystem.cpp (ParseV2_0)
  bed         src/slic3r/GUI/DeviceCore/DevBed.cpp
  chamber     src/slic3r/GUI/DeviceCore/DevChamber.cpp

Usage:
  python3 monitor.py --serial <SN> [--ip 192.168.1.207] [--csv log.csv] [--raw raw.jsonl]
The access code is read from BAMBU_ACCESS_CODE or asked for (not echoed).
"""
from __future__ import annotations

import argparse
import copy
import csv
import getpass
import json
import os
import ssl
import sys
import threading
import time
from datetime import datetime

# AIR_FUN ids (DevFan.h) -> labels used by Bambu Studio's fan popup
FAN_NAMES = {
    0: "Hotend",      # FAN_HEAT_BREAK_0_IDX
    1: "Parts",       # FAN_COOLING_0_AIRDOOR
    2: "Aux left",    # FAN_REMOTE_COOLING_0_IDX, M106 P2 ("Left(Aux)" in printers/N6.json)
    3: "Exhaust",     # FAN_CHAMBER_0_IDX
    4: "Hotend1",     # FAN_HEAT_BREAK_1_IDX
    5: "MCBoard",     # FAN_MC_BOARD_0_IDX
    6: "Heat",        # FAN_INNNER_LOOP_FAN_0_IDX
    10: "Aux right",  # FAN_REMOTE_COOLING_1_IDX, M106 P10 ("Right(Aux)" in printers/N6.json)
}
AIRDUCT_MODES = {-1: "none", 0: "cooling", 1: "heating", 2: "exhaust", 3: "full-cooling"}
# Device extruder ids on two-hotend printers: 0 = right, 1 = left (AmsMappingPopupUpdate.cpp).
# The X2D calls them Auxiliary and Main (tool_head_display_names in resources/printers/N6.json);
# confirmed on the owner's X2D. A single-hotend printer reports only id 0, which is then "Main".
HOTEND_NAMES = {0: "Aux", 1: "Main"}


def hotend_name(ext_id, total: int):
    return "Main" if total == 1 else HOTEND_NAMES.get(ext_id, ext_id)


def bits(value: int, start: int, count: int = 1) -> int:
    """Same as DevUtil::get_flag_bits."""
    return (int(value) >> start) & ((1 << count) - 1)


def deep_merge(dst: dict, src: dict) -> None:
    """Reports may be partial; keep the latest value of every key."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge(dst[k], v)
        elif _id_list(v) and _id_list(dst.get(k)):
            # fan parts, hotend info: update entries by "id", keep the others
            by_id = {e["id"]: e for e in dst[k]}
            for e in v:
                if e["id"] in by_id:
                    deep_merge(by_id[e["id"]], e)
                else:
                    dst[k].append(copy.deepcopy(e))
        else:
            dst[k] = copy.deepcopy(v)


def _id_list(v) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(e, dict) and "id" in e for e in v)


def legacy_fan_percent(raw) -> int | None:
    """Old 0..15 string fields, converted like DevFan::ParseV1_0 + FanControl."""
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None
    return int(v // 1.5) * 10


def decode(p: dict) -> dict:
    """Turn the merged 'print' object into a flat dict of the values we show."""
    out: dict = {
        "state": p.get("gcode_state", "?"),
        "percent": p.get("mc_percent"),
        "layer": p.get("layer_num"),
        "layers": p.get("total_layer_num"),
        "remain_min": p.get("mc_remaining_time"),
        "stage": p.get("stg_cur"),
        "file": p.get("subtask_name") or p.get("gcode_file"),
    }
    dev = p.get("device") or {}

    # hotends
    ext = dev.get("extruder") or {}
    total = bits(ext["state"], 0, 4) if "state" in ext else len(ext.get("info") or [])
    if "state" in ext:
        st = ext["state"]
        out["active"] = hotend_name(bits(st, 4, 4), total)
        out["target_hotend"] = hotend_name(bits(st, 8, 4), total)
        out["switching"] = bits(st, 12, 3)
    for e in ext.get("info") or []:
        name = hotend_name(e.get("id"), total)
        if "temp" in e:
            out[f"T_{name}"] = bits(e["temp"], 0, 16)
            out[f"T_{name}_tgt"] = bits(e["temp"], 16, 16)
        if "hnow" in e:
            out[f"nozzle_{name}"] = e["hnow"]
    if not ext and "nozzle_temper" in p:
        out["T_Main"] = p.get("nozzle_temper")
        out["T_Main_tgt"] = p.get("nozzle_target_temper")

    # bed and chamber
    if "bed_temp" in dev:
        out["T_bed"], out["T_bed_tgt"] = bits(dev["bed_temp"], 0, 16), bits(dev["bed_temp"], 16, 16)
    elif "bed_temper" in p:
        out["T_bed"], out["T_bed_tgt"] = p.get("bed_temper"), p.get("bed_target_temper")
    ctc = (dev.get("ctc") or {}).get("info") or {}
    if "temp" in ctc:
        out["T_chamber"], out["T_chamber_tgt"] = bits(ctc["temp"], 0, 16), bits(ctc["temp"], 16, 16)
    elif "chamber_temper" in p:
        out["T_chamber"] = p.get("chamber_temper")

    # fans: new airduct block first, legacy fields as fallback
    fans: dict = {}
    duct = dev.get("airduct") or {}
    if duct:
        out["duct_mode"] = AIRDUCT_MODES.get(duct.get("modeCur"), duct.get("modeCur"))
        out["duct_sub"] = duct.get("subMode")
        for part in duct.get("parts") or []:
            if bits(part.get("id", 0), 0, 4) != 0:  # type 1 = air door, not a fan
                continue
            fid = bits(part["id"], 4, 8)
            fans[FAN_NAMES.get(fid, f"fan{fid}")] = bits(part.get("state", 0), 0, 8)
    if not fans:
        for key, name in (("cooling_fan_speed", "Parts"), ("big_fan1_speed", "Aux left"),
                          ("big_fan2_speed", "Exhaust")):
            v = legacy_fan_percent(p.get(key))
            if v is not None:
                fans[name] = v
    if "heatbreak_fan_speed" in p and "Hotend" not in fans:
        fans["Hotend"] = legacy_fan_percent(p["heatbreak_fan_speed"])
    out["fans"] = fans
    return out


def fmt_temp(d: dict, key: str) -> str:
    cur, tgt = d.get(key), d.get(key + "_tgt")
    if cur is None:
        return "-"
    return f"{cur}/{tgt}" if tgt not in (None, 0) else f"{cur}"


def status_line(d: dict) -> str:
    prog = ""
    if d.get("percent") is not None:
        prog = f" {d['percent']}% L{d.get('layer')}/{d.get('layers')} {d.get('remain_min')}min"
    hot = f"active={d.get('active', '-')}"
    if d.get("switching"):
        hot += f"->{d.get('target_hotend')} (switching)"
    temps = (f"Main {fmt_temp(d, 'T_Main')}  Aux {fmt_temp(d, 'T_Aux')}  "
             f"bed {fmt_temp(d, 'T_bed')}  chamber {fmt_temp(d, 'T_chamber')}")
    fans = "  ".join(f"{k} {v}%" for k, v in sorted(d["fans"].items()))
    duct = f" duct={d['duct_mode']}" if "duct_mode" in d else ""
    return f"{d['state']}{prog} | {hot} | {temps} | {fans}{duct}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ip", default="192.168.1.207")
    ap.add_argument("--serial", required=True, help="printer serial number (printer screen)")
    ap.add_argument("--code", help="LAN access code (default: $BAMBU_ACCESS_CODE or prompt)")
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between status lines")
    ap.add_argument("--pushall", type=float, default=300, help="seconds between full-status requests")
    ap.add_argument("--csv", help="append one row per interval to this CSV file")
    ap.add_argument("--raw", help="append every received report as JSON lines to this file")
    ap.add_argument("--quiet", action="store_true", help="print only events, not the status line")
    args = ap.parse_args()

    try:  # imported here so printer_gui.py can reuse decode() without paho
        import paho.mqtt.client as mqtt
    except ImportError:
        sys.exit("paho-mqtt is missing: python3 -m pip install paho-mqtt")

    code =args.code or os.environ.get("BAMBU_ACCESS_CODE") or getpass.getpass("LAN access code: ")
    report_topic = f"device/{args.serial}/report"
    request_topic = f"device/{args.serial}/request"

    state: dict = {}
    lock = threading.Lock()
    got_data = threading.Event()
    raw_fh = open(args.raw, "a", encoding="utf-8") if args.raw else None

    def request_pushall(client) -> None:
        client.publish(request_topic, json.dumps(
            {"pushing": {"sequence_id": str(int(time.time())), "command": "pushall"}}))

    def on_connect(client, userdata, flags, rc, *extra):
        rc_val = getattr(rc, "value", rc)
        if rc_val != 0:
            print(f"connect refused: {rc} (wrong access code or serial?)", file=sys.stderr)
            client.disconnect()
            return
        print(f"connected to {args.ip}, subscribing to {report_topic}", file=sys.stderr)
        client.subscribe(report_topic)
        request_pushall(client)

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload)
        except ValueError:
            return
        if raw_fh:
            raw_fh.write(json.dumps({"t": time.time(), "msg": payload}) + "\n")
            raw_fh.flush()
        with lock:
            deep_merge(state, payload)
        if "print" in payload:
            got_data.set()

    if hasattr(mqtt, "CallbackAPIVersion"):
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"monitor-{os.getpid()}")
    else:
        client = mqtt.Client(client_id=f"monitor-{os.getpid()}")
    client.username_pw_set("bblp", code)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # printer uses a self-signed certificate
    client.tls_set_context(ctx)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(args.ip, 8883, keepalive=60)
    client.loop_start()

    if not got_data.wait(15):
        print("no status received within 15 s", file=sys.stderr)
        client.loop_stop()
        return 1

    csv_fh = csv_writer = None
    fan_cols: list = []
    prev: dict | None = None
    last_push = time.time()
    try:
        while True:
            with lock:
                d = decode(state.get("print") or {})
            now = datetime.now().strftime("%H:%M:%S")

            # events: fan, hotend, state and air duct changes
            if prev is not None:
                ev = []
                for k in sorted(set(d["fans"]) | set(prev["fans"])):
                    if d["fans"].get(k) != prev["fans"].get(k):
                        ev.append(f"{k} {prev['fans'].get(k)}% -> {d['fans'].get(k)}%")
                for k in ("active", "switching", "state", "duct_mode"):
                    if d.get(k) != prev.get(k):
                        ev.append(f"{k} {prev.get(k)} -> {d.get(k)}")
                if ev:
                    print(f"{now} EVENT active={d.get('active', '-')} L{d.get('layer')}: " + "; ".join(ev))
            if not args.quiet:
                print(f"{now} {status_line(d)}")

            if args.csv:
                if csv_writer is None:
                    fan_cols = sorted(d["fans"])
                    new = not os.path.exists(args.csv) or os.path.getsize(args.csv) == 0
                    csv_fh = open(args.csv, "a", newline="", encoding="utf-8")
                    csv_writer = csv.writer(csv_fh)
                    if new:
                        csv_writer.writerow(["time", "state", "percent", "layer", "active", "switching",
                                             "T_Main", "T_Aux", "T_bed", "T_chamber", "duct_mode"]
                                            + [f"fan_{c}" for c in fan_cols])
                csv_writer.writerow([datetime.now().isoformat(timespec="seconds"), d["state"], d.get("percent"),
                                     d.get("layer"), d.get("active"), d.get("switching"), d.get("T_Main"),
                                     d.get("T_Aux"), d.get("T_bed"), d.get("T_chamber"), d.get("duct_mode")]
                                    + [d["fans"].get(c) for c in fan_cols])
                csv_fh.flush()

            prev = d
            if time.time() - last_push > args.pushall:
                request_pushall(client)
                last_push = time.time()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop()
        client.disconnect()
        for fh in (csv_fh, raw_fh):
            if fh:
                fh.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
