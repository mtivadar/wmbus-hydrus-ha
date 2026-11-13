#!/usr/bin/env python3
"""
replay.py - run the wmbus-ha step/episode rules offline on rtl_wmbus logs.

Useful to check a new meter (does it produce clean 10 L steps?), to tune
--confirm / --flow-window, or just to look at your consumption history.

    # capture a while:
    rtl_sdr -f 868.95M -s 1.6e6 -g 49.6 - | rtl_wmbus > capture.txt
    # then:
    python3 replay.py capture.txt --id 12345678
    python3 replay.py data/sample_telegrams.txt --id 12345678 --anchor 80.330 --episodes

The same Tracker / classify code as the live service is used (imported from
wmbus_ha.py), so what you see here is what Home Assistant would get - minus the
hot-water hint, which needs the live boiler temperature.
"""

import argparse
from collections import Counter
from datetime import datetime

from wmbus_ha import STEP_L, Tracker, classify, parse_frame

TS_FMT = "%Y-%m-%d %H:%M:%S.%f"


def load(paths, meter_id):
    """[(timestamp, rssi, access_number, payload)] from rtl_wmbus logs, sorted."""
    frames = []
    for path in paths:
        with open(path, errors="replace") as f:
            for line in f:
                fr = parse_frame(line, meter_id)
                if not fr:
                    continue
                try:
                    ts = datetime.strptime(line.split(";")[3], TS_FMT).timestamp()
                except (IndexError, ValueError):
                    continue
                m1, m2, acc, pl = fr
                frames.append((ts, min(m1, m2), acc, pl))
    frames.sort(key=lambda x: x[0])
    return frames


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("logs", nargs="+")
    ap.add_argument("--id", required=True, help="meter id as printed by rtl_wmbus")
    ap.add_argument("--confirm", type=int, default=2)
    ap.add_argument("--flow-window", type=float, default=150.0)
    ap.add_argument("--anchor", type=float, help="index in m3 at the first frame")
    ap.add_argument("--episodes", action="store_true", help="list every episode")
    a = ap.parse_args()

    frames = load(a.logs, a.id)
    if not frames:
        raise SystemExit(f"no frames for meter {a.id}")
    tr = Tracker(a.confirm)
    steps, other = [], 0
    for ts, _, acc, pl in frames:
        k = tr.feed(ts, acc, pl)
        if k == "step":
            steps.append(ts)
        elif k == "other":
            other += 1

    eps = []
    for s in steps:
        if eps and s - eps[-1][1] <= a.flow_window:
            eps[-1][1], eps[-1][2] = s, eps[-1][2] + 1
        else:
            eps.append([s, s, 1])

    t0, t1 = frames[0][0], frames[-1][0]
    span_h = (t1 - t0) / 3600
    fmt = lambda t: datetime.fromtimestamp(t).strftime("%d.%m %H:%M")
    print(f"meter {a.id}: {len(frames)} frames, {fmt(t0)} -> {fmt(t1)} ({span_h:.1f} h)")

    # Compatibility check: the method needs the meter to repeat the same access
    # number (hence identical ciphertext) over several frames.
    runs, prev = [], None
    for _, _, acc, _ in frames:
        if acc == prev:
            runs[-1] += 1
        else:
            runs.append(1)
            prev = acc
    per_acc = len(frames) / len(runs)
    distinct_pl = len({pl for *_, pl in frames})
    print(f"access numbers: {len(runs)} runs, {per_acc:.1f} frames per run, "
          f"{distinct_pl} distinct payloads")
    if per_acc < 3:
        print("  !! the access number changes almost every frame - the ciphertext never repeats,\n"
              "     so payload changes cannot be told apart from new transmissions. This meter is\n"
              "     probably NOT compatible with the step-counting method.")
    print(f"steps: {len(steps)} = {len(steps) * STEP_L:.0f} L, other field changes: {other}")
    if span_h >= 1 and len(steps) / span_h > 60:
        print("  !! more than 60 steps per hour on average - block 1 probably holds something that\n"
              "     changes all the time (a clock or counter), not only the volume.")
    if a.anchor is not None:
        print(f"index: {a.anchor:.3f} -> {a.anchor + len(steps) * STEP_L / 1000:.3f} m3")
    kinds = Counter(classify(e[2] * STEP_L, datetime.fromtimestamp(e[0]).hour, None) for e in eps)
    print(f"episodes: {len(eps)}  " + ", ".join(f"{k} {v}" for k, v in kinds.most_common()))
    if a.episodes:
        print("\nstart          litres  minutes  guess")
        for s, e, n in eps:
            dur = (e - s) / 60 + 1
            print(f"{fmt(s)}  {n * STEP_L:6.0f}  {dur:7.1f}  "
                  f"{classify(n * STEP_L, datetime.fromtimestamp(s).hour, None)}")


if __name__ == "__main__":
    main()
