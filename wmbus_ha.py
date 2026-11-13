#!/usr/bin/env python3
"""
wmbus_ha.py - long-running service (Raspberry Pi + RTL-SDR) that follows an
*encrypted* wM-Bus water meter (developed on a Diehl Hydrus) and publishes to
Home Assistant, via MQTT discovery, an estimated index built from 10 L volume
steps, live consumption figures and classified consumption episodes.

Why steps: without the AES key the volume field cannot be decrypted, but the
encrypted payload still changes in a recognisable way whenever the meter's
volume register crosses a 10 L boundary. Rules (found empirically, see README):
  * dedup: identical payload within 150 ms = same transmission seen by both
    rtl_wmbus demodulators
  * frames are grouped by access number; a payload change is accepted only once
    `--confirm` consecutive frames carry it (a lone frame = corrupt but CRC-valid)
  * an accepted change touching AES block 1 = one 10 L step;
    changes confined to blocks 2-3 are other fields (no step)
  * steps less than `--flow-window` (150 s) apart belong to the same episode

Absolute index = manual LCD reading (anchor) + steps * 10 L. The anchor is set
from HA ("Set index" entity) or by publishing m3 to <base>/set_index. The index
has no long-term statistics (recalibration would make it jump); statistics are
kept on "Tracked consumption", which never resets.

Optional hot-water hint: publish the hot-water tank temperature (deg C) to
`--boiler-topic`. An episode is flagged hot_water if the tank drops >= 2 deg
during it, or starts reheating (rises >= 2 deg) within 15 min after it ends.

Environment: MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASS, WMBUS_METER_ID
State:       /var/lib/wmbus-ha/state.json, episodes.jsonl, raw.log
"""

import argparse
import json
import os
import pty
import re
import select
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

STEP_L = 10.0
DEDUP_S = 0.150
KEEP_STEPS_S = 48 * 3600
PAUSE_S = 3600           # one hour without steps counts as a pause (slow-leak detection)
REHEAT_WINDOW_S = 900    # look this long after an episode for tank reheating
LINE = re.compile(
    r"^(?P<mode>[A-Za-z0-9]+);\d+;\d+;(?P<ts>[\d\-]+ [\d:.]+);"
    r"(?P<m1>-?\d+);(?P<m2>-?\d+);(?P<id>[0-9a-fA-F]+);0x(?P<hex>[0-9a-fA-F]+)\s*$")

# Episode types (HA event_types). The label is a guess from litres, time and
# the hot-water hint - not a measurement.
TYPES = ["possible_leak", "watering", "bath_or_watering", "long_shower", "shower",
         "morning_routine", "cooking_or_dishes", "dishes_or_short_shower", "medium_use",
         "short_use"]
SHOWERS = ("shower", "long_shower", "dishes_or_short_shower")


def classify(liters, hour, hot):
    """Guess what an episode was. `hot` is True/False/None (unknown)."""
    if 1 <= hour < 5:
        return "possible_leak"
    if liters >= 150:
        return "watering"
    if liters >= 90:
        return "bath_or_watering"
    if liters >= 50:
        return "long_shower" if hot is not False else "watering"
    if liters >= 30:
        return "shower" if hot is not False else "medium_use"
    if liters >= 20:
        if hot and 18 <= hour < 23:
            return "dishes_or_short_shower"
        if 6 <= hour < 11:
            return "morning_routine"
        if 11 <= hour < 15:
            return "cooking_or_dishes"
        if 18 <= hour < 23:
            return "dishes_or_short_shower"
        return "medium_use"
    return "short_use"


def blocks(a, b):
    """1-based indices of the 16-byte AES blocks that differ between two payloads."""
    return [i + 1 for i in range(3) if a[i*16:(i+1)*16] != b[i*16:(i+1)*16]]


class Tracker:
    """Incremental step detector: feed it frames, it says 'step', 'other' or None."""

    def __init__(self, confirm):
        self.confirm = confirm
        self.acc = None
        self.val = None
        self.pending = None
        self.last_pl = {}

    def feed(self, ts, acc, pl):
        prev = self.last_pl.get(pl)
        self.last_pl[pl] = ts
        if len(self.last_pl) > 64:
            self.last_pl = {k: v for k, v in self.last_pl.items() if ts - v < 60}
        if prev is not None and ts - prev < DEDUP_S:
            return None
        if acc != self.acc:
            self.acc, self.val, self.pending = acc, pl, None
            return None
        if pl == self.val:
            self.pending = None
            return None
        if self.pending and self.pending[0] == pl:
            self.pending[1] += 1
        else:
            self.pending = [pl, 1]
        if self.pending[1] < self.confirm:
            return None
        blk = blocks(self.val, pl)
        self.val, self.pending = pl, None
        return "step" if 1 in blk else "other"


def parse_frame(line, meter_id):
    """rtl_wmbus output line -> (m1, m2, access_number, payload) or None."""
    m = LINE.match(line.strip())
    if not m or m["id"].lower() != meter_id.lower():
        return None
    try:
        raw = bytes.fromhex(m["hex"])
    except ValueError:
        return None
    if len(raw) < 16 or raw[10] != 0x7A:          # short header (CI 0x7A) only
        return None
    return int(m["m1"]), int(m["m2"]), raw[11], raw[15:]


def boiler_hint(hist, t0, t1):
    """(hot, drop) for an episode [t0, t1] from tank temperature history [(t, temp)]."""
    before = [x[1] for x in hist if x[0] <= t0]
    after = [x[1] for x in hist if t0 < x[0] <= t1 + REHEAT_WINDOW_S]
    if not before and not after:
        return None, None
    start_t = before[-1] if before else after[0]
    seq = [start_t] + after
    drop = round(start_t - min(seq), 1)
    lo, rise = seq[0], 0.0
    for v in seq:
        lo = min(lo, v)
        rise = max(rise, v - lo)
    return (drop >= 2.0 or rise >= 2.0), drop


class State:
    def __init__(self, path):
        self.path = path
        self.d = dict(anchor_m3=None, anchor_time=None, steps=0, total_steps=0,
                      step_times=[], day=None, day_steps=0, episode=None,
                      counts_day=None, counts={})
        try:
            self.d.update(json.load(open(path)))
        except (OSError, ValueError):
            pass

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.d, f)
        os.replace(tmp, self.path)

    def index_m3(self):
        if self.d["anchor_m3"] is None:
            return None
        return round(self.d["anchor_m3"] + self.d["steps"] * STEP_L / 1000.0, 3)


def spawn_chain(a):
    """Start rtl_sdr | rtl_wmbus. rtl_wmbus gets a pty as stdout so libc keeps it
    line-buffered, while its stdin stays a plain pipe (binary IQ data)."""
    master, slave = pty.openpty()
    p1 = subprocess.Popen([a.rtl_sdr, "-f", a.freq, "-s", a.rate, "-g", a.gain, "-"],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    p2 = subprocess.Popen([a.rtl_wmbus], stdin=p1.stdout, stdout=slave, stderr=subprocess.DEVNULL)
    p1.stdout.close()
    os.close(slave)
    return master, (p1, p2)


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat() if t else None


def main():
    import paho.mqtt.client as mqtt          # here, so replay.py can import the rules without paho

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--id", default=os.environ.get("WMBUS_METER_ID"),
                    help="meter id as printed by rtl_wmbus (default: $WMBUS_METER_ID)")
    ap.add_argument("--confirm", type=int, default=2)
    ap.add_argument("--flow-window", type=float, default=150.0)
    ap.add_argument("--freq", default="868.95M")
    ap.add_argument("--rate", default="1.6e6")
    ap.add_argument("--gain", default="49.6")
    ap.add_argument("--rtl-sdr", default="rtl_sdr")
    ap.add_argument("--rtl-wmbus", default="rtl_wmbus")
    ap.add_argument("--manufacturer", default="Diehl")
    ap.add_argument("--model", default="Hydrus (wM-Bus, encrypted)")
    ap.add_argument("--boiler-topic", default="wmbus/boiler/dhw_temp",
                    help="MQTT topic with the hot-water tank temperature (optional)")
    ap.add_argument("--state", default="/var/lib/wmbus-ha/state.json")
    ap.add_argument("--episodes", default="/var/lib/wmbus-ha/episodes.jsonl")
    ap.add_argument("--raw-log", default="/var/lib/wmbus-ha/raw.log")
    a = ap.parse_args()
    if not a.id:
        ap.error("meter id required (--id or WMBUS_METER_ID)")

    base = f"wmbus/{a.id}"
    st = State(a.state)
    tr = Tracker(a.confirm)
    lock = threading.Lock()
    live = dict(last_frame=None, rssi=None, frames=0)
    boiler = dict(temp=None, hist=[])     # hist: [(t, temp)]

    cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"wmbus-{a.id}")
    cli.username_pw_set(os.environ["MQTT_USER"], os.environ["MQTT_PASS"])
    cli.will_set(f"{base}/availability", "offline", retain=True)

    dev = dict(identifiers=[f"wmbus_{a.id}"], name="Water meter", manufacturer=a.manufacturer,
               model=a.model, serial_number=a.id)
    avail = dict(availability_topic=f"{base}/availability")

    # ------------------------------------------------------------ figures
    def roll_day(now):
        day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        if st.d["day"] != day:
            st.d.update(day=day, day_steps=0)
        if st.d["counts_day"] != day:
            st.d.update(counts_day=day, counts={})

    def no_pause_hours(now):
        """Hours since the last pause of >= PAUSE_S without any step (slow-leak indicator)."""
        ts = st.d["step_times"]
        if not ts or now - ts[-1] >= PAUSE_S:
            return 0.0
        start = ts[0]
        for x, y in zip(ts, ts[1:]):
            if y - x >= PAUSE_S:
                start = y
        return round((now - start) / 3600.0, 2)

    def dynamics(now):
        ts = st.d["step_times"]
        n = lambda s: sum(1 for x in ts if now - x <= s)
        ep = st.d["episode"]
        return dict(
            today_l=st.d["day_steps"] * STEP_L,
            last_15m_l=n(900) * STEP_L,
            last_1h_l=n(3600) * STEP_L,
            flow_lpm=round(n(300) * STEP_L / 5.0, 1),
            episode_l=(ep["n"] * STEP_L) if ep else 0,
            episode_min=round((now - ep["t0"]) / 60.0, 1) if ep else 0,
            no_pause_h=no_pause_hours(now))

    def close_episode(now, force=False):
        """End the live episode and queue it for classification."""
        ep = st.d["episode"]
        if not ep or (not force and now - ep["t1"] < a.flow_window):
            return
        st.d["episode"] = None
        st.d.setdefault("to_finalize", []).append(ep)
        st.save()

    def finalize_due(now):
        """Classify queued episodes: right away if the tank already shows hot-water
        use, otherwise once the reheat window has passed."""
        out, keep = [], []
        for ep in st.d.get("to_finalize", []):
            hot, drop = boiler_hint(boiler["hist"], ep["t0"], ep["t1"])
            if now - ep["t1"] < REHEAT_WINDOW_S and not hot:
                keep.append(ep)
                continue
            liters = ep["n"] * STEP_L
            hour = datetime.fromtimestamp(ep["t0"]).hour
            kind = classify(liters, hour, hot)
            dur = (ep["t1"] - ep["t0"]) / 60.0 + 1.0        # +1 min for the first step
            rec = dict(start=iso(ep["t0"]), end=iso(ep["t1"]), liters=liters,
                       duration_min=round(dur, 1), avg_lpm=round(liters / max(1.0, dur), 1),
                       type=kind, hot_water=hot, dhw_drop=drop)
            day = datetime.fromtimestamp(ep["t0"]).strftime("%Y-%m-%d")
            roll_day(now)
            if day == st.d["counts_day"]:
                st.d["counts"][kind] = st.d["counts"].get(kind, 0) + 1
            with open(a.episodes, "a") as f:
                f.write(json.dumps(rec) + "\n")
            out.append(rec)
        if out or len(keep) != len(st.d.get("to_finalize", [])):
            st.d["to_finalize"] = keep
            st.save()
        return out

    # ------------------------------------------------------------ MQTT
    def disc(component, key, cfg):
        cfg = dict(cfg, unique_id=f"wmbus_{a.id}_{key}", object_id=f"water_meter_{key}", device=dev, **avail)
        cli.publish(f"homeassistant/{component}/wmbus_{a.id}/{key}/config", json.dumps(cfg), retain=True)

    def publish_state():
        now = time.time()
        with lock:
            roll_day(now)
            idx = st.index_m3()
            dy = dynamics(now)
            c = st.d["counts"]
            p = dict(index=idx, flowing="ON" if st.d["episode"] else "OFF",
                     total_l=st.d["total_steps"] * STEP_L,
                     last_frame=iso(live["last_frame"]), rssi=live["rssi"],
                     calibrated=idx is not None,
                     showers_today=sum(c.get(k, 0) for k in SHOWERS),
                     episodes_today=sum(c.values()), **dy)
        cli.publish(f"{base}/state", json.dumps(p), retain=True)

    def recent_episodes(n=15):
        try:
            with open(a.episodes) as f:
                lines = f.readlines()[-n:]
        except OSError:
            return []
        out = []
        for l in reversed(lines):
            try:
                out.append(json.loads(l))
            except ValueError:
                pass
        return out

    def publish_recent():
        eps = recent_episodes()
        cli.publish(f"{base}/recent", json.dumps(dict(count=len(eps), episodes=eps)), retain=True)

    def publish_episode(rec):
        cli.publish(f"{base}/episode", json.dumps(dict(event_type=rec["type"], **rec)))
        cli.publish(f"{base}/last_episode", json.dumps(rec), retain=True)
        publish_recent()

    def on_connect(c, u, flags, rc, props=None):
        S = f"{base}/state"
        disc("sensor", "index", dict(name="Index", state_topic=S,
             value_template="{{ value_json.index }}", unit_of_measurement="m³",
             device_class="water", suggested_display_precision=3, icon="mdi:water"))
        disc("sensor", "tracked_consumption", dict(name="Tracked consumption", state_topic=S,
             value_template="{{ value_json.total_l }}", unit_of_measurement="L",
             device_class="water", state_class="total_increasing", icon="mdi:water-plus"))
        disc("sensor", "today", dict(name="Consumption today", state_topic=S,
             value_template="{{ value_json.today_l }}", unit_of_measurement="L",
             device_class="water", state_class="total_increasing", icon="mdi:calendar-today"))
        disc("sensor", "last_15_min", dict(name="Last 15 min", state_topic=S,
             value_template="{{ value_json.last_15m_l }}", unit_of_measurement="L",
             state_class="measurement", icon="mdi:timer-sand"))
        disc("sensor", "last_hour", dict(name="Last hour", state_topic=S,
             value_template="{{ value_json.last_1h_l }}", unit_of_measurement="L",
             state_class="measurement", icon="mdi:clock-outline"))
        disc("sensor", "flow_rate", dict(name="Flow rate", state_topic=S,
             value_template="{{ value_json.flow_lpm }}", unit_of_measurement="L/min",
             device_class="volume_flow_rate", state_class="measurement", icon="mdi:water-pump"))
        disc("sensor", "current_episode", dict(name="Current episode", state_topic=S,
             value_template="{{ value_json.episode_l }}", unit_of_measurement="L",
             state_class="measurement", icon="mdi:water-outline"))
        disc("sensor", "current_episode_duration", dict(name="Current episode duration", state_topic=S,
             value_template="{{ value_json.episode_min }}", unit_of_measurement="min",
             device_class="duration", state_class="measurement", icon="mdi:timer-outline"))
        disc("sensor", "hours_without_pause", dict(name="Hours without pause", state_topic=S,
             value_template="{{ value_json.no_pause_h }}", unit_of_measurement="h",
             device_class="duration", state_class="measurement", icon="mdi:pipe-leak"))
        disc("sensor", "showers_today", dict(name="Showers today", state_topic=S,
             value_template="{{ value_json.showers_today }}", state_class="measurement",
             icon="mdi:shower-head"))
        disc("sensor", "episodes_today", dict(name="Episodes today", state_topic=S,
             value_template="{{ value_json.episodes_today }}", state_class="measurement",
             icon="mdi:format-list-numbered"))
        disc("binary_sensor", "flowing", dict(name="Water flowing", state_topic=S,
             value_template="{{ value_json.flowing }}", device_class="moisture", icon="mdi:water-pump"))
        disc("sensor", "last_episode", dict(name="Last episode", state_topic=f"{base}/last_episode",
             value_template="{{ value_json.liters }}", unit_of_measurement="L",
             device_class="water", json_attributes_topic=f"{base}/last_episode", icon="mdi:history"))
        disc("event", "episode", dict(name="Consumption episode", state_topic=f"{base}/episode",
             event_types=TYPES, icon="mdi:water-sync"))
        disc("sensor", "recent_episodes", dict(name="Recent episodes", state_topic=f"{base}/recent",
             value_template="{{ value_json.count }}", json_attributes_topic=f"{base}/recent",
             icon="mdi:format-list-bulleted"))
        disc("sensor", "last_telegram", dict(name="Last telegram", state_topic=S,
             value_template="{{ value_json.last_frame }}", device_class="timestamp",
             entity_category="diagnostic"))
        disc("sensor", "signal", dict(name="Signal", state_topic=S,
             value_template="{{ value_json.rssi }}", entity_category="diagnostic", icon="mdi:signal"))
        disc("number", "set_index", dict(name="Set index", command_topic=f"{base}/set_index",
             state_topic=S, value_template="{{ value_json.index }}",
             min=0, max=99999, step=0.001, mode="box", unit_of_measurement="m³",
             entity_category="config", icon="mdi:counter"))
        subs = [(f"{base}/set_index", 0)]
        if a.boiler_topic:
            subs.append((a.boiler_topic, 0))
        c.subscribe(subs)
        c.publish(f"{base}/availability", "online", retain=True)
        publish_state()
        publish_recent()

    def on_message(c, u, msg):
        txt = msg.payload.decode(errors="replace").strip()
        now = time.time()
        if a.boiler_topic and msg.topic == a.boiler_topic:
            try:
                v = float(txt)
            except ValueError:
                return
            with lock:
                boiler["temp"] = v
                boiler["hist"].append((now, v))
                boiler["hist"] = [x for x in boiler["hist"] if now - x[0] < 6 * 3600]
            return
        try:
            v = float(txt.replace(",", "."))
        except ValueError:
            return
        with lock:
            st.d.update(anchor_m3=v, anchor_time=datetime.now().isoformat(timespec="seconds"), steps=0)
            st.save()
        print(f"anchor set: {v} m3", flush=True)
        publish_state()

    cli.on_connect = on_connect
    cli.on_message = on_message
    cli.connect_async(os.environ["MQTT_HOST"], int(os.environ.get("MQTT_PORT", 1883)))
    cli.loop_start()

    def ticker():
        while True:
            time.sleep(20)
            with lock:
                close_episode(time.time())
                recs = finalize_due(time.time())
            for rec in recs:
                print(f"episode: {rec}", flush=True)
                publish_episode(rec)
            publish_state()
    threading.Thread(target=ticker, daemon=True).start()

    def on_step(now):
        with lock:
            roll_day(now)
            st.d["steps"] += 1
            st.d["total_steps"] += 1
            st.d["day_steps"] += 1
            st.d["step_times"] = [x for x in st.d["step_times"] if now - x < KEEP_STEPS_S] + [now]
            ep = st.d["episode"]
            if ep and now - ep["t1"] <= a.flow_window:
                ep["t1"] = now
                ep["n"] += 1
            else:
                close_episode(now, force=True)
                st.d["episode"] = dict(t0=now, t1=now, n=1)
            st.save()
        print(f"{datetime.now():%H:%M:%S} +10 L -> {st.index_m3()} m3", flush=True)
        publish_state()

    fd, procs = spawn_chain(a)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    rawf = open(a.raw_log, "a", buffering=1) if a.raw_log else None
    buf = b""
    last_data = time.time()
    try:
        while True:
            if any(p.poll() is not None for p in procs):
                raise SystemExit("rtl_sdr / rtl_wmbus exited")
            if not select.select([fd], [], [], 30)[0]:
                if time.time() - last_data > 600:
                    raise SystemExit("no output from rtl_wmbus for 10 minutes")
                continue
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                raise SystemExit("pty closed")
            last_data = time.time()
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for raw_line in lines:
                line = raw_line.decode(errors="replace").strip()
                fr = parse_frame(line, a.id)
                if not fr:
                    continue
                m1, m2, acc, pl = fr
                if rawf:
                    rawf.write(line + "\n")
                now = time.time()
                kind = tr.feed(now, acc, pl)
                with lock:
                    live.update(last_frame=now, rssi=min(m1, m2))
                    live["frames"] += 1
                if kind == "step":
                    on_step(now)
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        cli.publish(f"{base}/availability", "offline", retain=True)
        time.sleep(0.5)


if __name__ == "__main__":
    main()
