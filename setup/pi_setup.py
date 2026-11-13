#!/usr/bin/env python3
"""
pi_setup.py - provision a fresh Raspberry Pi for wmbus-ha, from your computer, over SSH.

Prerequisites
  * Raspberry Pi OS Lite (64-bit) freshly flashed, booted with the cloud-init
    files from setup/cloud-init/ (user + your SSH public key, optional Wi-Fi)
  * the Pi reachable over SSH (Ethernet or Wi-Fi)
  * an MQTT broker reachable from the Pi (e.g. the Home Assistant Mosquitto add-on)

Steps
  1. system update + packages (build tools, python3-paho-mqtt, mosquitto-clients)
  2. blacklist the DVB-T kernel driver that grabs the RTL2832U dongle
  3. RTL-SDR Blog driver (required for V4 / V4 Lite dongles; harmless for others)
  4. rtl_wmbus (weetmuts) built from source
  5. MQTT credentials written to /etc/wmbus-ha/mqtt.env (root only)
     - either the ones you pass (--mqtt-user, MQTT_PASS env var)
     - or, with --ha-mosquitto-addon, a fresh random login created in the HA
       Mosquitto add-on through the Supervisor API (needs HA_URL / HA_TOKEN)
  6. wmbus_ha.py + systemd unit, then reboot
  7. optional calibration (--index <m3 read on the meter's LCD>)

Example
    MQTT_PASS=... python3 setup/pi_setup.py --host 192.168.1.50 --user pi \\
        --key ~/.ssh/id_ed25519 --meter-id 12345678 --mqtt-host 192.168.1.10 --mqtt-user wmbus \\
        --index 80.330
"""

import argparse
import json
import os
import secrets
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

UNIT = """[Unit]
Description=wM-Bus water meter -> Home Assistant (MQTT)
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=/etc/wmbus-ha/mqtt.env
ExecStart=/usr/bin/python3 /usr/local/bin/wmbus_ha.py
Restart=always
RestartSec=15
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
"""

DVB_BLACKLIST = "blacklist dvb_usb_rtl28xxu\nblacklist rtl2832\nblacklist rtl2830\nblacklist rtl2832_sdr\n"


class Pi:
    def __init__(self, host, user, key):
        self.target = f"{user}@{host}"
        self.base = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                     "-o", "ServerAliveInterval=30"] + (["-i", os.path.expanduser(key)] if key else [])

    def run(self, cmd, inp=None, check=True):
        r = subprocess.run(self.base + [self.target, cmd], input=inp, capture_output=True, text=True)
        if check and r.returncode != 0:
            sys.exit(f"ERROR running: {cmd[:80]}...\n{r.stderr[-1500:]}")
        return r.stdout

    def reachable(self):
        r = subprocess.run(self.base + ["-o", "ConnectTimeout=5", self.target,
                                        "systemctl is-active wmbus-ha; lsmod | grep -c dvb_usb_rtl28xxu"],
                           capture_output=True, text=True)
        return r.stdout.strip() if r.returncode in (0, 1) and r.stdout else None


def ha_ws(cmds):
    """Home Assistant WebSocket calls through tools/ha_ws.py (HA_URL / HA_TOKEN from env)."""
    out = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "tools", "ha_ws.py"), json.dumps(cmds)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def mosquitto_addon_login(user, password, slug="core_mosquitto"):
    """Create/replace a login in the HA Mosquitto add-on and restart it."""
    opts = ha_ws([{"type": "supervisor/api", "endpoint": f"/addons/{slug}/info", "method": "get"}])[0]["options"]
    opts["logins"] = [l for l in opts.get("logins", []) if l["username"] != user] + \
                     [{"username": user, "password": password}]
    ha_ws([{"type": "supervisor/api", "endpoint": f"/addons/{slug}/options", "method": "post",
            "data": {"options": opts}}])
    ha_ws([{"type": "supervisor/api", "endpoint": f"/addons/{slug}/restart", "method": "post", "timeout": 120}])


def step(n, title):
    print(f"\n[{n}/7] {title}", flush=True)


def main():
    ap = argparse.ArgumentParser(description="Provision a Raspberry Pi for wmbus-ha.")
    ap.add_argument("--host", required=True, help="Pi address")
    ap.add_argument("--user", default="pi", help="SSH user created by cloud-init")
    ap.add_argument("--key", help="SSH private key (default: your ssh config)")
    ap.add_argument("--meter-id", required=True, help="meter id as printed by rtl_wmbus")
    ap.add_argument("--mqtt-host", required=True)
    ap.add_argument("--mqtt-port", type=int, default=1883)
    ap.add_argument("--mqtt-user", default="wmbus")
    ap.add_argument("--ha-mosquitto-addon", action="store_true",
                    help="create a random login in the HA Mosquitto add-on (needs HA_URL/HA_TOKEN)")
    ap.add_argument("--index", type=float, help="meter LCD reading in m3, to calibrate at the end")
    a = ap.parse_args()
    pi = Pi(a.host, a.user, a.key)

    step(1, "system update and packages")
    pi.run("export DEBIAN_FRONTEND=noninteractive; sudo -E apt-get update -qq && sudo -E apt-get -y -qq full-upgrade "
           "&& sudo -E apt-get -y -qq install git build-essential cmake libusb-1.0-0-dev pkg-config "
           "python3-paho-mqtt mosquitto-clients >/dev/null 2>&1")

    step(2, "blacklist DVB-T driver")
    pi.run("sudo tee /etc/modprobe.d/blacklist-rtlsdr-dvb.conf >/dev/null", inp=DVB_BLACKLIST)

    step(3, "RTL-SDR Blog driver")
    pi.run("sudo apt-get -y -qq purge rtl-sdr librtlsdr0 librtlsdr-dev >/dev/null 2>&1; cd ~ && rm -rf rtl-sdr-blog "
           "&& git clone -q https://github.com/rtlsdrblog/rtl-sdr-blog && cd rtl-sdr-blog && mkdir build && cd build "
           "&& cmake .. -DINSTALL_UDEV_RULES=ON -DDETACH_KERNEL_DRIVER=ON >/dev/null 2>&1 && make -j3 >/dev/null 2>&1 "
           "&& sudo make install >/dev/null && sudo cp ../rtl-sdr.rules /etc/udev/rules.d/ && sudo ldconfig")

    step(4, "rtl_wmbus")
    pi.run("cd ~ && rm -rf rtl-wmbus && git clone -q https://github.com/weetmuts/rtl-wmbus.git && cd rtl-wmbus "
           "&& make -s release >/dev/null 2>&1 && sudo install -m 755 build/rtl_wmbus /usr/local/bin/rtl_wmbus")

    step(5, "MQTT credentials")
    if a.ha_mosquitto_addon:
        password = secrets.token_urlsafe(24)
        mosquitto_addon_login(a.mqtt_user, password)
        print(f"login '{a.mqtt_user}' created in the Mosquitto add-on (password stored only on the Pi)")
    else:
        password = os.environ.get("MQTT_PASS")
        if not password:
            sys.exit("set MQTT_PASS in the environment, or use --ha-mosquitto-addon")
    env = (f"MQTT_HOST={a.mqtt_host}\nMQTT_PORT={a.mqtt_port}\nMQTT_USER={a.mqtt_user}\n"
           f"MQTT_PASS={password}\nWMBUS_METER_ID={a.meter_id}\n")
    pi.run("sudo install -d -m 700 /etc/wmbus-ha && sudo tee /etc/wmbus-ha/mqtt.env >/dev/null "
           "&& sudo chmod 600 /etc/wmbus-ha/mqtt.env", inp=env)

    step(6, "wmbus-ha service")
    with open(os.path.join(ROOT, "wmbus_ha.py")) as f:
        pi.run("sudo tee /usr/local/bin/wmbus_ha.py >/dev/null && sudo chmod 755 /usr/local/bin/wmbus_ha.py",
               inp=f.read())
    pi.run("sudo install -d -m 755 /var/lib/wmbus-ha && sudo tee /etc/systemd/system/wmbus-ha.service >/dev/null "
           "&& sudo systemctl daemon-reload && sudo systemctl enable wmbus-ha >/dev/null 2>&1", inp=UNIT)
    pi.run("sudo systemctl reboot", check=False)
    print("rebooting, waiting 90 s ...")
    time.sleep(90)
    for _ in range(20):
        out = pi.reachable()
        if out:
            print("service / dvb modules loaded:", out.replace("\n", " / "))
            break
        time.sleep(10)
    else:
        sys.exit("Pi did not come back - check it, then run the remaining steps by hand")

    step(7, "calibration")
    if a.index is None:
        print("skipped - set the index later from HA (Water meter -> Set index)")
        return
    pi.run(f"sudo bash -c '. /etc/wmbus-ha/mqtt.env; mosquitto_pub -h $MQTT_HOST -p $MQTT_PORT "
           f"-u $MQTT_USER -P $MQTT_PASS -t wmbus/$WMBUS_METER_ID/set_index -m {a.index}'")
    print(f"index set to {a.index} m3 - done.")


if __name__ == "__main__":
    main()
