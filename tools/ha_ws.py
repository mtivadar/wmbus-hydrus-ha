"""Minimal stdlib WebSocket client for Home Assistant.
Usage: python3 ha_ws.py '<json list of commands without id>'   (or @file.json)
Prints a JSON list of results. Reads HA_URL / HA_TOKEN from env."""
import base64, json, os, socket, struct, sys
from urllib.parse import urlparse

u = urlparse(os.environ["HA_URL"])
host, port = u.hostname, u.port or 80
s = socket.create_connection((host, port), timeout=15)
key = base64.b64encode(os.urandom(16)).decode()
s.sendall((f"GET /api/websocket HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
buf = b""
while b"\r\n\r\n" not in buf:
    buf += s.recv(4096)
hdr, buf = buf.split(b"\r\n\r\n", 1)
assert b" 101 " in hdr.split(b"\r\n")[0], hdr

def recv_exact(n):
    global buf
    while len(buf) < n:
        c = s.recv(65536)
        if not c:
            raise EOFError
        buf += c
    out, buf = buf[:n], buf[n:]
    return out

def recv_msg():
    data = b""
    while True:
        b1, b2 = recv_exact(2)
        ln = b2 & 0x7F
        if ln == 126: ln = struct.unpack(">H", recv_exact(2))[0]
        elif ln == 127: ln = struct.unpack(">Q", recv_exact(8))[0]
        data += recv_exact(ln)
        if b1 & 0x80:
            return json.loads(data)

def send_msg(obj):
    p = json.dumps(obj).encode()
    m = os.urandom(4)
    h = bytes([0x81])
    if len(p) < 126: h += bytes([0x80 | len(p)])
    elif len(p) < 65536: h += bytes([0x80 | 126]) + struct.pack(">H", len(p))
    else: h += bytes([0x80 | 127]) + struct.pack(">Q", len(p))
    s.sendall(h + m + bytes(b ^ m[i % 4] for i, b in enumerate(p)))

recv_msg()  # auth_required
send_msg({"type": "auth", "access_token": os.environ["HA_TOKEN"]})
r = recv_msg()
assert r["type"] == "auth_ok", r
arg = sys.argv[1]
cmds = json.load(open(arg[1:])) if arg.startswith("@") else json.loads(arg)
results = []
for i, c in enumerate(cmds, 1):
    send_msg({"id": i, **c})
    while True:
        r = recv_msg()
        if r.get("id") == i and r.get("type") == "result":
            break
    results.append(r.get("result") if r.get("success") else {"ERROR": r.get("error")})
print(json.dumps(results, ensure_ascii=False))
