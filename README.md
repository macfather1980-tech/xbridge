# XBridge

**Free, open-source wireless debugging for Xcode.** Keeps your iPhone visible to
Xcode when it is on a different Wi‑Fi than your Mac: XBridge re-publishes the
phone's Bonjour identity locally and relays the debugging traffic over your own
mesh VPN (Tailscale). One click in the menu bar. No cloud, no account, no
telemetry.

> **Independent project.** XBridge is an independent, open-source project. It
> is **not affiliated with, endorsed by, or sponsored by** Apple, Tailscale, or
> any commercial bridge product. It is implemented independently from public
> information and observed behaviour of the ecosystem.

## The problem

Xcode's wireless debugging only finds an iPhone on the **same local network**:
Apple uses Bonjour/mDNS (`_remotepairing._tcp`), which never crosses networks.
Borrow a tester's phone at the office, take your laptop to a cafe, work from
two offices — the device disappears from Xcode.

XBridge makes it visible again, without touching the iPhone:

1. **capture** — while the phone is on your Wi‑Fi once, record its real Bonjour
   identity (pairing UUID, TXT record, control port).
2. **bridge** — re-publish that identity on the Mac (pointing at `127.0.0.1`),
   so Xcode and `devicectl` "see" the phone locally; then relay the
   RemotePairing control port and the CoreDevice tunnel range (default
   55000–58000) to the phone over Tailscale — TCP fully bidirectional, UDP in
   both directions.
3. **auto** (default) — a daemon checks every ~20 s: phone off your LAN → arm
   the bridge; phone back on your LAN → release everything (zero ports, no
   mDNS), so the built-in Xcode flow is undisturbed at home.

All traffic stays inside your own tailnet. XBridge holds no pairing keys — it
relays bytes between sockets. There is no XBridge server, account, or telemetry.

## Features

| | |
|---|---|
| Menu-bar app | One click to turn the bridge on/off; live status icon; Add / Manage Devices windows |
| `--auto` state machine | Zero footprint while the phone is at home; arms automatically when it leaves |
| Multi-device | Each device gets its own non-overlapping tunnel port range; several phones at once |
| Open source | The full engine and GUI are in this repo — audit exactly what runs on your Mac |
| Free | No license, no subscription. The only account you need is your own (free) Tailscale |

## Security model (read this first)

- The Xcode-facing listener binds **`0.0.0.0`** by default, because the
  verified publish path points the mDNS A record at the Mac's LAN IP — Xcode
  dials that address, so the listener must accept on that interface (the same
  behaviour as commercial bridges). Any host on your LAN can therefore *reach*
  the listener, but it cannot pair (no key material is exposed), and the UDP
  reply socket only accepts packets from the phone's tailnet peer.
- If your LAN is untrusted, run a loopback-only publish instead:
  `--bind-host 127.0.0.1 --advertise-ip 127.0.0.1` (some clients filter
  records that resolve to loopback — try it, and keep whichever works).
- Relay traffic is encrypted inside your tailnet; protect your tailnet like any
  other VPN (Taillock, approved devices, etc.).

## Requirements

- macOS (developed and tested on current macOS; menu-bar app needs 13+)
- Xcode Command Line Tools (`xcode-select --install`) — provides python3 and swiftc
- [Tailscale](https://tailscale.com/) (free) on the Mac **and** the iPhone,
  signed in with the same account, VPN on
- The iPhone on **Wi‑Fi** (cellular doesn't work — the phone's RemotePairing
  daemon listens on Wi‑Fi only) and paired with Xcode at least once

## Quick start

```sh
git clone https://github.com/macfather1980-tech/xbridge ~/tools/xbridge
cd ~/tools/xbridge
./install.sh --gui        # venv + deps, launchd agent, menu-bar app
```

Then, with the iPhone on your Wi‑Fi:

```sh
.venv-xbridge/bin/python3 xbridge.py capture   # record the phone's Bonjour identity
```

From then on the menu-bar icon does the rest:

| Icon | Meaning |
|---|---|
| `⏻` | **Off** (bridge not running) |
| `◌` | Starting |
| `○` | On, but the phone is on your Wi‑Fi → Xcode's built-in flow; zero ports used |
| `⇄` | **Bridging**: phone is elsewhere, traffic over Tailscale, Xcode works normally |

Left-click the icon = on/off. Right-click = live status, **Add Device…**,
**Manage Devices…**, open log, launch at login, quit.

> The menu-bar app's "dev mode" expects the engine at `~/tools/xbridge` — hence
> the clone location above. The engine itself runs from wherever it is installed.

## Adding more iPhones

Right-click the menu-bar icon → **Add Device…**: local Bonjour discovery of
phones on your Wi‑Fi (with Refresh) → choose the connector — the matching
Tailscale node (auto-matched by name, or by the phone's known IP when the node
name is unrelated) or a manual IP — → display name → Save. Each new device
automatically gets a non-overlapping tunnel port range, so several phones can
be bridged at once. **Manage Devices…** lists all configured devices with live
status and lets you remove them.

CLI equivalent:

```sh
PY=.venv-xbridge/bin/python
$PY xbridge.py discover                 # JSON list of iPhones on this Wi-Fi (+ tailnet match)
$PY xbridge.py add-device --id <UUID> --host <name.local> \
     [--port N] [--txt '{...}'] [--name "…"] --provider tailscale|manual-ip \
     [--tailscale-node <node>] [--ip <IP>]
$PY xbridge.py devices                   # configured devices + live status
$PY xbridge.py remove-device --uuid <prefix>
```

## The `--auto` state machine

| Situation | XBridge |
|---|---|
| iPhone on your LAN (Bonjour visible → built-in Xcode works) | **idle**: no ports, no mDNS, zero interference |
| iPhone off your LAN, reachable via Tailscale | **armed**: binds the relays, publishes the record — Xcode works |
| iPhone off your LAN, Tailscale down | stays idle, logs "is Tailscale connected?" — arms automatically when it returns |

## CLI reference

```sh
PY=.venv-xbridge/bin/python
$PY xbridge.py bridge --all --auto   # long-running daemon (the launchd agent runs this)
$PY xbridge.py scan [--uuid U]       # one-shot reachability check (port ping)
$PY xbridge.py status                # config + phone liveness
$PY xbridge.py capture [--seconds N] # re-record Bonjour identity (phone must be on your Wi-Fi)
$PY xbridge.py discover|add-device|remove-device|devices   # see "Adding more iPhones"
```

Key flags: `--bind-host`, `--advertise-ip` (which interface IP to advertise;
default `auto` = your LAN IP), `--tunnel-start/--tunnel-end` (port range),
`--toggle-interval` (auto check period, default 20 s), `--advertise-mode`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Xcode doesn't see the phone away from home | ① Is the icon `⇄`? If not, left-click to turn it on. ② Is the phone's Tailscale connected? ③ `scan` → REACHABLE? |
| `scan` REACHABLE but Xcode is still empty | The pairing UUID may have changed → run `capture` with the phone back on your LAN; check `~/Library/Logs/xbridge.log` for "could not publish" |
| Tunnel connections occasionally fail | Log shows "port(s) busy locally" — Xcode negotiated a port outside your static range; it usually retries onto a port the watcher takes over |
| Add Device finds no iPhone | ① Is the phone on this Mac's Wi‑Fi? (discovery is Bonjour-only) ② macOS Local Network permission dialog — allow XBridge once |
| 49152 already in use | That is macOS's own `remotepairingd` — xbridge automatically picks the next free port |
| Running XBridge together with another bridge app | Don't — two publishers of the same UUID interfere. Quit the other one |
| "is on the local network" refusal | The phone is on your Wi‑Fi → use Xcode's built-in flow; `--auto` simply goes idle |

## What it costs you

XBridge itself is free. You need **Tailscale** on both ends:

- Personal, non-commercial use: the free Tailscale Personal plan (see
  [tailscale.com/pricing](https://tailscale.com/pricing) for current limits).
- Commercial / team use: Tailscale's ToS expects a paid business plan. That
  cost applies equally to any mesh-VPN-based bridge.

## License & dependencies

- XBridge (this repository): **MIT** — see [LICENSE](LICENSE).
- Python engine dependencies:
  [python-zeroconf](https://github.com/python-zeroconf/python-zeroconf)
  (LGPL-2.1-or-later), [ifaddr](https://github.com/pydron/ifaddr) (MIT) —
  see [THIRD-PARTY.md](THIRD-PARTY.md).

## Development

```sh
/usr/bin/python3 -m venv .venv-xbridge
.venv-xbridge/bin/pip install zeroconf ifaddr     # or just run ./install.sh
swiftc -O -target "$(uname -m)-apple-macos13.0" xbridge-gui.swift -o /tmp/XBridge && /tmp/XBridge
```

`xbridge.py` is a single-file CLI containing all engine logic;
`xbridge-gui.swift` is a single-file AppKit menu-bar app. No build system, no
vendored code.

## Changelog

- **v1.5** — multi-device: Add / Manage Devices windows; `discover` /
  `add-device` / `devices` / `remove-device` CLI; per-device non-overlapping
  tunnel port ranges.
- **v1.4** — publish via `dns-sd -P` (mDNSResponder itself): registrations
  persist in mDNSResponder's cache, so Network.framework resolution stays
  valid for hours instead of minutes.
- **v1.3** — dual A/AAAA record publish; UDP reply socket allow-listed to the
  phone's tailnet peer.
- **v1.2** — `--advertise-mode` (relay / hybrid / direct6), link-port mirror
  (UDP :3722) so the phone's daemon answers macOS liveness probes, TunnelWatcher
  for dynamically negotiated tunnel ports.
- **v1.1** — `--auto` state machine, live `state.json`, menu-bar app, launchd
  agent.
- **v1.0** — capture + bridge: TCP/UDP relays for the RemotePairing control
  port and the CoreDevice tunnel range.

---

XBridge is an independent project, not affiliated with Apple, Tailscale, or any
commercial bridge product. It is provided "as is" (MIT) — it opens local network listener
sockets and is not sandboxed; run it only on devices and networks you are
authorized to configure.
