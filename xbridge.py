#!/usr/bin/env python3
"""xbridge - Xcode wireless debugging over a mesh VPN (Tailscale), free.

An independent, open-source implementation of the mesh-VPN bridge approach:
  1. publish your iPhone's captured Bonjour (_remotepairing._tcp) identity on the
     Mac, pointing at 127.0.0.1 so Xcode/devicectl "discovers" it locally;
  2. relay TCP + UDP for the RemotePairing control port and the CoreDevice tunnel
     range (default 55000-58001) to the phone over Tailscale - TCP fully
     bidirectional; UDP in both directions (Xcode-facing socket on 127.0.0.1,
     reply-facing socket allow-listed to the phone's tailnet peer only).

All traffic stays inside your own tailnet; Xcode-facing listeners bind 127.0.0.1 only.

Usage:
  xbridge.py capture [--seconds N]    # browse local Bonjour for phones and save profiles
  xbridge.py bridge [--all|--uuid U] [--auto] [--toggle-interval N]
      # run the bridge (long-running; Ctrl-C to stop). With --auto it checks every
      # N seconds: arms only while the iPhone is OFF your local network (reachable via
      # Tailscale) and releases everything when it comes back - one launch covers the commute.
  xbridge.py scan [--uuid U]          # one-shot reachability check (port ping)
  xbridge.py status                   # show config + phone liveness

v1.2 (after analysing the incumbent commercial bridge + macOS 27's remotepairingd in detail):
  - --advertise-mode direct6: publish the phone's stable tailnet IPv6 so Xcode dials
    the iPhone straight over the mesh (no relays; also dodges macOS's ~42s liveness
    flap that hits every self-address publication, commercial bridges included).
  - link-port mirror (UDP :3722): the phone's daemon can answer macOS liveness probes.
  - TunnelWatcher: learns dynamically-negotiated tunnel ports from system logs (the
    macOS-27 successor of the commercial bridge's 'Got tunnel endpoint' watcher) and relays them.
  - relay idle-timeout raised to 300s (was 60) + post-arm self-check of the relay path.

v1.4 (after live A/B testing against the incumbent commercial bridge on macOS 27):
  - DEFAULT publish path is now mDNSResponder ITSELF: xbridge keeps a `dns-sd -P`
    child alive (the same mechanism the commercial bridge uses). mDNSResponder's own
    registrations persist in its cache, so Network.framework's bonjour-scoped name
    resolution stays satisfied for HOURS (verified: the commercial bridge kept pairing ~every 42s
    all night). A raw multicast publish (python-zeroconf) decays out of that cache
    within minutes, after which every dial fails with 'NoSuchRecord' and the bridge
    goes silent - that is why v1.3's dual A/AAAA record worked for ~8 min then died.
  - --advertise-mode: auto|relay = parity anchor matching commercial bridges (A record via dns-sd -P,
    falls back to zeroconf A-only if the binary is unavailable); hybrid = experimental
    dual A+AAAA via zeroconf; direct6 = experimental AAAA-only (phone dials nothing
    of ours).
  - All relay machinery is unchanged: control relay, tunnel pool 55000-58000,
    TunnelWatcher, UDP :3722 liveness mirror. Note: macOS 27's CUNetLinkManager
    kills every ~42s because it liveness-probes the (self) address with ARP and no
    second host answers - the commercial bridge flaps identically; expect fast re-pair cycles.

Config file: ~/.xbridge/config.json  (see README.md)
"""

import argparse
import json
import os
import re
import selectors
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

CONFIG_PATH = os.path.expanduser("~/.xbridge/config.json")

DEFAULT_TUNNEL_START = 55000
DEFAULT_TUNNEL_END = 58001
# macOS's CUNetLinkManager (inside remotepairingd) liveness-monitors <addr>:3722 on
# every paired device; we mirror that UDP port to the phone so its daemon can answer.
DEFAULT_LINK_PORT = 3722


def log(msg):
    print(time.strftime("%H:%M:%S"), "xbridge:", msg, file=sys.stderr, flush=True)


def die(msg):
    log("ERROR: " + msg)
    sys.exit(1)


STATE_PATH = os.path.expanduser("~/.xbridge/state.json")


def write_state(state, devices=None):
    """Machine-readable status for the XBridge menu-bar app (one-click on/off).

    `state`: "starting" | "armed" (relaying over Tailscale) | "idle"
    (phone on LAN or unreachable; no ports held). Written atomically; any
    failure here is swallowed - the GUI must never break the bridge.
    """
    try:
        doc = {
            "version": 1,
            "state": state,
            "pid": os.getpid(),
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        if devices is not None:
            doc["devices"] = devices
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(doc, f)
        os.replace(tmp, STATE_PATH)
    except Exception:
        pass


def aggregate_state(ctxs):
    """'armed' if any device is relaying, else 'idle'; plus the per-device view."""
    armed = any(c.get("pump") is not None for c in ctxs)
    devs = []
    for c in ctxs:
        on = c.get("pump") is not None
        devs.append({
            "label": c.get("label"),
            "bucket": c.get("bucket") or ("remote-up" if on else "local"),
            "bridge": "on" if on else "off",
        })
    return ("armed" if armed else "idle"), devs


# ---------------------------------------------------------------- config

def default_config():
    return {
        "devices": [],
        "bind_host": "127.0.0.1",
        "advertise_ip": "127.0.0.1",
    }


def load_config(path=CONFIG_PATH):
    if not os.path.exists(path):
        return default_config()
    with open(path) as f:
        cfg = json.load(f)
    for k, v in default_config().items():
        cfg.setdefault(k, v)
    return cfg


def save_config(cfg, path=CONFIG_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)


# ---------------------------------------------------------------- tailscale

_TS_EXE = {"path": None, "resolved": False}


def _tailscale_exe():
    """Locate the tailscale CLI. launchd agents run with a minimal PATH
    (/usr/bin:/bin:...) where 'tailscale' is not visible, so also probe the
    well-known install locations. Result is cached."""
    if _TS_EXE["resolved"]:
        return _TS_EXE["path"]
    cands = ["/usr/local/bin/tailscale", "/opt/homebrew/bin/tailscale",
             os.path.expanduser("~/go/bin/tailscale")]
    for cand in cands:
        if os.path.exists(cand):
            _TS_EXE["path"] = cand
            break
    else:
        try:  # last resort: whatever PATH gives us (terminal runs)
            out = subprocess.run(["which", "tailscale"],
                                 capture_output=True, text=True, timeout=5)
            if out.returncode == 0 and out.stdout.strip():
                _TS_EXE["path"] = out.stdout.splitlines()[0].strip()
        except Exception:
            pass
    _TS_EXE["resolved"] = True
    return _TS_EXE["path"]


_TS_CACHE = {"data": None, "at": 0.0}
_TS_LOCK = threading.Lock()


def tailscale_status_json(max_age=10.0):
    """`tailscale status --json --self`, cached for `max_age` seconds.

    The per-packet UDP path calls this through the device's remote-ip holder;
    without a cache every datagram would spawn a CLI subprocess. On failure the
    last good snapshot keeps serving (a phone's tailnet IP barely changes),
    which also bounds CLI churn when tailscaled itself is wedged."""
    now = time.time()
    with _TS_LOCK:
        if (_TS_CACHE["data"] is not None
                and now - _TS_CACHE["at"] < max_age):
            return _TS_CACHE["data"]
    exe = _tailscale_exe()
    if not exe:
        log("warning: tailscale CLI not found - using static fallback IP from config")
        return None
    try:
        out = subprocess.run(
            [exe, "status", "--json", "--self"],
            capture_output=True, text=True, timeout=15
        )
    except Exception as e:
        log(f"tailscale CLI unavailable ({e}) - using static fallback IP from config")
        with _TS_LOCK:
            if _TS_CACHE["data"] is not None:
                _TS_CACHE["at"] = now  # throttle retries; serve last good data
        return _TS_CACHE["data"]
    try:
        data = json.loads(out.stdout) if out.returncode == 0 else None
    except Exception:
        data = None
    if data is None:
        with _TS_LOCK:
            if _TS_CACHE["data"] is not None:
                _TS_CACHE["at"] = now
        return _TS_CACHE["data"]
    with _TS_LOCK:
        _TS_CACHE["data"], _TS_CACHE["at"] = data, time.time()
    return data


def self_tailnet_ip():
    """This Mac's tailnet IPv4 (for binding reply sockets); None when unresolvable."""
    data = tailscale_status_json()
    if not data:
        return None
    s = data.get("Self")
    if not isinstance(s, dict):
        return None
    ip = s.get("TailscaleIP")
    if not ip:
        ips = [str(x) for x in (s.get("TailscaleIPs") or [])]
        ip = next((x for x in ips if ":" not in x), ips[0] if ips else None)
    return ip or None


def _primary_lan_ipv4():
    """IPv4 address of the interface owning the default route (Wi-Fi/LAN).

    This is what a local client (Xcode) will actually reach when it dials the
    address in our record."""
    try:
        out = subprocess.run(["/sbin/route", "-n", "get", "default"],
                             capture_output=True, text=True, timeout=5).stdout
        iface = None
        for line in out.splitlines():
            if "interface:" in line:
                iface = line.split("interface:")[-1].strip()
        if not iface:
            return None
        out2 = subprocess.run(["/sbin/ifconfig", iface],
                              capture_output=True, text=True, timeout=5).stdout
        for line in out2.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0] == "inet" and not parts[1].startswith("127."):
                return parts[1]
    except Exception:
        pass
    return None


def _addr_score(ip):
    """Rank candidate advertise addresses: classic RFC1918 LAN first, then
    tailscale/CGNAT 100.64/10, then anything else; loopback last."""
    try:
        o = [int(x) for x in ip.split(".")]
    except Exception:
        return 9
    if o[0] == 127:
        return 8  # loopback - many clients ignore records resolving here
    if o[0] == 10 or (o[0] == 192 and o[1] == 168) or (o[0] == 172 and 16 <= o[1] <= 31):
        return 0  # private LAN - exactly what commercial bridges publish and clients dial
    if o[0] == 100 and 64 <= o[1] <= 127:
        return 1  # tailscale/CGNAT - reachable, but filtered by some clients
    return 2


def pick_advertise_ip(pref):
    """Address to put in the published SRV record.

    'auto': this Mac's primary LAN IPv4 first (the network Xcode is on), then its
    other private addresses, then tailnet; loopback only as a last resort. Clients
    (CoreDevice) filter records whose target resolves to loopback-only or CGNAT-only
    addresses; commercial bridges publish the LAN IP for exactly this reason."""
    pref = (pref or "auto").strip()
    if pref and pref.lower() != "auto":
        return pref
    cands = []
    lan = _primary_lan_ipv4()
    if lan:
        cands.append(lan)
    try:
        for fam, _t, _p, _cns, sa in socket.getaddrinfo(socket.gethostname(), 0):
            if fam == socket.AF_INET:
                cands.append(sa[0])
    except Exception:
        pass
    try:
        t = self_tailnet_ip()
        if t and ":" not in t:
            cands.append(t)
    except Exception:
        pass
    if not cands:
        return "127.0.0.1"
    ranked = dict.fromkeys(cands)  # dedup, keep order (primary LAN first)
    return sorted(ranked, key=_addr_score)[0]


def _own_ipv4_addrs():
    """This machine's IPv4 addresses (loopback + LAN + tailnet) as raw bytes."""
    out = [socket.inet_aton("127.0.0.1")]
    try:
        ip = self_tailnet_ip()
        if ip and ":" not in ip:
            out.append(socket.inet_aton(ip))
    except Exception:
        pass
    try:
        for fam, _t, _p, _cns, sa in socket.getaddrinfo(socket.gethostname(), 0):
            if fam == socket.AF_INET:
                out.append(socket.inet_aton(sa[0]))
    except Exception:
        pass
    return out


def _node_entries(data):
    """Yield (names, ip, online) tuples across tailscale CLI schemas.

    Old CLI: top-level "nodes" list of {Name, TailscaleIP, Online}.
    New CLI (1.102+): no nodes array; top-level "Peer" dict keyed by nodekey,
    entries carry {HostName, DNSName, TailscaleIPs[], Online}.
    """
    if not data:
        return
    for key in ("nodes", "Nodes"):
        for n in data.get(key) or []:
            if not isinstance(n, dict):
                continue
            ip = n.get("TailscaleIP") or (n.get("TailscaleIPs") or [None])[0]
            names = [str(n.get(k) or "") for k in ("Name", "HostName")] + \
                    [str(x) for x in (n.get("DNSNames") or [])]
            online = n.get("Online")
            yield names, ip, (bool(online) if online is not None else None)
    peer = data.get("Peer")
    if isinstance(peer, dict):
        entries = list(peer.values())
    elif isinstance(peer, list):
        entries = peer
    else:
        entries = []
    for n in entries:
        if not isinstance(n, dict):
            continue
        ips = [str(x) for x in (n.get("TailscaleIPs") or [])]
        ip = n.get("TailscaleIP") or next((i for i in ips if ":" not in i),
                                          ips[0] if ips else None)
        names = [str(n.get(k) or "") for k in ("Name", "HostName")] + \
                [str(n.get("DNSName") or "")]
        online = n.get("Online")
        yield names, ip, (bool(online) if online is not None else None)


def find_tailnet_node(data, node_name):
    """Return (ip, online) for a tailnet node: exact name, then DNS-name prefix."""
    if not data or not node_name:
        return None, None
    want = str(node_name).lower().strip(".")
    for names, ip, online in _node_entries(data):
        if any(x and x.lower().rstrip(".") == want for x in names):
            return ip, online
    for names, ip, online in _node_entries(data):  # DNS-name / host prefix
        if any(x and x.lower().startswith(want) for x in names):
            return ip, online
    for names, ip, online in _node_entries(data):  # loose: node is prefix of a disambiguated Name
        if any(x and want.startswith(x.lower().rstrip(".")) for x in names):
            return ip, online
    return None, None


def tailnet_nodes_brief(data):
    """[(name, ip, online)] for every peer - feeds the GUI's node picker and hints.

    name is the short machine name: the DNSName host part (the stable tailnet
    hostname), falling back to HostName. Self is separate from peers in the CLI
    JSON, so it never appears here; duplicates dedupe case-insensitively."""
    brief, seen = [], set()
    for names, ip, online in _node_entries(data):
        host = dns = ""
        for x in names:
            s = str(x or "").strip().rstrip(".")
            if not s:
                continue
            parts = s.split(".")
            if len(parts) >= 2 and ("ts.net" in s or "tailnet." in s):
                dns = parts[0]
            elif not host:
                host = s
        name = dns or host
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        brief.append((name, ip or "", online))
    return brief


def resolve_remote_ip(dev):
    """(ip, online|None) - via tailscale when possible, else static fallback."""
    if dev.get("provider") == "tailscale" and dev.get("tailscale_node"):
        ip, online = find_tailnet_node(tailscale_status_json(), dev["tailscale_node"])
        if ip:
            return ip, online
    fb = dev.get("fallback_ip")
    if fb:
        return fb, None  # liveness unknown; probes will tell us
    return None, False


def _squash(s):
    """Lowercase and strip dots/spaces/dashes for lenient node-name comparison."""
    return re.sub(r"[\s.\-]+", "", str(s or "").lower())


def tailscale_node_v6(dev):
    """The device's stable tailnet IPv6 (coordinator-assigned; survives the phone
    changing networks) or None. Used by --advertise-mode direct6."""
    manual = str(dev.get("tailscale_v6") or "").strip()
    if manual:
        return manual if ":" in manual else None
    if dev.get("provider") != "tailscale" or not dev.get("tailscale_node"):
        return None
    data = tailscale_status_json()
    if not data:
        return None
    want = str(dev["tailscale_node"]).lower().strip(".")

    def match(n):
        # Lenient: exact, first label (strips the .ts.net DNS domain), or
        # space/dash-insensitive first-label compare (iPhone HostNames carry spaces,
        # e.g. 'John Apple iPhone' for node key name 'john-apple-iphone').
        labels = [str(n.get(k) or "") for k in ("Name", "HostName")]
        labels += [str(x) for x in (n.get("DNSNames") or [])]
        labels.append(str(n.get("DNSName") or ""))
        for raw in labels:
            base = str(raw).lower().strip().rstrip(".")
            if not base:
                continue
            first = base.split(".")[0]
            if base == want or first == want:
                return True
            if _squash(first) and _squash(first) == _squash(want):
                return True
        return False

    nodes = []
    peer = data.get("Peer")
    if isinstance(peer, dict):
        nodes += [v for v in peer.values() if isinstance(v, dict)]  # new CLI schema
    elif isinstance(peer, list):
        nodes += [v for v in peer if isinstance(v, dict)]
    nodes += [n for n in (data.get("Nodes") or data.get("nodes") or []) if isinstance(n, dict)]
    for n in nodes:
        if match(n):
            ips = [str(x) for x in (n.get("TailscaleIPs") or [])]
            if n.get("TailscaleIP"):
                ips.append(str(n["TailscaleIP"]))
            for ip in ips:
                if ":" in ip and not ip.startswith("fe80"):
                    return ip
    return None


# ---------------------------------------------------------------- raw sockets

def raise_fd_limit():
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = 100_000 if hard == resource.RLIM_INFINITY else min(hard, 65_536)
        if soft < want:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
    except Exception:
        pass


def bind_tcp(host, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # TIME_WAIT reuse on rebind
    try:
        s.bind((host, port))
    except OSError:
        return None
    try:
        s.listen(256)  # a bound socket that never listen()s never accepts!
    except OSError:
        s.close()
        return None
    s.setblocking(False)
    return s


def bind_udp(host, port, reuse=True, reuseport=False):
    """reuse=False for Xcode-facing sockets: refuse other binds on the same 127 port.
    reuseport=True shares the port with OTHER local listeners (system daemons' :3722)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if reuse:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if reuseport:
        rp = getattr(socket, "SO_REUSEPORT", None)
        if rp is not None:
            try:
                s.setsockopt(socket.SOL_SOCKET, rp, 1)
            except OSError:
                pass
    try:
        s.bind((host, port))
    except OSError:
        return None
    s.setblocking(False)
    return s


def port_free(host, port):
    for fn in (bind_tcp, bind_udp):
        s = fn(host, port)
        if s is None:
            return False
        s.close()
    return True


# ---------------------------------------------------------------- relay engine

class Pump:
    """One selector thread per device, multiplexing all of its listeners."""

    def __init__(self, dev, remote_ip_holder, health_flag, idle_timeout=300.0):
        self.dev = dev
        self.idle_timeout = max(5.0, float(idle_timeout))
        self.remote_ip_holder = remote_ip_holder  # callable -> ip or None
        self.health_flag = health_flag            # {"up": bool}
        self.socks = []                           # [sock, sock, ...] in register order
        self._meta = {}                           # id(sock) -> (kind, port)
        self._sel = selectors.DefaultSelector()
        self.remote_off = 0                       # remote-side port offset (tests/avoidance)
        self._udpsess = {}                        # (peer_ip, peer_port) -> client addr
        self._udp_pair = {}                       # id(sock of either leg) -> (client_sock, remote_sock)

    def add(self, sock, kind, port, remote_port=None):
        self._sel.register(sock, selectors.EVENT_READ)
        self.socks.append(sock)                   # keep socket alive; id() stable
        self._meta[id(sock)] = (kind, port, remote_port if remote_port is not None else port)

    def close_all(self):
        for s in self.socks:
            try:
                s.close()
            except OSError:
                pass
        try:  # a closed selector makes run()'s select() raise -> thread exits cleanly
            self._sel.close()
        except Exception:
            pass

    def run(self):
        sel = self._sel
        while True:
            try:
                events = sel.select(timeout=1.0)
            except (InterruptedError, OSError):
                break
            for key, _ in events:
                info = self._meta.get(id(key.fileobj))
                if not info:
                    continue
                kind, port = info[0], info[1]
                sock = key.fileobj
                try:
                    if kind == "tcp":
                        conn, _addr = sock.accept()
                        threading.Thread(
                            target=self._pipe_tcp,
                            args=(conn, port, info[2]), daemon=True).start()
                    elif kind == "udp-client":
                        data, from_addr = sock.recvfrom(65536)
                        self._handle_udp_client(sock, port, data, from_addr)
                    else:  # udp-remote (reply leg)
                        data, from_addr = sock.recvfrom(65536)
                        self._route_udp_reply(sock, port, data, from_addr)
                except OSError:
                    pass  # socket closed / queued ICMP error; keep serving

    def _info_for(self, key):
        """(kind, port, remote_port) for a selector key; tracked at register time."""
        return self._meta.get(id(key.fileobj), ("?", 0, 0))

    def _handle_udp_client(self, sock, port, data, from_addr):
        """Xcode-facing leg. Forward to the phone; never forward looped-back replies."""
        src_port = from_addr[1]
        if src_port in (port, port + self.remote_off):
            # A packet arriving on our own bound ports is a reply that looped back
            # through 127.0.0.1 - only possible when the "phone" lives on this host
            # (self-test). Xcode can never use our bound ports as its source port,
            # so this branch is unreachable in production. Route or drop, never forward.
            ip = self.remote_ip_holder()
            client = (self._udpsess.get((from_addr[0], from_addr[1]))
                      or (self._udpsess.get((ip, src_port)) if ip else None)
                      or (self._udpsess.get((ip, src_port + self.remote_off)) if ip else None))
            if client:
                try:
                    sock.sendto(data, client)
                except OSError:
                    pass
            return
        if not self.health_flag.get("up"):
            return  # phone offline: drop quietly (avoids log spam)
        ip = self.remote_ip_holder()
        if not ip:
            return
        pair = self._udp_pair.get(id(sock))
        sender = pair[1] if pair else sock  # send from the reply-facing socket so the
                                            # kernel picks a route-valid source address
        try:
            sender.sendto(data, (ip, port + self.remote_off))
        except OSError:
            self.health_flag["up"] = False
            return
        self._udpsess[(ip, port + self.remote_off)] = from_addr  # expect reply here
        if len(self._udpsess) > 8192:
            self._udpsess.clear()  # should not happen (bounded by tunnel range); safety

    def _route_udp_reply(self, sock, port, data, from_addr):
        """Reply-facing leg: strictly allow-listed by session map; drop everything else.

        Unsolicited traffic (e.g. from the LAN) matches no session and is dropped,
        so binding this socket on all interfaces exposes nothing to forwarders.
        """
        client = self._udpsess.get((from_addr[0], from_addr[1]))
        if not client:
            return
        pair = self._udp_pair.get(id(sock))
        client_sock = pair[0] if pair else sock
        try:
            client_sock.sendto(data, client)  # to Xcode it appears from 127.0.0.1:<port>
        except OSError:
            pass

    def _pipe_tcp(self, conn, port, remote_port):
        ip = self.remote_ip_holder()
        if not ip:
            conn.close()
            return
        try:
            remote = socket.create_connection((ip, remote_port), timeout=8)
        except OSError:
            conn.close()
            return
        self.health_flag["up"] = True
        log(f"tunnel session on local port {port} -> phone:{remote_port} (relay open)")
        sel = selectors.DefaultSelector()
        try:
            conn.setblocking(False)
            remote.setblocking(False)
            sel.register(conn, selectors.EVENT_READ | selectors.EVENT_WRITE)
            sel.register(remote, selectors.EVENT_READ | selectors.EVENT_WRITE)
            buffers = {conn: b"", remote: b""}
            while True:
                events = sel.select(timeout=self.idle_timeout)  # idle timeout; keep long chatty sessions
                if not events:
                    break
                for key, mask in events:
                    s = key.fileobj
                    if mask & selectors.EVENT_READ:  # the mask, not key.flags (not a public attr)
                        try:
                            data = s.recv(262144)
                        except (BlockingIOError, InterruptedError):
                            data = b""
                        except OSError:
                            data = None
                        if not data:
                            raise EOFError(s is conn)
                        other = remote if s is conn else conn
                        buffers[other] += data
                for s in (conn, remote):
                    if buffers[s]:
                        try:
                            n = s.send(buffers[s])
                            buffers[s] = buffers[s][n:]
                        except (BlockingIOError, InterruptedError):
                            pass
                        except OSError:
                            raise EOFError(s is conn)
        except (EOFError, OSError):
            pass
        finally:
            for s in (conn, remote):
                try:
                    sel.unregister(s)
                except Exception:
                    pass
                s.close()
            try:
                sel.close()
            except Exception:
                pass


# ---------------------------------------------------------------- mDNS

def norm_type(t):
    t = (t or "_remotepairing._tcp").strip().lower()
    if not t.endswith("."):
        t += "."
    if not t.endswith("local."):
        t += "local."
    return t


def make_service_info(dev, srv_port, ipv4=None, ipv6=None):
    """Build the ServiceInfo to publish for this device.

    The address set decides where macOS resolves the phone's bonjour name:
      ipv4 only  -> classic relay anchor (commercial-bridge parity): A = this Mac's advertise
                     address, SRV port = our local (possibly re-pointed) control relay.
      ipv4+ipv6  -> hybrid (default since v1.3): A = the Mac anchor for the
                     bonjour-SCOPED control channel (Apple's resolver only accepts
                     addresses it can route within that scope - a self-address), PLUS
                     AAAA = the phone's tailnet v6, so any LATER non-bonjour endpoint
                     CoreDevice builds for the same name can dial the phone directly.
      ipv6 only  -> direct6: AAAA = the phone's own tailnet v6, SRV port = its REAL
                     RemotePairing port; Xcode dials the iPhone straight over the mesh.
    (modern zeroconf puts 8/16-byte entries in `addresses` into A / AAAA records)"""
    try:
        from zeroconf import ServiceInfo
    except ImportError:
        die("missing dependency 'zeroconf' - activate the venv (see README)")

    stype = norm_type(dev.get("service_type", "_remotepairing._tcp"))
    uuid = dev["id"]
    host = (dev.get("bonjour_host") or "127.0.0.1.local").rstrip(".") + "."
    props = {str(k): str(v) for k, v in (dev.get("txt") or {}).items()}
    addrs = []
    if ipv4:
        addrs.append(socket.inet_aton(ipv4))
    if ipv6:
        addrs.append(socket.inet_pton(socket.AF_INET6, ipv6))
    if not addrs:
        addrs.append(socket.inet_aton(pick_advertise_ip(dev.get("advertise_ip", "127.0.0.1"))))
    kwargs = dict(
        type_=stype,  # zeroconf >= 0.139; older versions name it 'type'
        name=f"{uuid}.{stype}",
        addresses=addrs,
        port=int(srv_port),
        weight=0,
        priority=0,
        server=host,
    )
    for key in ("properties", "txt"):
        try:
            return ServiceInfo(**{key: props}, **kwargs)
        except TypeError:
            continue
    try:  # very old zeroconf keyword style
        return ServiceInfo(type=stype, **kwargs)
    except TypeError:
        die(f"could not construct ServiceInfo for {uuid} (zeroconf API mismatch)")


def dns_sd_proxy_register(dev, port, host, ip):
    """Publish the service via mDNSResponder itself - the commercial bridge's mechanism.

    Spawns `dns-sd -P <Name> <Type> local. <Port> <Host> <IP> [TXT...]` and keeps
    the child alive for as long as xbridge holds the bridge. `-P` (Register Proxy)
    registers BOTH an SRV record with target=<Host> and A(<Host>)=<IP>, inside the
    mDNSResponder daemon.

    Why this matters on macOS 27: Network.framework resolves the phone's .local name
    through mDNSResponder with an interface scope. The daemon keeps its OWN client
    registrations in that scoped cache indefinitely (a commercial bridge's -P registration was
    still resolving 4+ hours later), whereas FOREIGN multicast announcements (raw
    zeroconf publishes) decay out of the cache within minutes - after which every
    dial dies with 'DNS Error: NoSuchRecord' and the bridge goes silent. Registering
    through the daemon is therefore the only publish path that stays alive all day.

    The registration lives as long as this child process runs: when it exits,
    mDNSResponder removes the records (verified - no ghosting). The caller must
    terminate/kill the returned Popen on disarm/shutdown. Returns None if dns-sd
    is missing or bails out immediately.
    """
    binary = shutil.which("dns-sd") or ("/usr/bin/dns-sd"
                                       if os.path.exists("/usr/bin/dns-sd") else None)
    if binary is None:
        log("dns-sd not found - cannot register via mDNSResponder")
        return None
    stype = norm_type(dev.get("service_type", "_remotepairing._tcp")).rstrip(".")
    if stype.endswith(".local"):
        stype = stype[:-len(".local")]  # dns-sd appends its <Domain> argument itself
    host = (host or "127.0.0.1.local").rstrip(".")
    cmd = [binary, "-P", str(dev["id"]), stype, "local.", str(int(port)), host,
           str(ip)]
    for k, v in (dev.get("txt") or {}).items():
        cmd.append(f"{k}={v}")
    try:
        out = tempfile.NamedTemporaryFile(prefix="xbr-dnssd-", delete=False)
        proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL)
    except OSError as e:
        log(f"dns-sd -P launch failed ({e})")
        return None
    time.sleep(1.5)  # give it a moment to register (or bail out)
    if proc.poll() is not None:
        try:
            tail = open(out.name, errors="replace").read().strip()[-300:]
        except OSError:
            tail = ""
        os.unlink(out.name)
        log(f"WARNING - dns-sd -P exited early (rc={proc.returncode}); "
            f"record may not be active: {tail}")
        return None
    return proc, out.name


def dns_sd_kill(entry):
    """Stop a dns-sd -P child; mDNSResponder drops its registration on disconnect."""
    if entry is None:
        return
    proc = entry[0] if isinstance(entry, tuple) else entry
    logpath = entry[1] if isinstance(entry, tuple) else None
    try:
        proc.terminate()
        for _ in range(10):
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass
    if logpath:
        try:
            os.unlink(logpath)
        except OSError:
            pass


def browse_local(instance_uuid, service_type, seconds=5.0):
    """Return True if a REAL copy of this device is on our local network.

    A record that resolves only to THIS machine's own addresses (loopback / LAN /
    tailnet) is one of our own publications, not a phone on the Wi-Fi - ignored.
    A record with any foreign address is counted (covers merged duplicates too).
    """
    try:
        from zeroconf import Zeroconf, ServiceBrowser
    except ImportError:
        return False  # can't tell; let the user decide

    state = {"real": False}
    zc = Zeroconf()
    local_addrs = set(_own_ipv4_addrs())
    try:
        def on_change(zeroconf=None, service_type="", name="", state_change=None):
            if getattr(state_change, "name", None) not in ("Added", "Updated"):
                return
            if instance_uuid.lower() not in name.lower():
                return
            base = norm_type(".".join(name.split(".")[1:]))
            try:
                info = zc.get_service_info(base, name, timeout=2500)
            except Exception:
                info = None
            if not info or not getattr(info, "addresses", None):
                return  # resolved on a later event; keep waiting
            for addr in info.addresses:
                if isinstance(addr, bytes) and len(addr) == 4 and addr not in local_addrs:
                    state["real"] = True
                    break

        browser = ServiceBrowser(zc, [norm_type(service_type)], handlers=[on_change])
        deadline = time.time() + seconds
        while time.time() < deadline:
            if state["real"]:
                break
            time.sleep(0.3)
        browser.cancel()
    finally:
        zc.close()
    return state["real"]


def browse_phones(seconds):
    """Browse _remotepairing._tcp on the local network for `seconds` seconds.

    Returns a list of capture-style rec dicts (one per pairing identifier seen),
    or None when zeroconf is unavailable. A record that resolves only to THIS
    machine's addresses is a local bridge re-publication (ours or another
    vendor's, e.g. a third-party bridge): flagged `_bridge_copy`, remote port forced to 49152
    (a bridge's SRV port is its LOCAL relay port, not the phone's RSD daemon).

    NOTE: deliberately duplicates cmd_capture's on_change (kept in sync) so the
    proven capture code path is untouched.
    """
    try:
        from zeroconf import Zeroconf, ServiceBrowser
    except ImportError:
        return None
    seen = {}
    try:
        zc = Zeroconf()
    except Exception as e:
        log(f"browsing could not start ({e})")
        return []

    def on_change(zeroconf=None, service_type="", name="", state_change=None):
        if getattr(state_change, "name", None) not in ("Added", "Updated"):
            return
        base = norm_type(".".join(name.split(".")[1:]))  # e.g. _remotepairing._tcp.local.
        try:
            info = zc.get_service_info(base, name, timeout=3000)
        except Exception:
            info = None
        if not info:
            return
        txt = {}
        props = getattr(info, "properties", None)
        if isinstance(props, dict) and props:
            for k, v in props.items():
                if isinstance(k, bytes):
                    k = k.decode("utf-8", "replace")
                if isinstance(v, bytes):
                    v = v.decode("utf-8", "replace")
                txt[str(k)] = str(v) if v is not None else ""
        elif hasattr(info, "txt"):
            for pair in info.txt or []:  # older zeroconf exposes raw k=v pairs
                if b"=" in pair:
                    k, v = pair.split(b"=", 1)
                    try:
                        txt[k.decode()] = v.decode()
                    except UnicodeDecodeError:
                        pass
        ident = txt.get("identifier") or name.split(".")[0]
        host = (info.server or "").rstrip(".")
        rec_ = {
            "id": ident,
            "label": host or name.split(".")[0],
            # store without trailing dot/domain, e.g. _remotepairing._tcp
            "service_type": base.rstrip("local.").rstrip("."),
            "bonjour_host": host,
            "remote_port": info.port or 49152,
            "txt": txt,
        }
        addrs4 = [a for a in (getattr(info, "addresses", None) or [])
                  if isinstance(a, bytes) and len(a) == 4]
        foreign = [a for a in addrs4 if a not in set(_own_ipv4_addrs())]
        if addrs4 and not foreign:
            rec_["remote_port"] = 49152
            rec_["_bridge_copy"] = True
        seen[ident] = rec_

    browser = ServiceBrowser(zc, [norm_type("_remotepairing._tcp")], handlers=[on_change])
    log(f"browsing for iPhones on your local network for {seconds:g}s ...")
    deadline = time.time() + seconds
    grace = None
    try:
        while time.time() < deadline:
            if seen:
                if grace is None:
                    grace = time.time()
                elif time.time() - grace > 3:
                    break  # got results, let them settle for 3s, done
            elif time.time() > deadline - seconds / 3:
                break  # nothing found and most of the time is gone
            time.sleep(0.4)
    finally:
        browser.cancel()
        zc.close()
    return list(seen.values())


# ---------------------------------------------------------------- tunnel watcher

class TunnelWatcher(threading.Thread):
    """Learn live RSD/tunnel ports from the macOS system log; relay them on demand.

    After pairing, RemotePairing tells clients to continue on a (often dynamic)
    "tunnel" endpoint. The commercial bridge's TunnelPortWatcher watched remotepairingd for the
    old 'Got tunnel endpoint' message and opened relays for it; macOS 27 reworded
    those messages, so we watch both daemons for anything tunnel-shaped and extract
    every host:port token. Purely opportunistic - a port we miss only costs the
    retry Xcode makes seconds later."""

    PREDICATE = ('process == "remotepairingd" OR '
                 'process CONTAINS "CoreDevice"')

    def __init__(self, label, on_endpoint):
        super().__init__(daemon=True)
        self.label = label
        self.on_endpoint = on_endpoint      # (host, port) -> None
        self._stop_evt = threading.Event()
        self._proc = None

    def stop(self):
        self._stop_evt.set()
        p = self._proc
        if p is not None and p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass

    def run(self):
        while not self._stop_evt.is_set():
            try:
                self._proc = subprocess.Popen(
                    ["/usr/bin/log", "stream", "--style", "compact",
                     "--predicate", self.PREDICATE],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, bufsize=1)
            except (OSError, ValueError):
                log(f"{self.label}: tunnel watcher: /usr/bin/log unavailable - disabled")
                return
            for line in self._proc.stdout:
                if self._stop_evt.is_set():
                    break
                try:
                    for host, port in self.parse(line):
                        self.on_endpoint(host, port)
                except Exception:
                    pass
            try:
                self._proc.kill()
            except OSError:
                pass
            if not self._stop_evt.is_set():  # log(1) exited on its own - restart
                time.sleep(3)

    @staticmethod
    def parse(line):
        """Extract (host, port) endpoint tokens from a compact log line."""
        out = []
        for m in re.finditer(r"'([^']+)'", line):          # quoted 'host:port'
            tok = m.group(1)
            host, _, p = tok.rpartition(":")
            if host and p.isdigit() and 0 < int(p) < 65536:
                out.append((host, p))
        for m in re.finditer(r"(?:local|remote)\s+([0-9A-Fa-f.:]{2,51}):\(?([^)\s]*)\)?", line):
            port = m.group(2)
            if ":" in port:  # '[v6]:port' form
                port = port.rsplit(":", 1)[1]
            if port.isdigit() and 0 < int(port) < 65536:
                out.append((m.group(1), port))
        return out


# ---------------------------------------------------------------- bridge

def recapture_once(dev, seconds=8.0):
    """Browse for the newest _remotepairing record from this device's bonjour host.

    Returns a capture-style dict for the LATEST identifier seen, or None when nothing
    matched (e.g. the phone is not on a shared network). Records that resolve only to
    this machine's addresses (bridge copies, e.g. another vendor's publication) contribute
    their identity TXT but get remote_port forced to 49152. Used by --auto's periodic
    re-capture so pairing-identifier churn never leaves us with a stale profile.
    """
    want_host = (dev.get("bonjour_host") or "").lower().rstrip(".")
    try:
        from zeroconf import Zeroconf, ServiceBrowser
    except ImportError:
        return None
    latest = {}
    zc = Zeroconf()
    try:
        def on_change(zeroconf=None, service_type="", name="", state_change=None):
            if getattr(state_change, "name", None) not in ("Added", "Updated"):
                return
            try:
                info = zc.get_service_info(
                    norm_type(".".join(name.split(".")[1:])), name, timeout=2500)
            except Exception:
                return
            if not info or (want_host and
                            (info.server or "").lower().rstrip(".") != want_host):
                return
            props = getattr(info, "properties", None) or {}
            txt = {str(k): ("" if v is None else str(v)) for k, v in props.items()}
            host = (info.server or "").rstrip(".")
            addrs4 = [a for a in (getattr(info, "addresses", None) or [])
                      if isinstance(a, bytes) and len(a) == 4]
            port = info.port or 49152
            if addrs4 and all(a in set(_own_ipv4_addrs()) for a in addrs4):
                port = 49152  # bridge copy; the phone's RPAI daemon listens on 49152
            ident = txt.get("identifier") or name.split(".")[0]
            for k in [k for k in latest
                      if (latest[k].get("bonjour_host") or "").lower().rstrip(".") == want_host]:
                latest.pop(k)
            latest[ident] = {
                "id": ident, "label": host or name.split(".")[0],
                "service_type": norm_type(name).rstrip("local.").rstrip("."),
                "bonjour_host": host, "remote_port": port, "txt": txt,
            }
        browser = ServiceBrowser(zc, [norm_type("_remotepairing._tcp")], handlers=[on_change])
        time.sleep(seconds)
        browser.cancel()
    finally:
        zc.close()
    if not latest:
        return None
    return list(latest.values())[-1]  # last event for the host wins (same-host replaced)


def plan_ports(dev):
    start = int(dev.get("tunnel_start", DEFAULT_TUNNEL_START))
    end = int(dev.get("tunnel_end", DEFAULT_TUNNEL_END))
    return start, min(end, 65535)


def cmd_bridge(args):
    raise_fd_limit()
    cfg = load_config(args.config)

    if args.all:
        devs = list(cfg["devices"])
    elif args.uuid:
        devs = [d for d in cfg["devices"] if d.get("id", "").upper().startswith(args.uuid.upper())]
        if not devs:
            die(f"no device matching {args.uuid!r} in config (see 'status')")
    else:
        devs = cfg["devices"][:1]
    if not devs:
        die("no devices configured - run 'capture' first")

    bind_host = args.bind_host or cfg.get("bind_host", "127.0.0.1")

    # merge tunnel ranges across the selected devices
    t_start, t_end = None, None
    for d in devs:
        s, e = plan_ports(d)
        t_start, t_end = (s if t_start is None else min(t_start, s),
                          e if t_end is None else max(t_end, e))
    if args.tunnel_start:
        t_start = args.tunnel_start
    if args.tunnel_end:
        t_end = args.tunnel_end

    health_flags = {d["id"]: {"up": False} for d in devs}

    try:
        from zeroconf import Zeroconf
        zc = Zeroconf() if not args.no_publish else None
    except ImportError:
        zc = None
        log("zeroconf not installed - mDNS publish disabled (relay only)")

    if t_end is not None and (args.remote_port_offset or 0):
        if max(t_end, DEFAULT_TUNNEL_END) + args.remote_port_offset > 65535:
            die(f"tunnel end {t_end} + remote offset exceeds port 65535")
    self_ip = None
    try:
        self_ip = self_tailnet_ip()
    except Exception:
        pass

    ctxs = []  # per-device state; every device starts fully idle (no sockets, no mDNS)

    for d in devs:
        d2 = dict(d)
        d2["advertise_ip"] = args.advertise_ip or cfg.get("advertise_ip", "127.0.0.1")
        label = d2.get("label", d2["id"])

        def holder(dev=d2, fl=health_flags[d2["id"]]):
            ip, online = resolve_remote_ip(dev)
            # Only write liveness when the source actually knows (tailscale's Online
            # flag). A manual-ip profile has no liveness source - keep the last port-ping
            # result (this closure runs on every relayed packet, so a None must not
            # clobber the probe's finding or UDP traffic gets dropped packet-by-packet).
            if online is not None:
                fl["up"] = bool(online)
            return ip

        def _bind_ports(c):
            """Bind the control channel + tunnel range onto c[\"pump\"]; returns the local
            control port (None when none could be bound)."""
            pump = c["pump"]

            if (c.get("mode") or "auto") == "direct6":
                # direct6: Xcode dials the phone's own address - nothing listens here
                return None

            # 1) RemotePairing control channel: mirror it; if busy on this Mac (macOS's
            #    own remotepairingd holds 49152), scan upward - the published record
            #    advertises whichever local port we end up with.
            cp = int(c["d2"].get("remote_port", 49152))
            control_local = None
            s = bind_tcp(bind_host, cp)
            if s:
                control_local = cp
                pump.add(s, "tcp", cp)  # remote stays cp (mirror)
            else:
                for p2 in range(cp, min(cp + 64, 65535) + 1):
                    if port_free(bind_host, p2):
                        s = bind_tcp(bind_host, p2)
                        if s:
                            control_local = p2
                            pump.add(s, "tcp", p2, cp)  # local re-pointed; remote stays cp
                        break
            if control_local:
                log(f"{c['label']}: RemotePairing control channel on local port {control_local}"
                    + (f" (remote {cp}, re-pointed)" if control_local != cp else ""))
            else:
                log(f"{c['label']}: WARNING - no local port available for the control channel")

            # 2) Tunnel range: mirrored exactly (Xcode dials the negotiated port number).
            dev_s, dev_e = plan_ports(c["d2"])
            lo, hi = max(dev_s, t_start), min(dev_e, t_end)
            pool = c.setdefault("pool_ports", set())  # tunnel ports THIS pump owns
            n_tcp, holes = 0, []
            for p in range(lo, hi + 1):
                st = bind_tcp(bind_host, p)
                if st:
                    pump.add(st, "tcp", p, p + pump.remote_off)
                    pool.add(p)
                    n_tcp += 1
                else:
                    holes.append(p)
            if holes:
                sample = ", ".join(str(h) for h in holes[:5]) + (", ..." if len(holes) > 5 else "")
                log(f"{c['label']}: {len(holes)} tunnel port(s) busy locally ({sample}) - those exact "
                    f"ports may be holes")
            n_udp = 0
            udp_holes = []
            for p in range(lo, hi + 1):
                sc = bind_udp(bind_host, p, reuse=False)   # Xcode-facing; no REUSEADDR
                if sc is None:
                    udp_holes.append(p)
                    continue
                pump.add(sc, "udp-client", p)
                sr = None
                for cand in ("0.0.0.0", self_ip):  # all-ifaces first, then tailnet-only
                    if cand:
                        sr = bind_udp(cand, p, reuse=True)  # reply-facing socket
                        if sr:
                            break
                if sr is not None:
                    pump.add(sr, "udp-remote", p)
                    pump._udp_pair[id(sc)] = (sc, sr)
                    pump._udp_pair[id(sr)] = (sc, sr)
                else:
                    log(f"{c['label']}: WARNING - no reply socket on UDP port {p}; that tunnel "
                        f"port will be one-way (phone -> Mac UDP dropped)")
                n_udp += 1
            if udp_holes:
                sample = ", ".join(str(h) for h in udp_holes[:5]) + (", ..." if len(udp_holes) > 5 else "")
                log(f"{c['label']}: {len(udp_holes)} tunnel UDP port(s) busy locally ({sample})")
            log(f"{c['label']}: tunnel {lo}-{hi} armed: {n_tcp} TCP + {n_udp} UDP relays")

            # 3) Link-probe mirror: macOS's CUNetLinkManager (inside remotepairingd)
            #    liveness-monitors <advertised-IP>:3722 on every paired device and tears
            #    the control channel down ~42s after it stops answering. Mirror that UDP
            #    port to the phone so its daemon can answer (coexists with the system
            #    daemons' own :3722 sockets via SO_REUSEPORT). 0 disables.
            #    NOTE: on macOS 27 the liveness mechanism is ARP-based, so this mirror
            #    cannot satisfy it for a self-address bridge - relay mode still re-pairs
            #    periodically there; the mirror covers other OS versions/behaviors.
            lp = int(getattr(args, "link_port", None) or DEFAULT_LINK_PORT)
            if lp and 0 < lp < 65536:
                sl = bind_udp(bind_host, lp, reuse=True, reuseport=True)
                if sl is not None:
                    pump.add(sl, "udp-client", lp)
                    log(f"{c['label']}: link-probe mirror on UDP {lp} "
                        f"(CUNetLinkManager liveness) -> phone:{lp}")
                else:
                    log(f"{c['label']}: note - link port {lp} unavailable locally; "
                        f"liveness flapping may continue")
            return control_local

        def _arm(c):
            """Fresh Pump + all ports + mDNS record. True when the control channel came up.
            A new Pump object is created each time - a closed selector can't be re-used."""
            d2 = c["d2"]
            # 'auto' (default) := prefer direct6 when the phone has a tailnet IPv6
            # (macOS 27's CUNetLinkManager liveness is ARP-based, which a self-address
            # bridge can never satisfy - relay mode flaps forever; direct6 points Xcode
            # at the real phone so liveness works natively over the mesh). Fall back to
            # relay when no v6; force with --advertise-mode for either behavior.
            # v1.4 semantics (from live A/B tests vs the commercial bridge on macOS 27):
            #   auto/relay - register through mDNSResponder itself (dns-sd -P child,
            #     the commercial bridge's mechanism): SRV target = the phone's .local name,
            #     A(name) = our advertise IP. The daemon keeps its OWN registrations
            #     in the bonjour-scoped resolver cache indefinitely, so connections
            #     keep resolving and dialing all day (a raw zeroconf multicast copy of
            #     the same name decays out of that cache within ~minutes - v1.3 died).
            #   hybrid     - experimental: dual A + AAAA (phone's tailnet v6) via
            #     zeroconf. Gave ~8 working minutes in testing before the foreign-v6
            #     copy poisoned/decayed out of the scoped cache.
            #   direct6    - experimental: AAAA-only via zeroconf pointing at the
            #     phone's tailnet v6; Xcode would dial the mesh directly, but macOS 27
            #     rejects pure-remote-v6 answers in a bonjour-scoped resolution.
            mode = c.get("mode_spec") or "auto"
            v6 = ""
            try:
                v6 = tailscale_node_v6(d2) or ""
            except Exception:
                v6 = ""
            direct6 = (mode == "direct6") and bool(v6)
            c["v6"] = v6
            if direct6:
                c["mode"] = "direct6"
            elif mode == "hybrid":
                c["mode"] = "hybrid"
            else:
                c["mode"] = "relay"
            if mode == "direct6" and not v6:
                log(f"{c['label']}: WARNING - no tailnet IPv6 for this phone; falling back "
                    f"to relay mode (enable IPv6 on your tailnet for direct6)")
                c["mode"] = "relay"
            if mode == "hybrid":
                log(f"{c['label']}: EXPERIMENTAL hybrid record - dual A + AAAA (v6 {v6}) "
                    f"via raw mDNS; may decay out of macOS 27's resolver cache")
            if mode in ("auto", "relay") and v6:
                log(f"{c['label']}: phone tailnet v6 {v6} present (used only by "
                    f"--advertise-mode hybrid/direct6; relay anchor stays A-only)")
            pump = Pump(d2, c["holder"], health_flags[d2["id"]],
                        idle_timeout=getattr(args, "idle_timeout", 300.0))
            c["pump"] = pump
            pump.remote_off = max(0, int(args.remote_port_offset or 0))
            c["dyn_ports"] = set()
            c["dyn_tried"] = set()
            c["pool_ports"] = set()  # rebuilt by _bind_ports for this fresh pump
            try:
                c["adv"] = pick_advertise_ip(d2.get("advertise_ip", "127.0.0.1"))
            except Exception:
                c["adv"] = "127.0.0.1"
            cp = None if direct6 else _bind_ports(c)
            c["control_local"] = cp
            if not direct6 and not cp:
                pump.close_all()  # hold no ports at all until the next attempt
                c["pump"] = None
                return False
            threading.Thread(target=pump.run, daemon=True).start()

            # 3) Publish the Bonjour record (advertised port = chosen control port).
            if direct6:
                srv = int(d2.get("remote_port", 49152))  # the phone's REAL port
            else:
                srv = cp or int(d2.get("remote_port", 49152))
            published = False

            # commercial-bridge-parity path (auto/relay): register INSIDE mDNSResponder via a
            # kept-alive dns-sd -P child. Persist all day; the daemon removes the
            # records when we stop it (verified - no ghosts).
            if c["mode"] == "relay" and not getattr(args, "no_publish", False):
                host = (d2.get("bonjour_host") or "127.0.0.1.local").rstrip(".")
                adv = c.get("adv") or "127.0.0.1"
                entry = dns_sd_proxy_register(d2, srv, host, adv)
                if entry is not None:
                    c["dnssd"] = entry
                    stype = norm_type(d2.get("service_type", "_remotepairing._tcp"))
                    log(f"{c['label']}: published via mDNSResponder "
                        f"{d2['id']}.{stype} port={srv} "
                        f"target={host} A={adv}")
                    published = True
                else:
                    log(f"{c['label']}: WARNING - mDNSResponder registration failed; "
                        f"falling back to raw multicast publish")

            # Raw mDNS path (zeroconf): direct6 = AAAA-only; hybrid = A+AAAA;
            # relay-fallback = A-only anchor.
            if not published and zc is not None:
                if c["mode"] == "direct6":
                    ip4, ip6 = None, c.get("v6") or None
                elif c["mode"] == "hybrid":
                    ip4, ip6 = c.get("adv"), (c.get("v6") or None)
                else:  # relay fallback (dns-sd unavailable)
                    ip4, ip6 = c.get("adv"), None
                try:
                    info = make_service_info(d2, srv, ipv4=ip4, ipv6=ip6)
                except Exception as e:
                    log(f"{c['label']}: WARNING - could not build mDNS record ({e})")
                    info = None
                if info is not None:
                    try:
                        zc.register_service(info)
                        c["info"] = info
                        adv = (c.get("v6") if c["mode"] == "direct6"
                               else c.get("adv", "127.0.0.1"))
                        log(f"{c['label']}: published Bonjour {info.name} port={srv} -> {adv} "
                            f"({c['mode']} via raw mDNS)"
                            + (" - may decay on macOS 27" if c["mode"] in ("hybrid",) else ""))
                        published = True
                    except Exception as e:
                        log(f"{c['label']}: WARNING - could not publish mDNS record ({e}); "
                            f"Xcode will not auto-discover the phone")
            if c["mode"] == "relay" and not published:
                log(f"{c['label']}: note - no mDNS record published (--no-publish); "
                    f"the relay still works for manual dials")

            # Self-check (commercial bridges do the same): dial our own published endpoint to
            # prove the publish -> listener -> relay path before Xcode tries it.
            if not direct6 and cp:
                adv = c.get("adv", "127.0.0.1")
                try:
                    s = socket.create_connection((adv, cp), timeout=5)
                    s.close()
                    log(f"{c['label']}: self-check OK - control relay verified on {adv}:{cp}")
                except OSError as e:
                    log(f"{c['label']}: WARNING - self-check failed ({e}); is {adv} reachable?")

            # Tunnel watcher: opens on-demand relays for whatever port the pairing
            # protocol negotiates next (outside any fixed window), learned from logs.
            if not getattr(args, "no_watch", False):
                c["watcher"] = TunnelWatcher(c["label"],
                                             lambda h, p: _ensure_endpoint(c, h, p))
                c["watcher"].start()

            c["holder"]()  # prime reachability state
            return True

        def _disarm(c):
            """mDNS goodbye + close every socket (+ watcher); the device goes fully idle."""
            if c.get("watcher") is not None:
                try:
                    c["watcher"].stop()
                except Exception:
                    pass
                c["watcher"] = None
            if c.get("dyn_ports"):
                c["dyn_ports"].clear()
            if c.get("dyn_tried"):
                c["dyn_tried"].clear()
            if c.get("pool_ports"):
                c["pool_ports"].clear()
            if c["info"] is not None:
                try:
                    zc.unregister_service(c["info"])  # sends the mDNS goodbye
                except Exception:
                    pass
                c["info"] = None
            if c.get("dnssd") is not None:
                dns_sd_kill(c["dnssd"])  # mDNSResponder drops the records on disconnect
                c["dnssd"] = None
            if c["pump"] is not None:
                c["pump"].close_all()  # also ends the selector thread
                c["pump"] = None
            c["control_local"] = None
            log(f"{c['label']}: bridge released - holding no ports; native Xcode "
                f"wireless debugging applies")

        def _probe_reachable(c):
            """Resolve the phone + port-ping it; update its health flag. -> (ip, up)"""
            d2 = c["d2"]
            flags = health_flags[d2["id"]]
            port = int(d2.get("remote_port", 49152))
            ip, online = resolve_remote_ip(d2)
            up = bool(online) if online is not None else False
            if ip:
                try:
                    s = socket.create_connection((ip, port), timeout=12)
                    s.close()
                    up = True
                except OSError:
                    up = False
            if up != flags.get("up"):
                log(f"{c['label']}: "
                    f"{'REACHABLE' if up else 'UNREACHABLE'} at {ip or '?'}:{port}"
                    + ("" if up else " - is Tailscale connected on the iPhone?"))
            flags["up"] = up
            return ip, up

        def _ensure_endpoint(c, host, port):
            """TunnelWatcher callback: ensure a local relay exists for this endpoint.

            Hosts we published (our advertise IP, loopback, or the phone's .local name
            re-announced by us) must have a listener: Xcode resolves them to us. Hosts
            we did NOT publish (e.g. the phone's own tailnet address in direct6) are
            dialled by Xcode directly over the mesh - nothing to relay locally."""
            try:
                port = int(port)
            except (TypeError, ValueError):
                return
            if not 0 < port < 65536:
                return
            pump = c.get("pump")
            if pump is None:
                return
            h = str(host).lower().rstrip(".")
            us = {"127.0.0.1", "localhost"}
            if c.get("adv"):
                us.add(c["adv"])
            us.add((c["d2"].get("bonjour_host") or "").lower().rstrip("."))
            if h in us:
                relay = True
            elif (c.get("mode") or "auto") == "direct6":
                return  # Xcode dials the phone directly; nothing local to open
            else:
                relay = True  # unexpected host - open anyway; harmless if unused
            if not relay:
                return
            tried = c.setdefault("dyn_tried", set())
            done = c.get("dyn_ports") or set()
            pool = c.setdefault("pool_ports", set())
            if port in done:
                return
            if port in pool:  # this pump already listens on it - covered (membership,
                done.add(port)   #  not range: two devices with overlapping windows must
                return           #  never assume the other pump's ports are ours)
            lo, hi = plan_ports(c["d2"])
            if port in tried:
                return  # attempted before; avoid log spam on repeated watcher lines
            n = 0
            st = bind_tcp(bind_host, port)
            if st is not None:
                pump.add(st, "tcp", port)
                pool.add(port)
                n += 1
            sc = bind_udp(bind_host, port, reuse=False)
            if sc is not None:
                pump.add(sc, "udp-client", port)
                n += 1
            tried.add(port)
            if n:
                done.add(port)
                log(f"{c['label']}: live tunnel relay on local port {port} "
                    f"(endpoint '{host}:{port}' from system log)")
            else:
                log(f"{c['label']}: WARNING - live tunnel port {port} busy locally; "
                    f"that endpoint may fail")

        # v1.3: 'auto' := relay anchor + dual A/AAAA when the phone has tailnet v6.
        # The CLI-specified mode ("auto" when unset) is the stable spec; c["mode"]
        # is its per-arm derivation (e.g. direct6 -> relay when v6 is absent) and
        # must not feed back into the derivation, or the first arm's choice would be
        # frozen for every later --auto re-arm.
        mode = args.advertise_mode if args.advertise_mode in ("relay", "direct6") else None
        ctxs.append({
            "d2": d2, "label": label, "mode": mode, "mode_spec": args.advertise_mode or "auto",
            "holder": holder,
            "bind_ports": _bind_ports, "arm": _arm, "disarm": _disarm,
            "probe": _probe_reachable,
            # live state (mutated only by this bridge's own threads):
            "pump": None, "info": None, "dnssd": None,
            "pool_ports": set(),
            "control_local": None, "bucket": None,
            "fails": 0,  # consecutive unreachable probes (arm/release hysteresis)
        })

    import signal
    def _on_term(*_a):
        raise KeyboardInterrupt  # lets the finally-block clean up (mDNS goodbye etc.)
    signal.signal(signal.SIGTERM, _on_term)

    if args.auto:
        _last_state = [None, None]  # (overall state, per-device view)



        def auto_watch():
            while True:
                for c in ctxs:
                    d2 = c["d2"]
                    local = False if args.allow_local else browse_local(
                        d2["id"], d2.get("service_type", "_remotepairing._tcp"), seconds=4)
                    ip, up = (None, None) if local else c["probe"](c)

                    if local:
                        bucket = "local"
                        msg = (f"{c['label']}: iPhone is on your local network - native "
                               f"Xcode wireless debugging applies; bridge stays out of the way")
                    elif up:
                        bucket = "remote-up"
                        msg = (f"{c['label']}: iPhone left your local network "
                               f"(reachable at {ip} over Tailscale)")
                    else:
                        bucket = "down"
                        msg = (f"{c['label']}: iPhone is off your local network AND unreachable "
                               f"at {ip or '?'} - is Tailscale connected?")
                    if bucket != c["bucket"]:
                        log(msg)
                        c["bucket"] = bucket

                    armed = c["pump"] is not None
                    if local:
                        # Back on the LAN: native path takes over, release everything.
                        c["fails"] = 0
                        if armed:
                            c["disarm"](c)
                    elif up:
                        c["fails"] = 0
                        if not armed:
                            if c["arm"](c):
                                log(f"{c['label']}: bridge ACTIVE - relaying over Tailscale; "
                                    f"Xcode should see the iPhone")
                            else:
                                log(f"{c['label']}: could not arm (port conflict?) - "
                                    f"will retry next tick")
                    else:
                        # Remote but unreachable: hold the published record through brief
                        # Tailscale dips (iOS background suspension); only release after N
                        # consecutive failures so Xcode sees a STABLE record.
                        c["fails"] = c.get("fails", 0) + 1
                        if armed and c["fails"] >= args.disarm_fails:
                            log(f"{c['label']}: {c['fails']} unreachable probes in a row - "
                                f"releasing (is Tailscale connected on the iPhone?)")
                            c["disarm"](c)

                st, dv = aggregate_state(ctxs)
                # Write on ANY change (overall state or a single device's bucket),
                # not only armed<->idle flips: otherwise per-device live status
                # (the 'devices' merge + the menu-bar app) goes stale.
                if _last_state[0] != st or _last_state[1] != dv:
                    write_state(st, dv)
                _last_state[0], _last_state[1] = st, dv
                time.sleep(args.toggle_interval)

        write_state("starting", aggregate_state(ctxs)[1])
        threading.Thread(target=auto_watch, daemon=True).start()
        log(f"bridge auto-watch: {len(ctxs)} device(s), checking every "
            f"{args.toggle_interval:g}s; arms while the iPhone is off your local network, "
            f"releases everything (zero ports held) when it returns. Ctrl-C to stop.")

        recap = getattr(args, "recapture_interval", None) or 300

        def recapture_loop():
            """Adopt the phone's pairing identifier as it churns.

            iOS re-issues the phone's RemotePairing pairing id (observed roughly every
            few minutes on a freshly-paired iOS 27 device). While we share a network
            with the phone, periodically re-scan and adopt whatever identity it is
            publishing now, so we never arm with a stale one when it goes remote.
            """
            while True:
                time.sleep(recap)
                for c in ctxs:
                    d2 = c["d2"]
                    try:
                        new = recapture_once(d2, seconds=8)
                    except Exception as e:
                        log(f"{c['label']}: re-capture error ({e})")
                        continue
                    old = str(d2.get("id", ""))
                    if not new or str(new["id"]).lower() == old.lower():
                        continue
                    log(f"{c['label']}: pairing identifier churned {old} -> {new['id']} "
                        f"- syncing profile" + (", re-publishing..." if c["pump"] else ""))
                    if old and old in health_flags:
                        health_flags[new["id"]] = health_flags.pop(old)
                    d2.update({
                        "id": new["id"],
                        "txt": new.get("txt") or d2.get("txt"),
                        "remote_port": new.get("remote_port", 49152),
                        "service_type": new.get("service_type") or d2.get("service_type"),
                        "bonjour_host": new.get("bonjour_host") or d2.get("bonjour_host"),
                    })
                    try:  # persist so the next start (reboot) has the fresh identity
                        cfg_now = load_config(args.config)
                        for x in cfg_now["devices"]:
                            if (str(x.get("id", "")).lower() == old.lower()) or \
                               (x.get("bonjour_host") and d2.get("bonjour_host") and
                                x["bonjour_host"].lower() == d2["bonjour_host"].lower()):
                                x.update({"id": new["id"],
                                         "txt": new.get("txt") or x.get("txt"),
                                         "remote_port": new.get("remote_port", 49152)})
                        save_config(cfg_now, args.config)
                    except Exception as e:
                        log(f"{c['label']}: WARNING - could not persist churned profile ({e})")
                    if c["pump"] is not None:  # re-publish under the new identity
                        try:
                            c["disarm"](c)
                        except Exception:
                            pass
                        try:
                            c["arm"](c)
                        except Exception as e:
                            log(f"{c['label']}: WARNING - re-publish after churn failed ({e})")

        if recap:
            threading.Thread(target=recapture_loop, daemon=True).start()
            log(f"auto re-capture: refreshing the pairing identity every {recap:g}s "
                f"while the iPhone shares a network with this Mac")
    else:
        for c in ctxs:
            d2 = c["d2"]

            # 1) Guard: phone on our LAN? refuse (same-network rule).
            if not args.allow_local:
                log(f"{c['label']}: checking whether the iPhone is on your local network ...")
                if browse_local(d2["id"], d2.get("service_type", "_remotepairing._tcp"), seconds=4):
                    die(f"{c['label']} is on the local network - use Xcode's built-in wireless "
                        f"debugging (pass --allow-local to override)")

            # 2) Arm: bind + publish.
            if not c["arm"](c):
                log(f"{c['label']}: WARNING - bridge incomplete (no control channel); "
                    f"tunnel relays may still work")

            write_state("armed", aggregate_state(ctxs)[1])

        def health():
            while True:
                for c in ctxs:
                    if c["pump"] is not None:
                        c["probe"](c)
                time.sleep(args.health_interval)

        threading.Thread(target=health, daemon=True).start()
        n_armed = sum(1 for c in ctxs if c["pump"] is not None)
        log(f"bridge armed: {n_armed} device(s), tunnel range {t_start}-{t_end} on "
            f"{bind_host}. Xcode should discover the iPhone shortly. Ctrl-C to stop.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("shutting down ...")
    finally:
        for c in ctxs:
            if c["info"] is not None and zc is not None:
                try:
                    zc.unregister_service(c["info"])  # sends the mDNS goodbye
                except Exception:
                    pass
            if c.get("dnssd") is not None:
                try:
                    dns_sd_kill(c["dnssd"])
                except Exception:
                    pass
                c["dnssd"] = None
            if c.get("watcher") is not None:
                try:
                    c["watcher"].stop()
                except Exception:
                    pass
            if c["pump"] is not None:
                try:
                    c["pump"].close_all()
                except Exception:
                    pass
        if zc is not None:
            try:
                zc.close()
            except Exception:
                pass
        log("bye")


# ---------------------------------------------------------------- other cmds

def cmd_capture(args):
    try:
        from zeroconf import Zeroconf, ServiceBrowser
    except ImportError:
        die("missing dependency 'zeroconf' - activate the venv (see README)")

    cfg = load_config(args.config)
    seen = {}
    zc = Zeroconf()

    def on_change(zeroconf=None, service_type="", name="", state_change=None):
        if getattr(state_change, "name", None) not in ("Added", "Updated"):
            return
        base = norm_type(".".join(name.split(".")[1:]))  # e.g. _remotepairing._tcp.local.
        try:
            info = zc.get_service_info(base, name, timeout=3000)
        except Exception:
            info = None
        if not info:
            return
        txt = {}
        props = getattr(info, "properties", None)
        if isinstance(props, dict) and props:
            for k, v in props.items():
                if isinstance(k, bytes):
                    k = k.decode("utf-8", "replace")
                if isinstance(v, bytes):
                    v = v.decode("utf-8", "replace")
                txt[str(k)] = str(v) if v is not None else ""
        elif hasattr(info, "txt"):
            for pair in info.txt or []:  # older zeroconf exposes raw k=v pairs
                if b"=" in pair:
                    k, v = pair.split(b"=", 1)
                    try:
                        txt[k.decode()] = v.decode()
                    except UnicodeDecodeError:
                        pass
        ident = txt.get("identifier") or name.split(".")[0]
        host = (info.server or "").rstrip(".")
        rec_ = {
            "id": ident,
            "label": host or name.split(".")[0],
            # store without trailing dot/domain, e.g. _remotepairing._tcp
            "service_type": base.rstrip("local.").rstrip("."),
            "bonjour_host": host,
            "remote_port": info.port or 49152,
            "txt": txt,
        }
        # If the record resolves only to THIS machine's addresses, some local bridge
        # (ours or another vendor's) published it. Adopt its identity
        # TXT, but force the remote port: a bridge's SRV port is its LOCAL (possibly
        # re-pointed) relay port, not the phone's RemotePairing daemon.
        addrs4 = [a for a in (getattr(info, "addresses", None) or [])
                  if isinstance(a, bytes) and len(a) == 4]
        foreign = [a for a in addrs4 if a not in set(_own_ipv4_addrs())]
        if addrs4 and not foreign:
            rec_["remote_port"] = 49152
            rec_["_bridge_copy"] = True
        seen[ident] = rec_

    browser = ServiceBrowser(zc, [norm_type("_remotepairing._tcp")], handlers=[on_change])
    log(f"browsing for iPhones on your local network for {args.seconds}s ...")
    deadline = time.time() + args.seconds
    grace = None
    while time.time() < deadline:
        if seen:
            if grace is None:
                grace = time.time()
            elif time.time() - grace > 3:
                break  # got results, let them settle for 3s, done
        elif time.time() > deadline - args.seconds / 3:
            break  # nothing found and most of the time is gone
        time.sleep(0.4)
    browser.cancel()
    zc.close()

    if not seen:
        die("no iPhone found - the phone must be on your local Wi-Fi for capture")

    for rec in seen.values():
        data = tailscale_status_json()
        node, _online = find_tailnet_node(data, rec.get("bonjour_host", "").split(".")[0]) \
            if data else (None, None)
        rec["provider"] = "tailscale" if node else "manual-ip"
        rec["tailscale_node"] = rec.get("label", "").split(".")[0] if node else None
        rec["fallback_ip"] = node or args.remote_ip or None
        if rec.get("tailscale_node"):
            log(f"matched tailscale node '{rec['tailscale_node']}'")
        else:
            log("no obvious tailscale node - set 'tailscale_node' by hand in "
                f"{args.config}")
        # same phone re-issued its pairing identifier? replace the stale entry, but
        # keep human-managed fields (label/provider/tailscale_node/fallback_ip/range)
        same_host = [x for x in cfg["devices"]
                     if x.get("bonjour_host") and rec.get("bonjour_host")
                     and x["bonjour_host"] == rec["bonjour_host"]]
        if same_host:
            for x in same_host:
                rec["provider"] = x.get("provider") or rec.get("provider")
                if not rec.get("tailscale_node"):
                    rec["tailscale_node"] = x.get("tailscale_node")
                if not rec.get("fallback_ip"):
                    rec["fallback_ip"] = x.get("fallback_ip")
                if not rec.get("tunnel_start"):
                    rec["tunnel_start"] = x.get("tunnel_start")
                if not rec.get("tunnel_end"):
                    rec["tunnel_end"] = x.get("tunnel_end")
                if not rec.get("label"):
                    rec["label"] = x.get("label")
                log(f"note: same phone now answers with new pairing id "
                    f"{rec['id']} (was {x['id']}) - profile updated")
                cfg["devices"] = [y for y in cfg["devices"] if y is not x]
        if rec.pop("_bridge_copy", None):
            log(f"note: {rec['label']}'s record resolves to this Mac - a local bridge copy; "
                f"kept its identity, remote port forced to 49152")
        cfg["devices"] = [x for x in cfg["devices"] if x.get("id") != rec["id"]]
        cfg["devices"].append(rec)
        log(f"captured: {rec['label']} id={rec['id']} port={rec['remote_port']} "
            f"txt({len(rec['txt'])} keys)")

    save_config(cfg, args.config)
    log(f"saved {len(seen)} device(s) to {args.config}")


def cmd_scan(args):
    cfg = load_config(args.config)
    if args.uuid:
        devs = [d for d in cfg["devices"] if d.get("id", "").upper().startswith(args.uuid.upper())] \
            or cfg["devices"][:1]
    else:
        devs = list(cfg["devices"])
    if not devs:
        die("no devices configured")
    rc = 0
    for d in devs:
        ip, online = resolve_remote_ip(d)
        port = int(d.get("remote_port", 49152))
        ok = False
        if ip:
            try:
                s = socket.create_connection((ip, port), timeout=5)
                s.close()
                ok = True
            except OSError as e:
                log(f"probe {ip}:{port} failed ({e})")
        status = ("REACHABLE (ok)" if ok else
                  "UNREACHABLE (tailscale node offline)" if online is False else
                  "UNREACHABLE")
        print(f"{d.get('label', d['id']):34s} {str(ip or '?'):18s} :{port}  {status}")
        rc |= 0 if ok else 1
    return rc


def cmd_status(args):
    cfg = load_config(args.config)
    print(f"config: {args.config}")
    if not cfg["devices"]:
        print("  (no devices - run 'capture')")
    for d in cfg["devices"]:
        ip, online = resolve_remote_ip(d)
        print(f"  {d.get('label', d['id'])}")
        print(f"      id={d.get('id')}\n"
              f"      tailscale_node={d.get('tailscale_node')}  ip={ip or d.get('fallback_ip') or '?'}"
              f"  online={'yes' if online else 'no/unknown'}\n"
              f"      port={d.get('remote_port')}  tunnel_range="
              f"{d.get('tunnel_start', DEFAULT_TUNNEL_START)}-{d.get('tunnel_end', DEFAULT_TUNNEL_END)}\n"
              f"      txt={d.get('txt')}")


# ---------------------------------------------------------------- device management (Add Device flow)
# Lifecycle for adding phones after the fact: discover iPhones on
# the local network (Bonjour), pick a connector (Tailscale node / manual IP), give
# the phone a display name, save into config. The --all bridge then covers every
# configured device automatically (arms while off-LAN, idle at home).

def _short_host(h):
    """Strip a .local suffix for display; None when empty."""
    h = (h or "").strip().rstrip(".")
    if h.endswith(".local"):
        h = h[:-len(".local")]
    return h or None


def _valid_ip(s):
    if not s or not isinstance(s, str):
        return False
    try:
        socket.inet_aton(s)
        return True
    except Exception:
        pass
    try:
        socket.inet_pton(socket.AF_INET6, s)
        return True
    except Exception:
        return False


def _alloc_tunnel_range(devices):
    """A default-width tunnel window that does not overlap any existing device's.

    Two phones remote at the same time each own a fixed pool: with overlapping
    ranges the second pump would find every port busy (or, worse, tunnel traffic
    could land on a socket owned by the OTHER phone). Assigning disjoint windows
    keeps both fully covered; rare out-of-window endpoints are handled by the
    TunnelWatcher on demand (its bind is a hard fail if the other pump owns it,
    never mis-delivery)."""
    width = DEFAULT_TUNNEL_END - DEFAULT_TUNNEL_START + 1
    start = DEFAULT_TUNNEL_START
    for _ in range(32):
        clashes = []
        for d in devices:
            s, e = plan_ports(d)
            if start <= e and s <= start + width - 1:
                clashes.append(e)
        if not clashes:
            break
        start = max(clashes) + 1
    return start, min(start + width - 1, 65535)


def cmd_discover(args):
    """One-shot Bonjour scan; print a JSON document of the phones found.

    Consumed by the GUI's Add Device window. Each entry is annotated with config
    membership and Tailscale node matching so the UI can pre-fill its choices.
    Records resolving only to this Mac are our (or another bridge's) re-publication
    of a remote phone: reported with source='bridge-copy' so the UI can label them."""
    recs = browse_phones(args.seconds)
    error = None
    data = None
    if recs is None:
        error = ("missing dependency 'zeroconf' - rebuild the venv: "
                 "/usr/bin/python3 -m venv .venv-xbridge && .venv-xbridge/bin/pip install zeroconf")
        recs = []
    found = []
    nodes = []
    if not error:
        cfg = load_config(args.config)
        data = tailscale_status_json()  # cached - one CLI call covers every match + the node list
        for rec in recs:
            ident = str(rec.get("id", "") or "")
            host = (rec.get("bonjour_host") or "").lower().rstrip(".")
            same = [x for x in cfg["devices"]
                    if (ident and str(x.get("id", "")).lower() == ident.lower())
                    or (host and x.get("bonjour_host") and
                        x["bonjour_host"].lower().rstrip(".") == host)]
            same0 = next((x for x in cfg["devices"] if any(x is m for m in same)), None)
            short = _short_host(host) or (rec.get("label") or "")
            # Tailscale node candidates: the stored name first (stable spelling),
            # then the phone's bonjour host label(s) as capture does.
            cands = []
            for cand in (same0.get("tailscale_node") if same0 else None,
                         short, *([short.split(".")[0]] if short and "." in str(short) else [])):
                if cand and all(c.lower() != str(cand).lower() for c in cands):
                    cands.append(cand)
            node = ip = None
            online = None
            for cand in cands:
                if not data:
                    break
                i2, o2 = find_tailnet_node(data, cand)
                if i2:
                    node, ip, online = (same0.get("tailscale_node") if same0 and
                                        str(same0["tailscale_node"]).lower().strip(".") ==
                                        str(cand).lower().strip(".") else cand), i2, o2
                    break
            suggested = "tailscale" if node else \
                ((same0 or {}).get("provider") in (None, "manual-ip") and same0 is not None
                 or node) and "tailscale" or ("manual-ip")
            found.append({
                "id": ident,
                "label": rec.get("label"),
                "bonjour_host": (rec.get("bonjour_host") or
                                 (host + ".local" if host else None)),
                "short_name": short,
                "port": int(rec.get("remote_port") or 49152),
                "txt": rec.get("txt") or {},
                "source": "bridge-copy" if rec.get("_bridge_copy") else "phone",
                "in_config": bool(same),
                "config_label": (same0 or {}).get("label"),
                "config_provider": (same0 or {}).get("provider"),
                "config_ip": (same0 or {}).get("fallback_ip"),
                "provider_suggestion": suggested,
                "tailscale_node": node,
                "tailnet_ip": ip,
                "online": online,
            })
    if not error and data:
        nodes = [{"name": n, "ip": ip or "", "online": online}
                 for (n, ip, online) in tailnet_nodes_brief(data)]
    doc = {"ok": error is None, "error": error,
           "count": len(found), "seconds": args.seconds, "found": found,
           "tailnet_nodes": nodes}
    print(json.dumps(doc))
    return 0 if error is None else 1


def cmd_add_device(args):
    """Add (or re-identify) a device profile into the config from an explicit choice.

    --scan  passes the whole discovered entry (JSON: id/host/port/txt/...);
    field flags override it. Human-managed fields (label/provider/node/ip/range)
    follow: explicit flag > existing profile for this phone > scan suggestion.
    Prints a JSON result document (ok/error/device/note)."""
    def fail(msg):
        print(json.dumps({"ok": False, "error": msg}))
        log("ERROR: " + msg)
        return 1

    scan = {}
    if args.scan:
        try:
            scan = json.loads(args.scan)
        except Exception:
            return fail("--scan is not valid JSON")
    ident = (args.id or str(scan.get("id") or "")).strip()
    host = (args.host or str(scan.get("bonjour_host") or "")).strip().rstrip(".")
    if not ident:
        return fail("no pairing identifier - pass --id, or add-device after a discover "
                    "(a real phone publishes one in its TXT record)")
    try:
        port = int(args.port if args.port is not None else scan.get("remote_port") or 49152)
    except (TypeError, ValueError):
        return fail("invalid remote port")
    if not 0 < port < 65536:
        return fail("remote port out of range")
    txt = {}
    if args.txt:
        try:
            txt = json.loads(args.txt)
        except Exception:
            return fail("--txt is not valid JSON")
    elif isinstance(scan.get("txt"), dict):
        txt = scan["txt"]

    cfg = load_config(args.config)
    hlow = host.lower() if host else ""
    same = [x for x in cfg["devices"]
            if str(x.get("id", "")).lower() == ident.lower()
            or (hlow and x.get("bonjour_host") and
                x["bonjour_host"].lower().rstrip(".") == hlow)]
    keep = dict(same[0]) if same else {}

    provider = (args.provider or scan.get("provider_suggestion")
                or keep.get("provider") or "tailscale")
    if provider not in ("tailscale", "manual-ip"):
        return fail(f"unknown provider {provider!r} (tailscale | manual-ip)")

    node = args.tailscale_node or scan.get("tailscale_node") or keep.get("tailscale_node")
    manual_ip = args.ip if provider == "manual-ip" else None
    if provider == "manual-ip":
        manual_ip = args.ip or keep.get("fallback_ip") or scan.get("config_ip")
        if not _valid_ip(manual_ip):
            return fail("manual-ip provider needs a valid --ip the Mac can reach "
                        "(e.g. the phone's static LAN or public IP)")
    ts_node, ip = node, manual_ip
    if provider == "tailscale":
        data = tailscale_status_json()
        resolved = find_tailnet_node(data, node)[0] if (data and node) else None
        ts_node = node or None
        ip = args.ip or resolved or keep.get("fallback_ip")
        if ts_node is None and not _valid_ip(ip):
            avail = ", ".join(
                f"{n} ({i2 or '?'}, {'online' if o else 'offline'})"
                if o is not None or i2
                else str(n)
                for (n, i2, o) in tailnet_nodes_brief(data)) or "(none visible)"
            return fail("no matching Tailscale node found - pass --tailscale-node "
                        f"(visible on this tailnet: {avail}; a node whose IP is one you can "
                        f"already reach the phone at is almost always its node) or use "
                        f"--provider manual-ip --ip …. If this phone is not among those nodes, "
                        f"its Tailscale app is not logged into this Mac's tailnet (same account; "
                        f"check the admin console for a pending-approval node)")

    label = ((args.name or "").strip()
             or _short_host(host) or keep.get("label") or ident)

    ts, te = (args.tunnel_start if args.tunnel_start is not None else None,
              args.tunnel_end if args.tunnel_end is not None else None)
    note = []
    if ts is None and te is None:
        if keep.get("tunnel_start") or keep.get("tunnel_end"):
            ts = int(keep.get("tunnel_start") or DEFAULT_TUNNEL_START)
            te = int(keep.get("tunnel_end") or DEFAULT_TUNNEL_END)
        else:
            others = [d for d in cfg["devices"] if all(d is not m for m in same)]
            ts, te = _alloc_tunnel_range(others)
            if (ts, te) != (DEFAULT_TUNNEL_START, DEFAULT_TUNNEL_END):
                note.append(f"assigned tunnel range {ts}-{te} (disjoint from the other "
                            f"device(s), so both can bridge at once)")
    if not (0 < ts <= te <= 65535):
        return fail(f"invalid tunnel range {ts}-{te}")

    rec = {
        "id": ident,
        "label": label,
        "service_type": (scan.get("service_type") or keep.get("service_type")
                         or "_remotepairing._tcp"),
        "bonjour_host": host or keep.get("bonjour_host"),
        "remote_port": port,
        "txt": txt,
        "provider": provider,
        "tailscale_node": ts_node if provider == "tailscale" else None,
        "fallback_ip": ip,
        "tunnel_start": ts,
        "tunnel_end": te,
    }
    try:
        cfg["devices"] = [x for x in cfg["devices"] if all(x is not m for m in same)]
        cfg["devices"].append(rec)
        save_config(cfg, args.config)
    except Exception as e:
        return fail(f"could not save config ({args.config}): {e}")
    if same:
        note.append(f"replaced the existing profile (pairing id {keep.get('id')})")
    print(json.dumps({"ok": True, "replaced": bool(same), "device": rec,
                      "note": "; ".join(note) or None}))
    log(f"added device: {label} (id={ident}) provider={provider} "
        f"tunnel {ts}-{te}" + (f" - replaced stale profile" if same else ""))
    return 0


def cmd_remove_device(args):
    """Remove device profile(s) from the config (--uuid prefix and/or --host)."""
    def fail(msg):
        print(json.dumps({"ok": False, "error": msg}))
        log("ERROR: " + msg)
        return 1

    cfg = load_config(args.config)
    key_u = (args.uuid or "").strip().lower()
    key_h = (args.host or "").strip().lower().rstrip(".")
    if not key_u and not key_h:
        return fail("pass --uuid (prefix ok) or --host")
    matches = [x for x in cfg["devices"]
               if (key_u and str(x.get("id", "")).lower().startswith(key_u))
               or (key_h and x.get("bonjour_host") and
                   x["bonjour_host"].lower().rstrip(".") == key_h)]
    if not matches:
        return fail(f"no device matching {args.uuid or ''}{args.host or ''!r} in "
                    f"{args.config} (see 'devices')")
    try:
        cfg["devices"] = [x for x in cfg["devices"]
                          if all(x is not m for m in matches)]
        save_config(cfg, args.config)
    except Exception as e:
        return fail(f"could not save config ({args.config}): {e}")
    names = [m.get("label") or m.get("id") for m in matches]
    print(json.dumps({"ok": True, "removed": names}))
    log(f"removed {len(matches)} device(s): {', '.join(names)}")
    return 0


def cmd_devices(args):
    """List configured devices as JSON (for the GUI's Manage window), merging the
    bridge's live state.json buckets and each device's current remote resolution."""
    cfg = load_config(args.config)
    buckets = {}
    try:
        with open(STATE_PATH) as f:
            sdoc = json.load(f)
        for b in sdoc.get("devices") or []:
            key = str(b.get("label") or "").lower()
            if key:
                buckets[key] = b
    except Exception:
        pass  # state.json absent/stale - live fields just read as None
    devs = []
    for d in cfg["devices"]:
        label = d.get("label") or d.get("bonjour_host") or str(d.get("id"))
        ip, online = resolve_remote_ip(d)
        b = buckets.get(label.lower()) or {}
        devs.append({
            "id": d.get("id"),
            "label": label,
            "short_name": _short_host(label) or label,
            "provider": d.get("provider"),
            "tailscale_node": d.get("tailscale_node"),
            "fallback_ip": d.get("fallback_ip"),
            "remote_port": d.get("remote_port"),
            "tunnel_start": d.get("tunnel_start", DEFAULT_TUNNEL_START),
            "tunnel_end": d.get("tunnel_end", DEFAULT_TUNNEL_END),
            "resolved_ip": ip,
            "online": online,
            "bucket": b.get("bucket"),
            "bridging": (b.get("bridge") == "on"),
        })
    print(json.dumps({"ok": True, "count": len(devs), "devices": devs}))
    return 0


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)


    p = sub.add_parser("capture", help="browse local Bonjour for iPhones")
    p.add_argument("--seconds", type=float, default=20)
    p.add_argument("--remote-ip", help="static phone IP if no tailscale match")

    p = sub.add_parser("bridge", help="publish + relay (long-running)")
    p.add_argument("--uuid", help="bridge a specific device id (prefix ok); default: first")
    p.add_argument("--all", action="store_true", help="bridge every configured device")
    p.add_argument("--bind-host", default=None, help="default 127.0.0.1")
    p.add_argument("--advertise-ip", default=None, help="address put in the mDNS record")
    p.add_argument("--tunnel-start", type=int, default=None)
    p.add_argument("--tunnel-end", type=int, default=None)
    p.add_argument("--remote-port-offset", type=int, default=0,
                   help="shift remote-side tunnel ports by N (self-test / port avoidance)")
    p.add_argument("--health-interval", type=float, default=20)
    p.add_argument("--no-publish", action="store_true", help="relay only, no mDNS record")
    p.add_argument("--allow-local", action="store_true",
                   help="bridge even if the phone appears on your local network")
    p.add_argument("--auto", action="store_true",
                   help="keep running: arm the bridge while the iPhone is off your local "
                        "network, release it (zero ports held) when it returns")
    p.add_argument("--toggle-interval", type=float, default=20,
                   help="--auto: seconds between arm/release checks (default 20)")
    p.add_argument("--disarm-fails", type=int, default=3,
                   help="--auto: consecutive unreachable probes before releasing "
                        "(hysteresis against Tailscale flaps; default 3)")
    p.add_argument("--recapture-interval", type=int, default=300,
                   help="--auto: re-scan Bonjour every N seconds and adopt the phone's "
                        "(possibly churned) pairing identifier; 0 disables (default 300)")
    p.add_argument("--advertise-mode", choices=["auto", "relay", "direct6", "hybrid"],
                   default="auto",
                   help="'auto'|'relay' = parity with commercial bridges (RECOMMENDED): the record is "
                        "registered inside mDNSResponder via a kept-alive 'dns-sd -P' child - "
                        "SRV target = the phone's .local name, A record = this Mac. Only "
                        "mDNSResponder's OWN registrations persist in macOS 27's scoped "
                        "resolver cache all day (raw multicast copies decay in minutes). "
                        "'hybrid' = experimental dual A+AAAA via raw mDNS; 'direct6' = "
                        "experimental AAAA-only, no relays.")
    p.add_argument("--idle-timeout", type=float, default=300.0,
                   help="seconds of silence before a relayed session is torn down (default 300)")
    p.add_argument("--link-port", type=int, default=DEFAULT_LINK_PORT,
                   help="mirror this UDP port (default 3722 = macOS liveness probe) to the phone; "
                        "0 disables")
    p.add_argument("--no-watch", action="store_true",
                   help="disable the system-log tunnel-port watcher")

    p = sub.add_parser("scan", help="one-shot reachability check")
    p.add_argument("--uuid")

    sub.add_parser("status", help="show config + liveness")

    p = sub.add_parser("discover", help="scan the local network for iPhones (JSON; feeds the Add Device window)")
    p.add_argument("--seconds", type=float, default=12,
                   help="how long to browse Bonjour (default 12)")

    p = sub.add_parser("add-device", help="add a device profile to the config")
    p.add_argument("--scan", help="JSON of a discovered entry from 'discover' (identity source)")
    p.add_argument("--id", help="pairing identifier (overrides --scan)")
    p.add_argument("--host", help="bonjour host, e.g. my-iphone.local")
    p.add_argument("--port", type=int, default=None, help="RemotePairing port (default: from --scan or 49152)")
    p.add_argument("--txt", help="TXT record as a JSON object")
    p.add_argument("--name", help="display name (default: the phone's bonjour host)")
    p.add_argument("--provider", choices=["tailscale", "manual-ip"], default=None,
                   help="network connector (default: suggested by the scan)")
    p.add_argument("--ip", help="IP to reach the phone at (required for manual-ip)")
    p.add_argument("--tailscale-node", help="node name in your tailnet (default: matched by the scan)")
    p.add_argument("--tunnel-start", type=int, default=None,
                   help="start of the tunnel port window (default: auto-assigned, disjoint)")
    p.add_argument("--tunnel-end", type=int, default=None)

    p = sub.add_parser("remove-device", help="remove device profile(s) from the config")
    p.add_argument("--uuid", help="device id (prefix ok)")
    p.add_argument("--host", help="bonjour host")

    p = sub.add_parser("devices", help="list configured devices as JSON (Manage window)")
    p.add_argument("--json", action="store_true", default=False,
                   help=("no-op alias: 'devices' always prints JSON; accepted so GUI builds "
                         "that pass it (v1.5) keep working"))

    for p in sub.choices.values():
        if not any(getattr(a, "dest", None) == "config" for a in p._actions):
            p.add_argument("--config", default=CONFIG_PATH, help="path to config file")

    args = ap.parse_args()


    handlers = {"bridge": cmd_bridge, "capture": cmd_capture,
                "scan": cmd_scan, "status": cmd_status,
                "discover": cmd_discover, "add-device": cmd_add_device,
                "remove-device": cmd_remove_device, "devices": cmd_devices}
    return handlers[args.cmd](args) or 0


if __name__ == "__main__":
    sys.exit(main())
