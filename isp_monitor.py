"""
ISP Monitor v6.1 (improved from v6)
- v6.1: the verdict strip seeds from the latest logged test on startup
  (new verdict column in the CSV, auto-migrated), instead of showing the
  placeholder until the first new test runs
- v6: packet loss %, WiFi signal %, separate DNS check, top verdict strip,
  downtime totals + outage windows + slowest hours in the report, copy-ISP-
  summary button, Windows autostart, outage sound + taskbar flash, light
  theme (applies on restart), sortable log (newest first), chart plan line
  + downsampling for long histories, first-run hint, remembered window size
- v5.3: animated spinner + status text in the top bar while any test runs;
  TEST NOW / LINE SPEED buttons disable during a run, so a click can no
  longer vanish silently when another test is still going
- Logs to isp_log.csv (opens in Excel or Notepad, no install needed)
- Live event indicator pills with timestamps
- Network layer diagnostics: WiFi/Ethernet, Router, Modem, ISP
- Pinpoints WHERE the issue is on each test
- Save as plain .txt report / open CSV directly
- v5.1: main window scrolls (canvas + scrollbar) and fits screen height
- v5.2: ping/traceroute/netsh/ipconfig helpers run hidden — no console
  windows flashing during tests; help text updated for v5/v5.1/v5.2
- v5.3: animated spinner + status text in the top bar while any test runs;
  TEST NOW / LINE SPEED buttons disable during a run, so a click can no
  longer vanish silently when another test is still going
- v5 improvements:
  * Real ICMP ping first (TCP-connect fallback) instead of TCP-only
  * Traceroute hop diagnosis when a test flags a problem (+ trace_log.txt)
  * Time-boxed routine download test (8 s / 25 MB) so fast lines measure
    real throughput instead of connection setup
  * LINE SPEED now measures upload too and logs results to peak_log.csv
  * Settings row: your plan speed, slow-alert % of plan, check interval
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import threading, time, socket, urllib.request
import statistics, csv, os, sys, subprocess, re
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
LOG_FILE      = "isp_log.csv"
TEST_INTERVAL = 300
LAG_MS        = 150
JITTER_MS     = 40
SLOW_MBPS     = 10.0
OUTAGE_MBPS   = 0.5
CDN_HOST      = "cdnjs.cloudflare.com"
CDN_PORT      = 443
PING_SAMPLES  = 5
STABLE_IP     = "8.8.8.8"   # Google DNS — no DNS resolution needed, modem-level check
STABLE_IP2    = "1.1.1.1"   # Cloudflare DNS — secondary check

CSV_HEADERS = ["timestamp","datetime","dl_mbps","ping_ms","jitter_ms",
               "gw_ms","modem_ms","success","flags","layer",
               "loss_pct","wifi_signal","dns_ms","verdict"]

DOWNLOAD_TARGETS = [
    ("three.js",  "https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"),
    ("d3.js",     "https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js"),
    ("chart.js",  "https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"),
    ("lodash",    "https://cdnjs.cloudflare.com/ajax/libs/lodash.js/4.17.21/lodash.min.js"),
    ("react",     "https://cdnjs.cloudflare.com/ajax/libs/react/18.2.0/umd/react.production.min.js"),
]

# ── User settings (v5) ────────────────────────────────────────────────────────
# Stored in isp_config.json next to the .exe (created on first save).
# plan_mbps: 0 = not set (absolute 10 Mbps slow threshold applies).
# slow_pct : flag SLOW when download falls below this % of your plan.
# interval_min: minutes between automatic monitoring tests.
CONFIG_FILE    = "isp_config.json"
PEAK_LOG_FILE  = "peak_log.csv"
TRACE_LOG_FILE = "trace_log.txt"
DL_TEST_SECONDS = 8.0    # time-box for the routine download test
DL_TEST_MAX_MB  = 25.0   # cap for the routine download test

def _base_dir():
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent

def load_config():
    defaults = {"plan_mbps": 0, "slow_pct": 50, "interval_min": 5,
                "theme": "dark", "sound_on_outage": 1, "hint_dismissed": 0,
                "geometry": ""}
    try:
        import json
        with open(_base_dir() / CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
        for k in ("plan_mbps", "slow_pct", "interval_min",
                  "sound_on_outage", "hint_dismissed"):
            if isinstance(data.get(k), (int, float)):
                defaults[k] = data[k]
        if data.get("theme") in ("dark", "light"):
            defaults["theme"] = data["theme"]
        if isinstance(data.get("geometry"), str):
            defaults["geometry"] = data["geometry"]
    except Exception:
        pass
    return defaults

_CONFIG = load_config()
PLAN_MBPS    = float(_CONFIG["plan_mbps"])
SLOW_PCT     = float(_CONFIG["slow_pct"])
INTERVAL_MIN = float(_CONFIG["interval_min"])
SOUND_ON_OUTAGE = int(_CONFIG["sound_on_outage"])

def save_config(plan_mbps, slow_pct, interval_min):
    global PLAN_MBPS, SLOW_PCT, INTERVAL_MIN, _CONFIG
    import json
    # Merge, don't replace: theme/sound/hint/geometry keys must survive.
    _CONFIG.update({"plan_mbps": plan_mbps, "slow_pct": slow_pct,
                    "interval_min": interval_min})
    PLAN_MBPS, SLOW_PCT, INTERVAL_MIN = float(plan_mbps), float(slow_pct), float(interval_min)
    with open(_base_dir() / CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(_CONFIG, f, indent=2)

def update_config_value(key, value):
    """Save one setting (theme, sound, hint, geometry) without touching others."""
    global _CONFIG, SOUND_ON_OUTAGE
    import json
    _CONFIG[key] = value
    if key == "sound_on_outage":
        SOUND_ON_OUTAGE = int(value)
    with open(_base_dir() / CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(_CONFIG, f, indent=2)

def slow_threshold():
    """Mbps below which a test flags SLOW. Plan-aware when a plan is set."""
    if PLAN_MBPS > 0:
        return max(SLOW_MBPS, PLAN_MBPS * SLOW_PCT / 100.0)
    return SLOW_MBPS

def interval_seconds():
    return max(60, int(INTERVAL_MIN * 60))

# ── Palette ───────────────────────────────────────────────────────────────────
BG      = "#0d0d14"
SURFACE = "#13131e"
CARD    = "#191926"
CARD2   = "#1e1e2e"
BORDER  = "#252538"
ACCENT  = "#00e5a0"
BLUE    = "#4d9fff"
PURPLE  = "#a78bfa"
WARN    = "#ffaa00"
DANGER  = "#ff4466"
TEXT    = "#e2e2f0"
TEXT2   = "#6a6a90"
TEXT3   = "#3a3a58"
GOOD_BG = "#081a10"
WARN_BG = "#1a1000"
BAD_BG  = "#1a0008"
CHART_FILL = "#081812"
INFO_BG    = "#120d1e"
SEL_BG     = "#22223a"
# Light theme (v6): chosen in isp_config.json, applied at startup. A theme
# change in Settings takes effect on the next launch — every widget reads
# these constants when it is built, so a restart is the reliable switch.
if _CONFIG.get("theme") == "light":
    BG, SURFACE, CARD, CARD2, BORDER = "#f0f0f5", "#ffffff", "#ffffff", "#e6e6ee", "#d2d2e0"
    TEXT, TEXT2, TEXT3 = "#17172a", "#55556e", "#8a8aa5"
    GOOD_BG, WARN_BG, BAD_BG = "#e2f6ea", "#fdf3d7", "#fde2e8"
    CHART_FILL = "#e2f6ea"
    INFO_BG = "#efe9f5"
    SEL_BG = "#c9c9e8"
FNT     = "Courier New"
FNT2    = "Segoe UI"

# ── Network helpers ───────────────────────────────────────────────────────────

def _hidden_startupinfo():
    """Windows: run console helpers (ping, tracert, netsh, ipconfig) with no
    visible console window. Without this, every helper call flashes a
    console on screen — about 15 flashes per TEST NOW. None elsewhere."""
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        return si
    return None

def get_default_gateway():
    """Return default gateway IP string, or None."""
    try:
        if sys.platform == "win32":
            out = subprocess.check_output(
                ["ipconfig"], timeout=5, text=True, stderr=subprocess.DEVNULL,
                startupinfo=_hidden_startupinfo())
            for line in out.splitlines():
                l = line.strip()
                if "Default Gateway" in l or "Standardgateway" in l:
                    parts = l.split(":")
                    if len(parts) >= 2:
                        ip = parts[-1].strip()
                        if ip and ip != "" and "." in ip:
                            return ip
        else:
            out = subprocess.check_output(
                ["ip", "route"], timeout=5, text=True, stderr=subprocess.DEVNULL,
                startupinfo=_hidden_startupinfo())
            for line in out.splitlines():
                if line.startswith("default"):
                    parts = line.split()
                    if len(parts) >= 3:
                        return parts[2]
    except Exception:
        pass
    return None

def get_connection_type():
    """Return ('WiFi', ssid) or ('Ethernet', adapter) or ('Unknown', '')."""
    try:
        if sys.platform == "win32":
            out = subprocess.check_output(
                ["netsh", "wlan", "show", "interfaces"],
                timeout=5, text=True, stderr=subprocess.DEVNULL,
                startupinfo=_hidden_startupinfo())
            if "State" in out and "connected" in out.lower():
                ssid = ""
                for line in out.splitlines():
                    if "SSID" in line and "BSSID" not in line:
                        ssid = line.split(":")[-1].strip()
                        break
                return ("WiFi", ssid)
            # Check ethernet via ipconfig
            out2 = subprocess.check_output(
                ["ipconfig"], timeout=5, text=True, stderr=subprocess.DEVNULL,
                startupinfo=_hidden_startupinfo())
            for line in out2.splitlines():
                if "Ethernet adapter" in line:
                    adapter = line.replace("Ethernet adapter","").replace(":","").strip()
                    return ("Ethernet", adapter)
        else:
            out = subprocess.check_output(
                ["iwgetid", "-r"], timeout=3, text=True, stderr=subprocess.DEVNULL,
                startupinfo=_hidden_startupinfo()).strip()
            if out:
                return ("WiFi", out)
            return ("Ethernet", "")
    except Exception:
        pass
    return ("Unknown", "")

def _parse_netsh_signal(text):
    """Parse 'Signal : 82%' from netsh wlan show interfaces output."""
    m = re.search(r"Signal\s*:\s*(\d+)%", text or "")
    return int(m.group(1)) if m else None

def get_wifi_signal():
    """Current WiFi signal % on Windows, else None."""
    if sys.platform != "win32":
        return None
    try:
        out = subprocess.check_output(
            ["netsh", "wlan", "show", "interfaces"],
            timeout=5, text=True, stderr=subprocess.DEVNULL,
            startupinfo=_hidden_startupinfo())
        return _parse_netsh_signal(out)
    except Exception:
        return None

def _dns_query(server, host, timeout=3):
    """Minimal UDP DNS A query. Returns RTT ms, or None on no valid reply."""
    import random, struct
    txid = random.randint(0, 65535)
    header = struct.pack(">HHHHHH", txid, 0x0100, 1, 0, 0, 0)
    qname = b"".join(bytes([len(p)]) + p.encode() for p in host.split(".")) + b"\x00"
    question = qname + struct.pack(">HH", 1, 1)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        t0 = time.perf_counter()
        s.sendto(header + question, (server, 53))
        data, _ = s.recvfrom(512)
        s.close()
        if len(data) >= 2 and struct.unpack(">H", data[:2])[0] == txid:
            return (time.perf_counter() - t0) * 1000
    except Exception:
        pass
    return None

def measure_dns():
    """Returns (default_resolver_ok, independent_resolver_ms).

    default_resolver_ok: whether the system's own DNS (usually the ISP's
    or the router's) resolves the CDN host. independent_resolver_ms: how
    fast 8.8.8.8 answers directly, or None if it doesn't.
    """
    try:
        socket.getaddrinfo(CDN_HOST, CDN_PORT)
        default_ok = True
    except Exception:
        default_ok = False
    explicit_ms = _dns_query(STABLE_IP, CDN_HOST, timeout=3)
    return default_ok, explicit_ms

def tcp_ping(host, port=80, timeout=3):
    """TCP connect ping — returns RTT in ms or None."""
    try:
        t0 = time.perf_counter()
        s = socket.create_connection((host, port), timeout=timeout)
        rtt = (time.perf_counter() - t0) * 1000
        s.close()
        return rtt
    except Exception:
        return None

def icmp_ping(host, timeout=3):
    """Real ICMP echo ping via the system ping command. Returns ms or None."""
    try:
        if sys.platform == "win32":
            cmd = ["ping", "-n", "1", "-w", str(int(timeout * 1000)), host]
        else:
            cmd = ["ping", "-c", "1", "-W", str(int(timeout)), host]
        out = subprocess.check_output(cmd, timeout=timeout + 2,
                                      text=True, stderr=subprocess.DEVNULL,
                                      startupinfo=_hidden_startupinfo())
        m = re.search(r"time[=<](\d+(?:\.\d+)?)\s*ms", out)
        if m:
            return float(m.group(1))
        if "time<1ms" in out or "time <1ms" in out:
            return 0.5
    except Exception:
        pass
    return None

def ping_host(host, port=80, timeout=3):
    """ICMP ping first (real ping), TCP-connect fallback (firewalled hosts)."""
    rtt = icmp_ping(host, timeout)
    if rtt is not None:
        return rtt
    return tcp_ping(host, port, timeout)

def run_traceroute(host, max_hops=20, timeout=35):
    """Hop-by-hop route. Returns [(hop, ms|None, ip|None)] or [] on failure."""
    try:
        if sys.platform == "win32":
            cmd = ["tracert", "-d", "-w", "1000", "-h", str(max_hops), host]
        else:
            cmd = ["traceroute", "-n", "-w", "2", "-q", "1", "-m", str(max_hops), host]
        out = subprocess.check_output(cmd, timeout=timeout,
                                      text=True, stderr=subprocess.DEVNULL,
                                      startupinfo=_hidden_startupinfo())
    except Exception:
        return []
    hops = []
    for line in out.splitlines():
        m = re.match(r"\s*(\d+)\s+(.*)$", line)
        if not m:
            continue
        hop, rest = int(m.group(1)), m.group(2)
        times = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*ms", rest)]
        ip_m = re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", rest)
        ip = ip_m[-1] if ip_m else None
        if "*" in rest and not times:
            hops.append((hop, None, ip))
        elif times:
            hops.append((hop, statistics.mean(times), ip))
    return hops

def diagnose_trace(hops):
    """Conservative hop analysis. Returns (layer, detail) or (None, '').
    Only speaks when the route shows a clean break or one clear bottleneck."""
    if len(hops) < 2:
        return None, ""
    answered = [h for h, ms, _ in hops if ms is not None]
    if answered:
        first_dead = None
        for h, ms, _ in hops:
            if ms is None:
                first_dead = h
                break
        if first_dead is not None and all(ms is None for h, ms, _ in hops if h >= first_dead):
            # NB: first_dead == 1 with nothing answered is ambiguous (ICMP may
            # simply be blocked), so it is handled by the no-override path, not
            # blamed on the router. A break at hop >= 2 is a real ISP break.
            return "isp", "Traceroute dies at hop %d — traffic leaves your home but stops inside your ISP's network." % first_dead
    for i in range(1, len(hops)):
        h, ms, _ = hops[i]
        prev = hops[i-1][1]
        if ms is not None and prev is not None and prev > 0 and ms > max(3 * prev, prev + 50):
            later = [m for _, m, _ in hops[i+1:] if m is not None]
            if later and min(later) >= ms * 0.7:
                if h <= 1:
                    return "router", "Traceroute shows the slowdown starting at hop 1 (your router)."
                return "isp", "Traceroute shows the slowdown starting at hop %d (inside your ISP's network)." % h
    return None, ""

def append_trace_log(hops, layer):
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        desc = " ".join("%d:%s" % (h, ("%.0fms" % ms) if ms is not None else "timeout") for h, ms, _ in hops)
        with open(_base_dir() / TRACE_LOG_FILE, "a", encoding="utf-8") as f:
            f.write("%s  layer=%s  %s\n" % (ts, layer, desc))
    except Exception:
        pass

def ping_ip(ip, timeout=3):
    """Ping a raw IP via TCP port 443 (HTTPS) or 80, fallback to ICMP via socket."""
    # Try TCP 443 first (usually open on routers/gateways for management)
    rtt = tcp_ping(ip, 443, timeout)
    if rtt is None:
        rtt = tcp_ping(ip, 80, timeout)
    if rtt is None:
        # Fallback: raw socket connect on port 53 (DNS)
        rtt = tcp_ping(ip, 53, timeout)
    return rtt

def avg_ping_samples(host, port, samples=4, skip_first=True):
    """Take multiple samples, skip first (warm-up), return avg and stdev."""
    rtts = []
    for i in range(samples + (1 if skip_first else 0)):
        rtt = tcp_ping(host, port, timeout=4)
        if rtt is not None and (i > 0 or not skip_first):
            rtts.append(rtt)
        time.sleep(0.08)
    if not rtts:
        return None, None
    avg = statistics.mean(rtts)
    jit = statistics.stdev(rtts) if len(rtts) > 1 else 0.0
    return avg, jit

def peak_speed_test(progress_cb=None):
    """
    Uses the speedtest-cli library (same infrastructure as Speedtest.net)
    to measure true line speed. Falls back to multi-stream if unavailable.
    """

    # ── Try speedtest-cli first (most accurate) ───────────────────────────────
    try:
        import speedtest as st_lib

        if progress_cb: progress_cb("Finding nearest server…", 10)
        s = st_lib.Speedtest(timeout=15)

        if progress_cb: progress_cb("Connecting to best server…", 25)
        s.get_best_server()

        if progress_cb: progress_cb("Measuring download speed…", 40)

        # Run download in a thread so we can send progress ticks
        result_box = [None]
        err_box    = [None]
        done_evt   = threading.Event()

        def _do_dl():
            try:
                result_box[0] = s.download(threads=None)
            except Exception as e:
                err_box[0] = e
            finally:
                done_evt.set()

        dl_thread = threading.Thread(target=_do_dl, daemon=True)
        dl_thread.start()

        pct = 40
        while not done_evt.wait(timeout=0.5):
            pct = min(88, pct + 3)
            if progress_cb: progress_cb("Measuring download speed…", pct)

        if err_box[0]:
            raise err_box[0]

        if progress_cb: progress_cb("Measuring upload speed…", 90)
        try:
            ul = s.upload(threads=None) / 1e6
        except Exception:
            ul = None
        if progress_cb: progress_cb("Done!", 95)
        return result_box[0] / 1e6, ul   # bits/s → Mbps

    except ImportError:
        # speedtest-cli not installed — shouldn't happen after build, but handle it
        pass
    except Exception:
        pass

    # ── Fallback: persistent-connection pipelining ────────────────────────────
    # Uses HTTP/1.1 keep-alive to reuse TCP connections and pipeline requests,
    # closer to what browsers actually do than simple parallel urllib calls.
    import ssl

    if progress_cb: progress_cb("Fallback: pipelined streams…", 10)

    ctx    = ssl.create_default_context()
    lock   = threading.Lock()
    fb_results = []
    DURATION = 10.0
    PATHS = [
        "/typescript/-/typescript-5.2.2.tgz",
        "/aws-sdk/-/aws-sdk-2.1450.0.tgz",
        "/three/-/three-0.157.0.tgz",
        "/firebase/-/firebase-10.4.0.tgz",
    ]

    def pipelined(duration):
        total = 0
        t0    = time.perf_counter()
        try:
            with socket.create_connection(("registry.npmjs.org", 443), timeout=10) as raw:
                raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
                with ctx.wrap_socket(raw, server_hostname="registry.npmjs.org") as s:
                    idx = 0
                    while time.perf_counter() - t0 < duration:
                        path = PATHS[idx % len(PATHS)]; idx += 1
                        req  = (
                            "GET %s HTTP/1.1\r\nHost: registry.npmjs.org\r\n"
                            "User-Agent: Mozilla/5.0\r\nConnection: keep-alive\r\n"
                            "Cache-Control: no-cache\r\nAccept-Encoding: identity\r\n\r\n"
                        ) % path
                        s.sendall(req.encode())
                        buf = b""
                        while b"\r\n\r\n" not in buf:
                            d = s.recv(4096);
                            if not d: break
                            buf += d
                        cl = 0
                        for line in buf.split(b"\r\n"):
                            if line.lower().startswith(b"content-length:"):
                                try: cl = int(line.split(b":")[1].strip())
                                except: pass
                        body   = buf.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in buf else b""
                        remain = cl - len(body)
                        while remain > 0 and time.perf_counter() - t0 < duration:
                            chunk  = s.recv(min(524288, remain))
                            if not chunk: break
                            remain -= len(chunk)
                        total += cl
        except Exception:
            pass
        with lock: fb_results.append(total)

    CONNS   = 4
    threads = [threading.Thread(target=pipelined, args=(DURATION,), daemon=True)
               for _ in range(CONNS)]
    t_start = time.perf_counter()
    for t in threads: t.start()

    pct = 15
    while any(t.is_alive() for t in threads):
        elapsed = time.perf_counter() - t_start
        pct = min(88, 15 + int((elapsed / DURATION) * 73))
        if progress_cb:
            progress_cb("Pipelined streams… %.0f / %.0f sec" % (elapsed, DURATION), pct)
        time.sleep(0.5)
    for t in threads: t.join(timeout=3)

    if progress_cb: progress_cb("Calculating…", 94)
    total_bytes  = sum(fb_results)
    elapsed_wall = time.perf_counter() - t_start
    dl = (total_bytes * 8) / max(elapsed_wall, 1.0) / 1e6 if total_bytes else None
    return dl, None   # fallback path has no upload measurement


PEAK_CSV_HEADERS = ["timestamp","datetime","dl_mbps","ul_mbps"]

def peak_log_path():
    return _base_dir() / PEAK_LOG_FILE

def append_peak_log(dl_mbps, ul_mbps):
    p = peak_log_path()
    try:
        new = not p.exists() or p.stat().st_size == 0
        with open(p, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(PEAK_CSV_HEADERS)
            w.writerow(["%.3f" % time.time(),
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        ("%.3f" % dl_mbps) if dl_mbps is not None else "",
                        ("%.3f" % ul_mbps) if ul_mbps is not None else ""])
    except Exception:
        pass

def load_peak_log():
    rows = []
    try:
        with open(peak_log_path(), newline="") as f:
            for r in csv.DictReader(f):
                rows.append(r)
    except Exception:
        pass
    return rows

def diagnose_layer(gw_ms, modem_ms, cdn_ms, dl_mbps, conn_type,
                   loss_pct=None, wifi_signal=None,
                   dns_default_ok=None, dns_explicit_ms=None):
    """
    Returns (layer, verdict, detail) where layer is one of:
      'ok', 'wifi', 'router', 'modem', 'isp', 'unknown'
    v6 params: loss_pct (0-100), wifi_signal (0-100), dns_default_ok (bool),
    dns_explicit_ms (ms or None) — all optional, default off.
    """
    gw_ok     = gw_ms is not None and gw_ms < LAG_MS * 1.5
    modem_ok  = modem_ms is not None and modem_ms < LAG_MS * 2
    cdn_ok    = cdn_ms is not None and cdn_ms < LAG_MS
    speed_ok  = dl_mbps is not None and dl_mbps >= slow_threshold()

    # Full outage
    if gw_ms is None and modem_ms is None and cdn_ms is None:
        if conn_type == "WiFi":
            return ("wifi", "No response at any level",
                    "Cannot reach router. Check WiFi connection or restart router.")
        return ("router", "No response at any level",
                "Cannot reach router or gateway. Check cables or restart modem/router.")

    # Can reach gateway but not internet
    if gw_ms is not None and modem_ms is None and cdn_ms is None:
        return ("router", "Router reachable — internet blocked",
                "Your router responds but cannot reach the internet. Issue is between router and modem, or modem itself.")

    # DNS failure: names won't resolve but IP connectivity works.
    # Placed before the modem-ok branch so a DNS fault isn't misread as
    # a routing fault. Guarded: needs a working modem path, otherwise the
    # earlier full-outage / router branches already own the verdict.
    if dns_default_ok is False and (dns_explicit_ms is not None or modem_ok):
        return ("isp", "DNS problem detected",
                "Names won't resolve but the internet answers by IP. Try switching DNS to 8.8.8.8 / 1.1.1.1, or check your ISP's DNS status.")

    # Can reach modem/ISP IP but not CDN
    if modem_ms is not None and cdn_ms is None:
        return ("isp", "Modem OK — ISP not routing traffic",
                "Connection reaches your ISP but external sites are unreachable. Contact your provider.")

    # Speed issues with good ping
    if not speed_ok and cdn_ok and gw_ok:
        return ("isp", "Latency OK but speed throttled",
                "Ping is normal but download speed is low. Possible ISP throttling or congestion.")

    # WiFi-specific: gateway lag but modem ok
    if conn_type == "WiFi" and gw_ms is not None and gw_ms > LAG_MS:
        if modem_ok and cdn_ok:
            return ("wifi", "WiFi signal lag detected",
                    "Router and ISP are healthy but WiFi latency is high. Move closer to router or check for interference.")

    # WiFi-specific: weak signal even when latency looks OK
    if conn_type == "WiFi" and wifi_signal is not None and wifi_signal < 40 and (modem_ok or cdn_ok):
        return ("wifi", "Weak WiFi signal",
                "WiFi signal is %d%% — move closer to the router or use a wired connection." % wifi_signal)

    # Router lag: gateway slow, modem and CDN OK
    if gw_ms is not None and gw_ms > LAG_MS * 1.5 and modem_ok:
        return ("router", "Router responding slowly",
                "Gateway latency is elevated. Router may be overloaded — try restarting it.")

    # ISP lag: gateway + modem OK, CDN slow
    if gw_ok and modem_ok and not cdn_ok:
        return ("isp", "ISP routing has high latency",
                "Local network is healthy but CDN latency is high. Issue is in your ISP's network.")

    # Packet loss with otherwise healthy latency
    if loss_pct is not None and loss_pct >= 20 and cdn_ok and gw_ok:
        return ("isp", "Packet loss detected",
                "%.0f%% of packets are being lost. Video calls and games will stutter — report this number to your ISP." % loss_pct)

    # Everything degraded
    if not gw_ok and not modem_ok:
        return ("modem", "Modem/router degraded",
                "Both gateway and external pings are slow. Modem may need a restart.")

    if not speed_ok:
        return ("isp", "Slow speeds",
                "All pings are healthy but download speed is below threshold — likely ISP congestion.")

    return ("ok", "All layers healthy", "No issues detected.")


# ── CSV log ───────────────────────────────────────────────────────────────────

def log_path():
    return _base_dir() / LOG_FILE

def ensure_log():
    p = log_path()
    if not p.exists() or p.stat().st_size == 0:
        with open(p, "w", newline="") as f:
            csv.writer(f).writerow(CSV_HEADERS)
        return
    # Migrate older logs: if the header lacks newer columns (loss_pct,
    # wifi_signal, dns_ms, verdict), rewrite the file with the full header so
    # DictWriter rows stay aligned. Old rows get empty new columns.
    try:
        with open(p, newline="") as f:
            first = f.readline().strip()
        if first != ",".join(CSV_HEADERS):
            with open(p, newline="") as f:
                rows = list(csv.DictReader(f))
            with open(p, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=CSV_HEADERS)
                w.writeheader()
                for r in rows:
                    w.writerow({k: r.get(k, "") for k in CSV_HEADERS})
    except Exception:
        pass

def append_log(row):
    ensure_log()
    with open(log_path(), "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        # Write only known fields, ignore extras
        filtered = {k: row.get(k, "") for k in CSV_HEADERS}
        w.writerow(filtered)

def load_log(since_ts=0):
    ensure_log()
    rows = []
    try:
        with open(log_path(), newline="") as f:
            for r in csv.DictReader(f):
                try:
                    if float(r.get("timestamp", 0)) >= since_ts:
                        rows.append(r)
                except Exception:
                    pass
    except Exception:
        pass
    return rows

def row_to_typed(r):
    def f(k): return float(r[k]) if r.get(k) not in (None,"","None") else None
    return {
        "timestamp": float(r.get("timestamp", 0)),
        "datetime":  r.get("datetime", ""),
        "dl_mbps":   f("dl_mbps"),
        "ping_ms":   f("ping_ms"),
        "jitter_ms": f("jitter_ms"),
        "gw_ms":     f("gw_ms"),
        "modem_ms":  f("modem_ms"),
        "success":   r.get("success","0") in ("1","True","true"),
        "flags":     [x for x in r.get("flags","").split("|") if x],
        "layer":     r.get("layer","unknown"),
        "loss_pct":  f("loss_pct"),
        "wifi_signal": f("wifi_signal"),
        "dns_ms":    f("dns_ms"),
        "verdict":   r.get("verdict",""),
    }

# ── Full test ─────────────────────────────────────────────────────────────────

def run_test(progress_cb=None, deep=False):
    """
    Runs full layered test. progress_cb(step, total, msg) optional.
    deep=True (manual TEST NOW) always traces the route; otherwise the
    route is traced only when the test flags a problem, so healthy
    monitoring cycles stay fast.
    Returns typed result dict.
    """
    def prog(step, msg):
        if progress_cb: progress_cb(step, 7, msg)

    flags   = []
    ping_ms = jitter_ms = dl_mbps = gw_ms = modem_ms = None

    # Step 1: detect connection type & gateway
    prog(1, "Detecting network type…")
    conn_type, conn_name = get_connection_type()
    gateway = get_default_gateway()

    # WiFi signal (Windows only) — the #1 fixable home issue
    wifi_signal = None
    if conn_type == "WiFi":
        wifi_signal = get_wifi_signal()

    # Step 2: ping gateway (router)
    prog(2, "Pinging router/gateway…")
    if gateway:
        gw_pings = []
        for i in range(4):
            r = ping_host(gateway, 80, timeout=3)
            if r is None: r = ping_host(gateway, 443, timeout=3)
            if r is None: r = ping_host(gateway, 53,  timeout=3)
            if r is not None and i > 0:
                gw_pings.append(r)
            time.sleep(0.07)
        gw_ms = statistics.mean(gw_pings) if gw_pings else None

    # Step 3: ping stable external IP (modem/ISP handoff)
    prog(3, "Pinging external IP (modem check)…")
    modem_pings = []
    for ip in [STABLE_IP, STABLE_IP2]:
        r = ping_host(ip, 443, timeout=4)
        if r is None: r = ping_host(ip, 53, timeout=4)
        if r is not None:
            modem_pings.append(r)
        time.sleep(0.05)
    modem_ms = statistics.mean(modem_pings) if modem_pings else None

    # Step 4: CDN latency (ISP routing quality)
    prog(4, "Measuring CDN latency…")
    cdn_pings = []
    for i in range(PING_SAMPLES + 1):
        r = ping_host(CDN_HOST, CDN_PORT, timeout=4)
        if r is not None and i > 0:
            cdn_pings.append(r)
        time.sleep(0.08)
    if cdn_pings:
        ping_ms   = statistics.mean(cdn_pings)
        jitter_ms = statistics.stdev(cdn_pings) if len(cdn_pings) > 1 else 0.0
        if ping_ms   > LAG_MS:    flags.append("lag")
        if jitter_ms > JITTER_MS: flags.append("jitter")
    else:
        flags.append("no_ping")

    # Step 4b: DNS — can names resolve, and how fast does an independent
    # resolver (8.8.8.8) answer? Separates "DNS is broken" from
    # "internet is broken".
    prog(4, "Checking DNS…")
    dns_default_ok, dns_ms = measure_dns()

    # Step 4c: packet loss — 10 quick probes to the CDN host. One lost
    # packet in 10 is noise; 2+ (20%) is a real problem worth flagging.
    prog(4, "Checking packet loss…")
    loss_pct = None
    if cdn_pings:
        fails = 0
        for _ in range(10):
            if ping_host(CDN_HOST, CDN_PORT, timeout=1) is None:
                fails += 1
        loss_pct = fails * 10.0
        if loss_pct >= 20:
            flags.append("loss")
    else:
        loss_pct = 100.0
    if dns_default_ok is False and (dns_ms is not None or modem_ms is not None):
        flags.append("dns")

    # Step 5–6: download speed — time-boxed so fast lines get enough bytes
    # to measure. The old one-pass ~1 MB test finished in <0.1 s at 500 Mbps
    # and mostly measured connection setup, not throughput.
    prog(5, "Measuring download speed…")
    total_bytes = total_secs = 0
    t_start = time.perf_counter()
    cycle = 0
    while time.perf_counter() - t_start < DL_TEST_SECONDS and total_bytes < DL_TEST_MAX_MB * 1e6:
        cycle += 1
        for i, (_, url) in enumerate(DOWNLOAD_TARGETS):
            if time.perf_counter() - t_start >= DL_TEST_SECONDS or total_bytes >= DL_TEST_MAX_MB * 1e6:
                break
            prog(5 + min(cycle - 1, 1) * 0.4, "Downloading test files (pass %d)…" % cycle)
            try:
                req = urllib.request.Request(
                    url + "?_t=" + str(int(time.time()*1000)),
                    headers={"User-Agent":"ISP-Monitor/6","Cache-Control":"no-cache"})
                t0 = time.perf_counter()
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = resp.read()
                total_bytes += len(data)
                total_secs  += time.perf_counter() - t0
            except Exception:
                pass

    if total_secs > 0 and total_bytes > 0:
        dl_mbps = (total_bytes * 8) / total_secs / 1e6
        if dl_mbps < OUTAGE_MBPS: flags.append("outage")
        elif dl_mbps < slow_threshold(): flags.append("slow")
    else:
        flags.append("outage")
        dl_mbps = 0.0

    # Step 7: diagnose
    prog(7, "Diagnosing…")
    success = not any(f in flags for f in ("outage","no_ping"))
    layer, verdict, detail = diagnose_layer(
        gw_ms, modem_ms, ping_ms, dl_mbps, conn_type,
        loss_pct=loss_pct, wifi_signal=wifi_signal,
        dns_default_ok=dns_default_ok, dns_explicit_ms=dns_ms)

    # Step 7b: traceroute when something is wrong (or on a manual deep test).
    # Healthy monitor cycles skip this so the loop stays fast.
    if deep or any(f in flags for f in ("outage","no_ping","lag","slow","loss")):
        prog(7, "Tracing route to find the failing hop…")
        hops = run_traceroute(CDN_HOST)
        if hops:
            trace_layer, trace_detail = diagnose_trace(hops)
            if trace_layer:
                layer, detail = trace_layer, detail + " " + trace_detail
            append_trace_log(hops, layer)

    ts = time.time()
    row = {
        "timestamp": "%.3f" % ts,
        "datetime":  datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
        "dl_mbps":   "%.3f" % dl_mbps   if dl_mbps   is not None else "",
        "ping_ms":   "%.1f" % ping_ms   if ping_ms   is not None else "",
        "jitter_ms": "%.1f" % jitter_ms if jitter_ms is not None else "",
        "gw_ms":     "%.1f" % gw_ms     if gw_ms     is not None else "",
        "modem_ms":  "%.1f" % modem_ms  if modem_ms  is not None else "",
        "success":   "1" if success else "0",
        "flags":     "|".join(flags),
        "layer":     layer,
        "loss_pct":  "%.1f" % loss_pct if loss_pct is not None else "",
        "wifi_signal": "%d" % wifi_signal if wifi_signal is not None else "",
        "dns_ms":    "%.1f" % dns_ms if dns_ms is not None else "",
        "verdict":   verdict,
        # runtime-only (not in CSV)
        "_conn_type": conn_type,
        "_conn_name": conn_name,
        "_gateway":   gateway or "—",
        "_verdict":   verdict,
        "_detail":    detail,
    }
    append_log(row)
    result = row_to_typed(row)
    # Carry runtime extras through
    result["_conn_type"] = conn_type
    result["_conn_name"] = conn_name
    result["_gateway"]   = gateway or "—"
    result["_verdict"]   = verdict
    result["_detail"]    = detail
    result["_wifi_signal"] = wifi_signal
    result["_loss_pct"] = loss_pct
    result["_dns_ms"] = dns_ms
    return result


# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt_speed(mbps):
    if mbps is None: return "—", ""
    if mbps >= 1000: return "%.2f" % (mbps/1000), "Gbps"
    if mbps >= 1:    return "%.1f" % mbps, "Mbps"
    return "%.0f" % (mbps*1000), "Kbps"

def fmt_ms(ms):
    return ("%.0f ms" % ms) if ms is not None else "—"

def severity(flags):
    if not flags: return "good"
    if "outage" in flags or "no_ping" in flags: return "danger"
    if "slow"   in flags or "lag"    in flags:  return "warn"
    if "loss"   in flags or "dns"    in flags:  return "warn"
    return "info"

LAYER_COLOR = {
    "ok":      ACCENT,
    "wifi":    PURPLE,
    "router":  WARN,
    "modem":   WARN,
    "isp":     DANGER,
    "unknown": TEXT2,
}
LAYER_ICON = {
    "ok":      "✓",
    "wifi":    "📶",
    "router":  "🔁",
    "modem":   "📡",
    "isp":     "🌐",
    "unknown": "?",
}

def outage_windows(rows, interval_s=None):
    """Group consecutive failed tests into (start_dt, end_dt, count, minutes).

    A window's length is (last - first) + one interval, so a single failed
    test counts as roughly one interval of downtime.
    """
    if interval_s is None:
        interval_s = interval_seconds()
    windows = []
    cur = None
    for r in rows:
        if not r["success"]:
            if cur is None:
                cur = [r, r, 1]
            else:
                cur[1] = r
                cur[2] += 1
        else:
            if cur is not None:
                windows.append(cur)
                cur = None
    if cur is not None:
        windows.append(cur)
    out = []
    for first, last, count in windows:
        minutes = (last["timestamp"] - first["timestamp"] + interval_s) / 60.0
        out.append((first["datetime"], last["datetime"], count, minutes))
    return out

def slowest_hours(rows, min_tests=3):
    """Per-hour average download, slowest first: (hour, avg_mbps, tests, fail%).

    Hours need >= min_tests tests. Averages exclude outright outages (0 Mbps)
    so the figure means "slow", not "down"; fail% covers the outages.
    """
    by_hour = {}
    for r in rows:
        try:
            h = int(r["datetime"][11:13])
        except Exception:
            continue
        by_hour.setdefault(h, []).append(r)
    out = []
    for h, lst in by_hour.items():
        if len(lst) >= min_tests:
            vals = [x["dl_mbps"] for x in lst
                    if x["dl_mbps"] and x["dl_mbps"] > OUTAGE_MBPS]
            if not vals:
                continue
            out.append((h, statistics.mean(vals), len(lst),
                        sum(1 for x in lst if not x["success"]) / len(lst) * 100))
    out.sort(key=lambda t: t[1])
    return out

def concerns(rows):
    out = []
    if not rows: return out
    total   = len(rows)
    outages = [r for r in rows if "outage" in r["flags"] or "no_ping" in r["flags"]]
    lags    = [r for r in rows if "lag"    in r["flags"]]
    slows   = [r for r in rows if "slow"   in r["flags"]]
    jits    = [r for r in rows if "jitter" in r["flags"]]

    def pct(lst): return len(lst)/total*100

    # Layer-specific concerns
    wifi_issues  = [r for r in rows if r.get("layer") == "wifi"]
    router_issues= [r for r in rows if r.get("layer") == "router"]
    modem_issues = [r for r in rows if r.get("layer") == "modem"]
    isp_issues   = [r for r in rows if r.get("layer") == "isp"]

    if len(wifi_issues)/total > 0.2:
        out.append(("📶 WiFi ISSUES",   "WiFi was the fault in %.1f%% of tests — move closer to router or check interference" % (len(wifi_issues)/total*100), PURPLE))
    if len(router_issues)/total > 0.1:
        out.append(("🔁 ROUTER ISSUES", "Router was the fault in %.1f%% of tests — consider restarting or replacing router" % (len(router_issues)/total*100), WARN))
    if len(modem_issues)/total > 0.1:
        out.append(("📡 MODEM ISSUES",  "Modem layer faults in %.1f%% of tests — may need modem restart or replacement" % (len(modem_issues)/total*100), WARN))
    if len(isp_issues)/total > 0.1:
        out.append(("🌐 ISP ISSUES",    "ISP was the fault in %.1f%% of tests — consider contacting your provider" % (len(isp_issues)/total*100), DANGER))

    if pct(outages) > 5:
        out.append(("OUTAGE",        "%.1f%% outage rate (%d events)" % (pct(outages), len(outages)), DANGER))
    if pct(lags) > 20:
        out.append(("HIGH LATENCY",  "Latency >%dms in %.1f%% of tests" % (LAG_MS, pct(lags)), WARN))
    if pct(slows) > 30:
        out.append(("SLOW SPEED",    "Speed <%.0fMbps in %.1f%% of tests" % (slow_threshold(), pct(slows)), WARN))
    if pct(jits)  > 25:
        out.append(("UNSTABLE",      "High jitter in %.1f%% of tests" % pct(jits), BLUE))
    losses = [r for r in rows if "loss" in r["flags"]]
    if pct(losses) > 25:
        out.append(("PACKET LOSS",   "Packet loss in %.1f%% of tests — video calls and games will stutter" % pct(losses), DANGER))
    dnss = [r for r in rows if "dns" in r["flags"]]
    if pct(dnss) > 10:
        out.append(("DNS FAILURES",  "DNS failed in %.1f%% of tests — try 8.8.8.8 / 1.1.1.1 as your DNS servers" % pct(dnss), WARN))
    sigs = [r["wifi_signal"] for r in rows if r.get("wifi_signal") is not None]
    if len(sigs) >= 4 and statistics.mean(sigs) < 50:
        out.append(("WEAK WIFI SIGNAL", "Avg WiFi signal %.0f%% — move closer to the router or use a wired connection" % statistics.mean(sigs), PURPLE))
    hours = slowest_hours(rows)
    if hours:
        dl_all = [r["dl_mbps"] for r in rows if r["dl_mbps"] and r["dl_mbps"] > OUTAGE_MBPS]
        if dl_all:
            overall = statistics.mean(dl_all)
            h, avg, cnt, _opct = hours[0]
            if avg < overall * 0.5:
                out.append(("SLOW HOURS", "Slowest hour: %02d:00 (avg %.1f Mbps over %d tests) vs overall %.1f Mbps" % (h, avg, cnt, overall), WARN))

    dl_vals = [r["dl_mbps"] for r in rows if r["dl_mbps"] and r["dl_mbps"] > OUTAGE_MBPS]
    if len(dl_vals) >= 4:
        peak = max(dl_vals); avg = statistics.mean(dl_vals)
        if avg < peak * 0.5:
            out.append(("THROTTLING?", "Avg %.1f Mbps vs peak %.1f Mbps — possible ISP throttle" % (avg, peak), WARN))

    streak = mx = 0
    for r in rows:
        if not r["success"]: streak += 1; mx = max(mx, streak)
        else: streak = 0
    if mx >= 3:
        out.append(("EXTENDED OUTAGE", "Longest outage: ~%d min (%d consecutive failures)" % (mx*interval_seconds()//60, mx), DANGER))

    if not out:
        out.append(("ALL CLEAR", "No significant issues detected in this period", ACCENT))
    return out


# ── Text export ───────────────────────────────────────────────────────────────

def export_text(rows, path):
    lines = ["="*72, "  ISP MONITOR REPORT",
             "  Generated : " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "  Log file  : " + str(log_path()), "="*72, ""]
    total   = len(rows)
    ok      = sum(1 for r in rows if r["success"])
    dl_vals = [r["dl_mbps"] for r in rows if r["dl_mbps"] and r["dl_mbps"] > OUTAGE_MBPS]
    pings   = [r["ping_ms"] for r in rows if r["ping_ms"]]
    losses  = [r["loss_pct"] for r in rows if r.get("loss_pct") is not None]
    wins    = outage_windows(rows)
    av,au   = fmt_speed(statistics.mean(dl_vals) if dl_vals else None)
    pv,pu   = fmt_speed(max(dl_vals)             if dl_vals else None)
    lines  += ["SUMMARY", "-"*40,
               "  Total tests   : %d" % total,
               ("  Period        : %s  to  %s" % (rows[0]["datetime"], rows[-1]["datetime"])) if rows else "",
               "  Uptime        : %.1f%%" % (ok/total*100 if total else 0),
               ("  Plan speed    : %.0f Mbps (slow alert below %.0f Mbps)" % (PLAN_MBPS, slow_threshold())) if PLAN_MBPS > 0 else "  Plan speed    : not set (slow alert below %.0f Mbps)" % slow_threshold(),
               "  Avg download  : %s %s" % (av, au),
               "  Peak download : %s %s" % (pv, pu),
               "  Avg latency   : %s" % fmt_ms(statistics.mean(pings) if pings else None),
               "  Avg packet loss : %s" % (("%.1f%%" % statistics.mean(losses)) if losses else "—"),
               "  Total downtime  : ~%.0f min across %d outage(s)" % (sum(w[3] for w in wins), len(wins)) if wins else "  Total downtime  : none recorded",
               "", "LAYER BREAKDOWN", "-"*40]
    for layer in ["wifi","router","modem","isp","ok"]:
        cnt = sum(1 for r in rows if r.get("layer")==layer)
        if cnt:
            lines.append("  %-10s : %d tests (%.1f%%)" % (layer.upper(), cnt, cnt/total*100))
    lines += ["", "CONCERNS", "-"*40]
    for title, desc, _ in concerns(rows):
        lines.append("  [%s] %s" % (title, desc))
    lines += ["", "DOWNTIME (outage windows)", "-"*40]
    if wins:
        for start, end, count, minutes in wins:
            lines.append("  %s  to  %s   ~%.0f min (%d failed tests)" % (start, end, minutes, count))
    else:
        lines.append("  None.")
    lines += ["", "SLOWEST HOURS", "-"*40]
    hours = slowest_hours(rows)
    if hours:
        for h, avg, cnt, opct in hours[:3]:
            av2, au2 = fmt_speed(avg)
            lines.append("  %02d:00  avg %s %s  (%d tests, %.0f%% failed)" % (h, av2, au2, cnt, opct))
    else:
        lines.append("  Not enough data (need 3+ tests in an hour).")
    lines += ["", "FLAGGED EVENTS", "-"*40]
    flagged = [r for r in rows if r["flags"]]
    if flagged:
        for r in flagged:
            dv,du = fmt_speed(r["dl_mbps"])
            line = "  %s  %s %-5s  gw=%-8s modem=%-8s" % (
                r["datetime"], dv, du,
                fmt_ms(r.get("gw_ms")), fmt_ms(r.get("modem_ms")))
            if r.get("loss_pct") is not None:
                line += " loss=%.0f%%" % r["loss_pct"]
            line += "  layer=%-8s  [%s]" % (
                r.get("layer","?").upper(),
                " | ".join(r["flags"]).upper())
            lines.append(line)
    else:
        lines.append("  None.")
    peak_rows = load_peak_log()
    if peak_rows:
        lines += ["", "LINE SPEED TESTS (Speedtest.net, manual)", "-"*40]
        for pr in peak_rows[-20:]:
            dv,du = fmt_speed(float(pr["dl_mbps"]) if pr.get("dl_mbps") else None)
            uv,uu = fmt_speed(float(pr["ul_mbps"]) if pr.get("ul_mbps") else None)
            lines.append("  %s  down %s %s  up %s %s" % (pr.get("datetime",""), dv, du, uv, uu))
    lines += ["", "FULL LOG", "-"*40,
              "  %-20s %-10s %-10s %-10s %-10s %-7s %-8s %s" % (
                  "Timestamp","Download","Latency","GW ping","Modem","Loss","Layer","Flags")]
    for r in rows:
        dv,du = fmt_speed(r["dl_mbps"])
        lines.append("  %-20s %-10s %-10s %-10s %-10s %-7s %-8s %s" % (
            r["datetime"], dv+" "+du,
            fmt_ms(r["ping_ms"]), fmt_ms(r.get("gw_ms")), fmt_ms(r.get("modem_ms")),
            ("%.0f%%" % r["loss_pct"]) if r.get("loss_pct") is not None else "—",
            r.get("layer","?").upper(),
            " | ".join(r["flags"]).upper() if r["flags"] else "OK"))
    lines += ["", "="*72]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ── Windows autostart (v6, Run key) ─────────────────────────────────────────

def _autostart_enabled():
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
            winreg.QueryValueEx(k, "ISP Monitor")
        return True
    except Exception:
        return False

def _autostart_target():
    if getattr(sys, "frozen", False):
        return '"%s"' % sys.executable
    return '"%s" "%s"' % (sys.executable, str(Path(__file__).resolve()))

def set_autostart(enable):
    """Windows Run-key autostart. Returns True on success, False otherwise."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        if enable:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, "ISP Monitor", 0, winreg.REG_SZ,
                                  _autostart_target())
        else:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                                winreg.KEY_SET_VALUE) as k:
                try:
                    winreg.DeleteValue(k, "ISP Monitor")
                except FileNotFoundError:
                    pass
        return True
    except Exception:
        return False

def _parse_geometry(s):
    """'1100x700+100+100' -> (1100, 700, 100, 100), else None."""
    m = re.match(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", s or "")
    return tuple(int(g) for g in m.groups()) if m else None


# ── App ───────────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        ensure_log()
        self.title("ISP Monitor")
        # Restore the last window size/position (v6), clamped to the current
        # screen so a changed monitor setup can't strand the window offscreen.
        # Otherwise fit the screen: the old fixed 860px height overflowed
        # smaller displays, and with no scrollbar the bottom was unreachable.
        _saved = _parse_geometry(_CONFIG.get("geometry", ""))
        if _saved:
            _w, _h, _x, _y = _saved
            _sw, _sh = self.winfo_screenwidth(), self.winfo_screenheight()
            _w = min(max(_w, 900), _sw); _h = min(max(_h, 500), _sh - 60)
            _x = min(max(_x, 0), max(_sw - 200, 0)); _y = min(max(_y, 0), max(_sh - 100, 0))
            self.geometry("%dx%d+%d+%d" % (_w, _h, _x, _y))
        else:
            self.geometry("1100x%d" % min(860, self.winfo_screenheight() - 60))
        self.minsize(900, 500)
        self.configure(bg=BG)
        self._active  = False
        self._testing = False
        self._filter  = tk.IntVar(value=1)
        self._next_ts = 0
        self._epills  = []
        self._last_result = None
        self._busy = False
        self._spin_after = None
        self._spin_idx = 0
        self._was_outage = False
        self._tree_rows = []
        self._sort_col = None
        self._sort_rev = False
        self._layer_hints = {}
        self.hint_frame = None
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        # Remember window size/position for the next launch (v6).
        try:
            update_config_value("geometry", self.geometry())
        except Exception:
            pass
        self.destroy()

    def _build(self):
        # ── Scrollable main area (v5.1) ─────────────────────────────────────
        # The old layout packed a ~1200px column straight into the window
        # with no scrollbar, so on smaller screens the bottom (chart, log,
        # status bar) was unreachable. Everything now lives in a canvas.
        self._canvas = tk.Canvas(self, bg=BG, highlightthickness=0)
        _vsb = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=_vsb.set)
        _vsb.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)
        self._main = tk.Frame(self._canvas, bg=BG)
        self._cwin = self._canvas.create_window((0, 0), window=self._main, anchor="nw")
        self._canvas.bind("<Configure>",
            lambda e: self._canvas.itemconfig(self._cwin, width=e.width))
        self._main.bind("<Configure>",
            lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))
        def _on_main_wheel(e):
            # Wheel over the log table scrolls the table (its own binding);
            # everywhere else it scrolls the page.
            tree = getattr(self, "tree", None)
            if tree is not None and e.widget == tree:
                return
            self._canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        def _on_main_wheel_linux(e, direction):
            tree = getattr(self, "tree", None)
            if tree is not None and e.widget == tree:
                return
            self._canvas.yview_scroll(direction, "units")
        # Toplevel bindings fire for every descendant (bindtags chain), so
        # one binding covers the whole page without bind_all side effects.
        self.bind("<MouseWheel>", _on_main_wheel)
        self.bind("<Button-4>", lambda e: _on_main_wheel_linux(e, -1))
        self.bind("<Button-5>", lambda e: _on_main_wheel_linux(e, 1))

        # ── Top bar ───────────────────────────────────────────────────────────
        tb = tk.Frame(self._main, bg=SURFACE); tb.pack(fill="x")
        ti = tk.Frame(tb, bg=SURFACE);  ti.pack(fill="x", padx=18, pady=10)
        tk.Label(ti, text="ISP MONITOR", bg=SURFACE, fg=ACCENT,
                 font=(FNT,13,"bold")).pack(side="left")
        tk.Label(ti, text="  speed & reliability tracker  ·  v6.1",
                 bg=SURFACE, fg=TEXT2, font=(FNT2,9)).pack(side="left")
        tk.Label(ti, text="  |  created by SBB",
                 bg=SURFACE, fg=TEXT3, font=(FNT2,9)).pack(side="left")
        r = tk.Frame(ti, bg=SURFACE); r.pack(side="right")
        self.lbl_busy = tk.Label(r, text="", bg=SURFACE, fg=ACCENT, font=(FNT2,9,"bold"))
        self.lbl_busy.pack(side="left", padx=(0,12))
        # Outage alert (v6): set on an outage result, cleared on recovery.
        self.lbl_alert = tk.Label(r, text="", bg=SURFACE, fg=DANGER, font=(FNT2,9,"bold"))
        self.lbl_alert.pack(side="left", padx=(0,12))
        self.lbl_next = tk.Label(r, text="", bg=SURFACE, fg=TEXT3, font=(FNT,8))
        self.lbl_next.pack(side="left", padx=(0,12))
        self.btn_mon = tk.Button(r, text="▶  START MONITORING",
            command=self._toggle_mon, bg=ACCENT, fg=BG,
            font=(FNT,9,"bold"), relief="flat", padx=12, pady=4, cursor="hand2")
        self.btn_mon.pack(side="left", padx=(0,6))
        self.btn_test = tk.Button(r, text="⚡ TEST NOW", command=self._manual_test,
            bg=CARD2, fg=TEXT, font=(FNT,9,"bold"),
            relief="flat", padx=12, pady=4, cursor="hand2")
        self.btn_test.pack(side="left")
        self.btn_peak = tk.Button(r, text="📶 LINE SPEED", command=self._run_peak_test,
            bg=CARD2, fg=BLUE, font=(FNT,9,"bold"),
            relief="flat", padx=12, pady=4, cursor="hand2")
        self.btn_peak.pack(side="left", padx=(6,0))
        tk.Button(r, text="? HELP", command=self._show_help,
            bg=CARD2, fg=TEXT2, font=(FNT,9),
            relief="flat", padx=10, pady=4, cursor="hand2").pack(side="left", padx=(6,0))

        # ── Top verdict strip (v6): latest verdict visible without scrolling ─
        self.verdict_strip = tk.Frame(self._main, bg=CARD); self.verdict_strip.pack(fill="x")
        self.lbl_top_verdict = tk.Label(self.verdict_strip,
            text="Run a test or start monitoring to see your verdict here.",
            bg=CARD, fg=TEXT3, font=(FNT2,9,"bold"), anchor="w")
        self.lbl_top_verdict.pack(fill="x", padx=18, pady=6)

        # ── Filter + export bar ───────────────────────────────────────────────
        fb = tk.Frame(self._main, bg=CARD); fb.pack(fill="x")
        fi = tk.Frame(fb, bg=CARD);  fi.pack(fill="x", padx=18, pady=7)
        tk.Label(fi, text="SHOW:", bg=CARD, fg=TEXT2, font=(FNT,8)).pack(side="left")
        self._fbts = {}
        for d, lbl in [(1,"24 H"),(3,"3 D"),(7,"7 D"),(14,"14 D"),(30,"30 D"),(0,"ALL")]:
            b = tk.Button(fi, text=lbl, command=lambda d=d: self._set_filter(d),
                bg=CARD, fg=TEXT2, font=(FNT,8), relief="flat",
                padx=10, pady=3, cursor="hand2", bd=0)
            b.pack(side="left", padx=2)
            self._fbts[d] = b
        exp = tk.Frame(fi, bg=CARD); exp.pack(side="right")
        tk.Label(exp, text="SAVE:", bg=CARD, fg=TEXT3, font=(FNT,8)).pack(side="left", padx=(0,6))
        for lbl, cmd in [("📄 Text report", self._exp_text),
                         ("📂 Open CSV log", self._open_csv),
                         ("📋 Copy ISP summary", self._copy_summary)]:
            tk.Button(exp, text=lbl, command=cmd, bg=CARD2, fg=TEXT2,
                font=(FNT,8), relief="flat", padx=10, pady=3,
                cursor="hand2", bd=0).pack(side="left", padx=3)

        # ── Settings (v5): plan speed, slow alert %, check interval ──────────
        st = tk.Frame(self._main, bg=CARD); st.pack(fill="x")
        si = tk.Frame(st, bg=CARD);  si.pack(fill="x", padx=18, pady=(0,7))
        tk.Label(si, text="PLAN:", bg=CARD, fg=TEXT2, font=(FNT,8)).pack(side="left")
        self.ent_plan = tk.Entry(si, bg=CARD2, fg=TEXT, font=(FNT,8), width=8,
                                 insertbackground=TEXT)
        self.ent_plan.insert(0, "%d" % PLAN_MBPS if PLAN_MBPS else "0")
        self.ent_plan.pack(side="left", padx=(4,2))
        tk.Label(si, text="Mbps (0 = not set)", bg=CARD, fg=TEXT3, font=(FNT,8)).pack(side="left", padx=(0,12))
        tk.Label(si, text="SLOW ALERT BELOW:", bg=CARD, fg=TEXT2, font=(FNT,8)).pack(side="left")
        self.ent_pct = tk.Entry(si, bg=CARD2, fg=TEXT, font=(FNT,8), width=5,
                                insertbackground=TEXT)
        self.ent_pct.insert(0, "%d" % SLOW_PCT)
        self.ent_pct.pack(side="left", padx=(4,2))
        tk.Label(si, text="% of plan", bg=CARD, fg=TEXT3, font=(FNT,8)).pack(side="left", padx=(0,12))
        tk.Label(si, text="CHECK EVERY:", bg=CARD, fg=TEXT2, font=(FNT,8)).pack(side="left")
        self.ent_int = tk.Entry(si, bg=CARD2, fg=TEXT, font=(FNT,8), width=5,
                                insertbackground=TEXT)
        self.ent_int.insert(0, "%d" % INTERVAL_MIN)
        self.ent_int.pack(side="left", padx=(4,2))
        tk.Label(si, text="min", bg=CARD, fg=TEXT3, font=(FNT,8)).pack(side="left", padx=(0,12))
        tk.Button(si, text="💾 SAVE SETTINGS", command=self._save_settings,
            bg=CARD2, fg=ACCENT, font=(FNT,8,"bold"), relief="flat",
            padx=10, pady=3, cursor="hand2", bd=0).pack(side="left")
        self.lbl_settings = tk.Label(si, text="", bg=CARD, fg=TEXT3, font=(FNT,8))
        self.lbl_settings.pack(side="left", padx=10)

        # ── Settings row 2 (v6): theme, outage sound, autostart ───────────────
        si2 = tk.Frame(st, bg=CARD); si2.pack(fill="x", padx=18, pady=(0,7))
        tk.Label(si2, text="THEME:", bg=CARD, fg=TEXT2, font=(FNT,8)).pack(side="left")
        self._theme_var = tk.StringVar(value="Light" if _CONFIG.get("theme") == "light" else "Dark")
        theme_menu = tk.OptionMenu(si2, self._theme_var, "Dark", "Light",
                                   command=self._on_theme_change)
        theme_menu.configure(bg=CARD2, fg=TEXT, font=(FNT,8), relief="flat",
                             bd=0, highlightthickness=0)
        theme_menu.pack(side="left", padx=(4,12))
        self._sound_var = tk.IntVar(value=SOUND_ON_OUTAGE)
        tk.Checkbutton(si2, text="🔔 Sound on outage", variable=self._sound_var,
            command=self._on_sound_change, bg=CARD, fg=TEXT2, font=(FNT,8),
            selectcolor=CARD2, activebackground=CARD).pack(side="left", padx=(0,12))
        self._auto_var = tk.IntVar(value=1 if _autostart_enabled() else 0)
        auto_cb = tk.Checkbutton(si2, text="▶ Start with Windows", variable=self._auto_var,
            command=self._on_autostart_change, bg=CARD, fg=TEXT2, font=(FNT,8),
            selectcolor=CARD2, activebackground=CARD)
        if sys.platform != "win32":
            auto_cb.configure(state="disabled")
        auto_cb.pack(side="left")
        tk.Label(si2, text="Theme applies on restart", bg=CARD, fg=TEXT3,
                 font=(FNT,8)).pack(side="left", padx=12)

        # ── First-run hint (v6): shown until dismissed, only when no data yet ─
        if not _CONFIG.get("hint_dismissed") and not load_log(0):
            hf = tk.Frame(self._main, bg=CARD); hf.pack(fill="x", padx=14, pady=(10,0))
            tk.Label(hf, text="👋 New here?  1) Set your plan speed above  2) Hit ⚡ TEST NOW  3) Hit ▶ START MONITORING to build ISP evidence.",
                bg=CARD, fg=TEXT2, font=(FNT2,9)).pack(side="left", padx=12, pady=6)
            tk.Button(hf, text="✕", command=self._dismiss_hint, bg=CARD2, fg=TEXT3,
                font=(FNT,8), relief="flat", padx=8, pady=2, cursor="hand2", bd=0).pack(side="right", padx=8)
            self.hint_frame = hf

        # ── Metric cards ──────────────────────────────────────────────────────
        mf = tk.Frame(self._main, bg=BG); mf.pack(fill="x", padx=14, pady=(12,0))
        for i in range(5): mf.columnconfigure(i, weight=1)
        self._mvars = []
        for i, n in enumerate(["Avg Download","Peak Download","Avg Latency","Uptime","Tests Run"]):
            c = tk.Frame(mf, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
            c.grid(row=0, column=i, sticky="ew", padx=(0 if i==0 else 6, 0))
            tk.Label(c, text=n.upper(), bg=CARD, fg=TEXT3, font=(FNT,7)).pack(pady=(8,0))
            vl = tk.Label(c, text="—", bg=CARD, fg=TEXT, font=(FNT,18,"bold")); vl.pack()
            ul = tk.Label(c, text="",  bg=CARD, fg=TEXT2, font=(FNT,8));         ul.pack(pady=(0,8))
            self._mvars.append((vl, ul))

        # ── Peak vs Real-World speed card ─────────────────────────────────────
        sf = tk.Frame(self._main, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        sf.pack(fill="x", padx=14, pady=(10,0))
        sh = tk.Frame(sf, bg=CARD); sh.pack(fill="x", padx=12, pady=(8,6))
        tk.Label(sh, text="YOUR LINE CAPACITY  —  what your speed can actually do",
                 bg=CARD, fg=TEXT2, font=(FNT,8,"bold")).pack(side="left")
        self.lbl_peak_run = tk.Label(sh, text="Click  📶 LINE SPEED  to measure max capacity",
                 bg=CARD, fg=TEXT3, font=(FNT2,8))
        self.lbl_peak_run.pack(side="right")

        # Top row: LINE SPEED big number + CDN speed
        top_row = tk.Frame(sf, bg=CARD); top_row.pack(fill="x", padx=10, pady=(0,6))
        top_row.columnconfigure(0, weight=2)
        top_row.columnconfigure(1, weight=3)

        # Left: Line speed
        pk = tk.Frame(top_row, bg=CARD2, highlightbackground=BLUE, highlightthickness=1)
        pk.grid(row=0, column=0, sticky="nsew", padx=(0,6))
        tk.Label(pk, text="📶  LINE CAPACITY", bg=CARD2, fg=BLUE,
                 font=(FNT,8,"bold")).pack(pady=(8,2))
        tk.Label(pk, text="What Speedtest.net measures", bg=CARD2, fg=TEXT3,
                 font=(FNT2,7)).pack()
        self.lbl_peak_val  = tk.Label(pk, text="—", bg=CARD2, fg=BLUE,
                                       font=(FNT,28,"bold")); self.lbl_peak_val.pack(pady=(4,0))
        self.lbl_peak_unit = tk.Label(pk, text="run LINE SPEED to measure", bg=CARD2, fg=TEXT2,
                                       font=(FNT,8));          self.lbl_peak_unit.pack(pady=(0,10))

        # Right: Usage scenarios
        self._scenario_frame = tk.Frame(top_row, bg=CARD); self._scenario_frame.grid(row=0, column=1, sticky="nsew")
        self._scenario_frame.columnconfigure(0, weight=1)
        self._scenario_frame.columnconfigure(1, weight=1)
        self._scenario_frame.columnconfigure(2, weight=1)

        # Placeholder text until line speed is measured
        self._scenario_placeholder = tk.Label(self._scenario_frame,
            text="Run  📶 LINE SPEED  to see what your connection can actually handle",
            bg=CARD, fg=TEXT3, font=(FNT2,9), wraplength=400)
        self._scenario_placeholder.pack(expand=True, pady=20)

        # Explanation / gap message
        self.lbl_gap_explain = tk.Label(sf,
            text="LINE SPEED = raw pipe capacity  |  Actual usable speed depends on what you're doing — see scenarios above.",
            bg=CARD, fg=TEXT3, font=(FNT2,8), wraplength=1000, justify="left", anchor="w")
        self.lbl_gap_explain.pack(fill="x", padx=12, pady=(0,8))

        # ── Network layer diagram ─────────────────────────────────────────────
        nf = tk.Frame(self._main, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        nf.pack(fill="x", padx=14, pady=(12,0))
        nh = tk.Frame(nf, bg=CARD); nh.pack(fill="x", padx=12, pady=(8,6))
        tk.Label(nh, text="NETWORK LAYERS", bg=CARD, fg=TEXT2, font=(FNT,8,"bold")).pack(side="left")
        tk.Label(nh, text="  shows where the issue is on each test",
                 bg=CARD, fg=TEXT3, font=(FNT2,8)).pack(side="left")
        self.lbl_conntype = tk.Label(nh, text="", bg=CARD, fg=TEXT2, font=(FNT,8))
        self.lbl_conntype.pack(side="right")

        layers_row = tk.Frame(nf, bg=CARD); layers_row.pack(fill="x", padx=12, pady=(0,10))
        layers_row.columnconfigure(0, weight=1)
        layers_row.columnconfigure(2, weight=1)
        layers_row.columnconfigure(4, weight=1)
        layers_row.columnconfigure(6, weight=1)

        self._layer_cards = {}
        # Thresholds per card: (excellent_ms, good_ms, warn_ms)
        # Below excellent = green EXCELLENT, below good = green GOOD,
        # below warn = amber FAIR, above = red SLOW
        THRESHOLDS = {
            "wifi":   (5,   20,  50),
            "router": (5,   20,  50),
            "modem":  (20,  60, 120),
            "isp":    (30,  80, 150),
        }
        layer_defs = [
            ("wifi",   "📶",  "WiFi / Cable",   "Signal to router",     "< 5ms = Excellent"),
            ("router", "🔁",  "Router",         "Local gateway",        "< 20ms = Good"),
            ("modem",  "📡",  "Modem / ISP",    "External IP reach",    "< 60ms = Good"),
            ("isp",    "🌐",  "Internet / ISP", "CDN & download speed", "< 80ms = Good"),
        ]
        arrow_col = TEXT3
        for col, (key, icon, title, sub, thresh_hint) in enumerate(layer_defs):
            if col > 0:
                tk.Label(layers_row, text="→", bg=CARD, fg=arrow_col,
                         font=(FNT,14)).grid(row=0, column=col*2-1, padx=2)

            card = tk.Frame(layers_row, bg=CARD2,
                             highlightbackground=BORDER, highlightthickness=1)
            card.grid(row=0, column=col*2, sticky="ew", padx=2)

            # Icon + title
            tk.Label(card, text=icon, bg=CARD2, font=(FNT2,15)).pack(pady=(8,1))
            lbl_title = tk.Label(card, text=title, bg=CARD2, fg=TEXT,
                                  font=(FNT,8,"bold")); lbl_title.pack()
            lbl_sub   = tk.Label(card, text=sub, bg=CARD2, fg=TEXT3,
                                  font=(FNT2,7)); lbl_sub.pack()

            # Health bar (canvas)
            bar_frame = tk.Frame(card, bg=CARD2); bar_frame.pack(fill="x", padx=10, pady=(6,2))
            bar_bg    = tk.Canvas(bar_frame, bg=BORDER, height=5,
                                   highlightthickness=0, bd=0)
            bar_bg.pack(fill="x")
            bar_fill  = bar_bg.create_rectangle(0, 0, 0, 5, fill=ACCENT, outline="")

            # Ping value
            lbl_ping  = tk.Label(card, text="—", bg=CARD2, fg=TEXT2,
                                  font=(FNT,11,"bold")); lbl_ping.pack(pady=(2,0))

            # Rating label
            lbl_rating= tk.Label(card, text="WAITING", bg=CARD2, fg=TEXT3,
                                  font=(FNT2,8,"bold")); lbl_rating.pack()

            # Threshold hint
            lbl_hint  = tk.Label(card, text=thresh_hint, bg=CARD2, fg=TEXT3,
                                  font=(FNT2,7)); lbl_hint.pack(pady=(0,8))

            self._layer_hints[key] = thresh_hint
            self._layer_cards[key] = (card, lbl_title, lbl_ping, lbl_rating, bar_bg, bar_fill, lbl_hint, THRESHOLDS[key])

        # Verdict bar
        self.verdict_frame = tk.Frame(nf, bg=CARD); self.verdict_frame.pack(fill="x", padx=12, pady=(0,10))
        self.lbl_verdict = tk.Label(self.verdict_frame,
            text="Run a test to diagnose your connection",
            bg=CARD, fg=TEXT3, font=(FNT2,10), anchor="w")
        self.lbl_verdict.pack(fill="x")
        self.lbl_detail = tk.Label(self.verdict_frame, text="",
            bg=CARD, fg=TEXT2, font=(FNT2,9), anchor="w", wraplength=900)
        self.lbl_detail.pack(fill="x")

        # What each card measures — always visible explanation
        explain_row = tk.Frame(self.verdict_frame, bg=CARD)
        explain_row.pack(fill="x", pady=(6,0))
        self.lbl_layer_explain = tk.Label(explain_row,
            text="Each card measures an independent hop:  "
                 "📶 PC→Router  |  🔁 Router health  |  📡 Router→Internet  |  🌐 ISP routing & speed  "
                 "—  each has different thresholds so ratings are not directly comparable",
            bg=CARD, fg=TEXT3, font=(FNT2,8), anchor="w", wraplength=1000)
        self.lbl_layer_explain.pack(fill="x")

        # ── Live event ticker ─────────────────────────────────────────────────
        ef = tk.Frame(self._main, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        ef.pack(fill="x", padx=14, pady=(10,0))
        eh = tk.Frame(ef, bg=CARD); eh.pack(fill="x", padx=12, pady=(7,4))
        tk.Label(eh, text="LIVE EVENTS", bg=CARD, fg=TEXT2, font=(FNT,8,"bold")).pack(side="left")
        tk.Label(eh, text="  each pill = one test  |  colour = fault layer",
                 bg=CARD, fg=TEXT3, font=(FNT2,8)).pack(side="left")
        self.lbl_lv = tk.Label(eh, text="● IDLE", bg=CARD, fg=TEXT3, font=(FNT,8))
        self.lbl_lv.pack(side="right")
        self.evt_row = tk.Frame(ef, bg=CARD); self.evt_row.pack(fill="x", padx=10, pady=(0,8))
        self.lbl_noevt = tk.Label(self.evt_row,
            text="No events yet — run a test or start monitoring",
            bg=CARD, fg=TEXT3, font=(FNT2,9))
        self.lbl_noevt.pack(anchor="w", pady=4)

        # ── Concerns ──────────────────────────────────────────────────────────
        self.cf = tk.Frame(self._main, bg=BG); self.cf.pack(fill="x", padx=14, pady=(10,0))

        # ── Chart ─────────────────────────────────────────────────────────────
        cc = tk.Frame(self._main, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        cc.pack(fill="x", padx=14, pady=(10,0))
        ch = tk.Frame(cc, bg=CARD); ch.pack(fill="x", padx=12, pady=(8,2))
        tk.Label(ch, text="DOWNLOAD SPEED  (Mbps)", bg=CARD, fg=TEXT2, font=(FNT,8)).pack(side="left")
        self.lbl_cr = tk.Label(ch, text="", bg=CARD, fg=TEXT3, font=(FNT,8)); self.lbl_cr.pack(side="right")
        self.chart = tk.Canvas(cc, bg=CARD, height=120, highlightthickness=0)
        self.chart.pack(fill="x", padx=8, pady=(0,8))

        # ── Log table ─────────────────────────────────────────────────────────
        lo = tk.Frame(self._main, bg=BG); lo.pack(fill="both", expand=True, padx=14, pady=(10,14))
        lh = tk.Frame(lo, bg=BG);  lh.pack(fill="x", pady=(0,4))
        tk.Label(lh, text="TEST LOG", bg=BG, fg=TEXT2, font=(FNT,8)).pack(side="left")
        self.lbl_lc = tk.Label(lh, text="", bg=BG, fg=TEXT3, font=(FNT,8)); self.lbl_lc.pack(side="right")

        cols   = ("time","dl","gw","modem","cdn","layer","flags")
        hdrs   = ("Timestamp","Download","Router ms","Modem ms","CDN ms","Layer","Issues")
        widths = (162, 100, 90, 90, 90, 90, 210)
        style  = ttk.Style(); style.theme_use("default")
        style.configure("M.Treeview", background=CARD, foreground=TEXT,
            fieldbackground=CARD, rowheight=23, font=(FNT,9))
        style.configure("M.Treeview.Heading", background=SURFACE, foreground=TEXT2,
            font=(FNT,8,"bold"), relief="flat")
        style.map("M.Treeview",
            background=[("selected",SEL_BG)], foreground=[("selected",TEXT)])

        tf = tk.Frame(lo, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        tf.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tf, columns=cols, show="headings", style="M.Treeview")
        for c, h, w in zip(cols, hdrs, widths):
            self.tree.heading(c, text=h, command=lambda c=c: self._sort_tree(c))
            self.tree.column(c, width=w, anchor="w" if c in ("time","flags") else "center")
        vsb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.tag_configure("good",   foreground=ACCENT,  background=GOOD_BG)
        self.tree.tag_configure("warn",   foreground=WARN,    background=WARN_BG)
        self.tree.tag_configure("danger", foreground=DANGER,  background=BAD_BG)
        self.tree.tag_configure("info",   foreground=BLUE,    background=CARD)
        self.tree.tag_configure("purple", foreground=PURPLE,  background=INFO_BG)
        self.tree.tag_configure("norm",   foreground=TEXT,    background=CARD)

        # ── Progress bar (hidden until test runs) ─────────────────────────────
        self.prog_frame = tk.Frame(self._main, bg=SURFACE); # packed dynamically
        self.prog_lbl = tk.Label(self.prog_frame, text="", bg=SURFACE, fg=ACCENT,
                                  font=(FNT,8), anchor="w")
        self.prog_lbl.pack(side="left", padx=14, pady=4)
        self.prog_bar = ttk.Progressbar(self.prog_frame, length=300,
                                         mode="determinate", maximum=7)
        style.configure("P.Horizontal.TProgressbar",
                         troughcolor=CARD, background=ACCENT, thickness=4)
        self.prog_bar.configure(style="P.Horizontal.TProgressbar")
        self.prog_bar.pack(side="left", padx=8, pady=6)

        # ── Status bar ────────────────────────────────────────────────────────
        sb = tk.Frame(self._main, bg=SURFACE); sb.pack(fill="x", side="bottom")
        self.lbl_sb = tk.Label(sb, text="Log: %s" % log_path(),
            bg=SURFACE, fg=TEXT2, font=(FNT,8), anchor="w")
        self.lbl_sb.pack(side="left", padx=14, pady=5)
        self.lbl_live2 = tk.Label(sb, text="", bg=SURFACE, fg=ACCENT, font=(FNT,8,"bold"))
        self.lbl_live2.pack(side="right", padx=14)

        self._start_countdown()
        self._set_filter(1)  # must be last — all widgets must exist first

    # ── Layer card updater ────────────────────────────────────────────────────

    def _update_layer_cards(self, result):
        """Update the 4 layer cards with health bars and plain-English ratings."""
        gw_ms    = result.get("gw_ms")
        modem_ms = result.get("modem_ms")
        ping_ms  = result.get("ping_ms")
        dl_mbps  = result.get("dl_mbps")
        layer    = result.get("layer", "unknown")
        conn     = result.get("_conn_type", "Unknown")
        name     = result.get("_conn_name", "")
        verdict  = result.get("_verdict", "")
        detail   = result.get("_detail", "")

        conn_str = "%s%s" % (conn, ("  (%s)" % name) if name else "")
        self.lbl_conntype.configure(text=conn_str, fg=PURPLE if conn=="WiFi" else ACCENT)

        def rate(ms, thresholds):
            """Return (rating_text, colour, bar_fraction) based on ms and thresholds."""
            exc, good, warn = thresholds
            if ms is None:
                return "NO RESPONSE", DANGER, 1.0
            if ms <= exc:
                return "EXCELLENT  (%.0f ms)" % ms, ACCENT, ms / exc * 0.25
            if ms <= good:
                return "GOOD  (%.0f ms)" % ms, ACCENT, 0.25 + (ms-exc)/(good-exc) * 0.25
            if ms <= warn:
                return "FAIR  (%.0f ms)" % ms, WARN, 0.5 + (ms-good)/(warn-good) * 0.25
            return "SLOW  (%.0f ms)" % ms, DANGER, min(1.0, 0.75 + (ms-warn)/warn * 0.25)

        is_fault = {
            "wifi":   layer == "wifi",
            "router": layer == "router",
            "modem":  layer == "modem",
            "isp":    layer == "isp",
        }

        card_vals = {
            "wifi":   gw_ms,
            "router": gw_ms,
            "modem":  modem_ms,
            "isp":    ping_ms,
        }

        for key, (card, lbl_t, lbl_p, lbl_r, bar_bg, bar_fill, lbl_hint, thresholds) \
                in self._layer_cards.items():

            fault = is_fault[key]
            ms    = card_vals[key]
            rating_txt, rating_col, bar_frac = rate(ms, thresholds)

            if fault:
                bg         = BAD_BG
                border_col = DANGER
                rating_txt = "⛔  ISSUE FOUND"
                rating_col = DANGER
                bar_frac   = 1.0
            else:
                bg         = GOOD_BG if rating_col == ACCENT else WARN_BG if rating_col == WARN else BAD_BG
                border_col = rating_col

            # Update card background
            card.configure(bg=bg, highlightbackground=border_col)
            for w in card.winfo_children():
                try: w.configure(bg=bg)
                except Exception: pass

            # Ping / speed value
            if key == "isp" and dl_mbps is not None:
                dv, du = fmt_speed(dl_mbps)
                lbl_p.configure(text="%s %s" % (dv, du), fg=rating_col, bg=bg)
            else:
                lbl_p.configure(text=fmt_ms(ms), fg=rating_col, bg=bg)

            lbl_r.configure(text=rating_txt, fg=rating_col, bg=bg)

            # Health bar
            bar_bg.update_idletasks()
            W = bar_bg.winfo_width() or 100
            bar_bg.configure(bg=BORDER)
            bar_bg.coords(bar_fill, 0, 0, int(W * bar_frac), 5)
            bar_bg.itemconfig(bar_fill, fill=rating_col)

        # WiFi signal on the WiFi card's hint line (v6)
        wifi_card = self._layer_cards.get("wifi")
        if wifi_card is not None:
            _c, _t, _p, _r, _bb, _bf, lbl_hint, _th = wifi_card
            sig = result.get("_wifi_signal")
            if conn == "WiFi" and sig is not None:
                lbl_hint.configure(text="Signal %d%%" % sig, fg=TEXT2)
            else:
                lbl_hint.configure(text=self._layer_hints.get("wifi", ""), fg=TEXT3)

        col  = LAYER_COLOR.get(layer, TEXT2)
        icon = LAYER_ICON.get(layer, "?")
        self.lbl_verdict.configure(text="%s  %s" % (icon, verdict), fg=col)
        self.lbl_detail.configure(text=detail, fg=TEXT2)

    def _update_top_verdict(self, result):
        """One-line verdict at the very top of the window (v6)."""
        if not result:
            return
        layer = result.get("layer", "unknown")
        # Live results carry _verdict; logged rows carry the verdict column
        # (v6.1); older rows have neither, so fall back to layer + flags.
        verdict = result.get("_verdict") or result.get("verdict") or ""
        if not verdict:
            if layer == "ok":
                verdict = "All layers healthy"
            elif result["flags"]:
                verdict = "%s issue (%s)" % (layer.upper(), ", ".join(f.upper() for f in result["flags"]))
            else:
                verdict = "%s issue" % layer.upper()
        dv, du = fmt_speed(result["dl_mbps"])
        text = "%s  %s — %s %s" % (LAYER_ICON.get(layer, "?"), verdict, dv, du)
        loss = result.get("_loss_pct", result.get("loss_pct"))
        if loss is not None and loss >= 20:
            text += "  ·  packet loss %.0f%%" % loss
        # wifi_signal is only ever recorded on WiFi, live or logged, so no
        # connection-type check is needed here.
        sig = result.get("_wifi_signal", result.get("wifi_signal"))
        if sig is not None:
            text += "  ·  WiFi signal %d%%" % sig
        sev = severity(result["flags"])
        bg = GOOD_BG if sev == "good" else WARN_BG if sev == "warn" else BAD_BG if sev == "danger" else CARD
        col = LAYER_COLOR.get(layer, TEXT2)
        self.lbl_top_verdict.configure(text=text, fg=col, bg=bg)
        self.verdict_strip.configure(bg=bg)

    def _reset_layer_cards(self):
        for key, (card, lbl_t, lbl_p, lbl_r, bar_bg, bar_fill, lbl_hint, thresholds) \
                in self._layer_cards.items():
            card.configure(bg=CARD2, highlightbackground=BORDER)
            for w in card.winfo_children():
                try: w.configure(bg=CARD2)
                except Exception: pass
            lbl_p.configure(text="—",        fg=TEXT2,  bg=CARD2)
            lbl_r.configure(text="TESTING…", fg=TEXT3,  bg=CARD2)
            bar_bg.itemconfig(bar_fill, fill=BORDER)
            bar_bg.coords(bar_fill, 0, 0, 0, 5)

    # ── Event pills ───────────────────────────────────────────────────────────

    def _push_event(self, result):
        flags  = result["flags"]
        layer  = result.get("layer", "unknown")
        ts     = result["datetime"][11:19]
        dv, du = fmt_speed(result["dl_mbps"])
        col    = LAYER_COLOR.get(layer, TEXT2)
        icon   = LAYER_ICON.get(layer, "?")
        sev    = severity(flags)

        if layer == "ok":
            label = "✓ %s  %s %s" % (ts, dv, du)
            bg = GOOD_BG
        else:
            flag_txt = " | ".join(f.upper() for f in flags) if flags else layer.upper()
            label = "%s %s  %s %s  %s" % (icon, ts, dv, du, flag_txt)
            bg = BAD_BG if sev=="danger" else WARN_BG if sev=="warn" else INFO_BG

        try:
            if self.lbl_noevt.winfo_exists():
                self.lbl_noevt.destroy()
        except Exception:
            pass

        pill = tk.Frame(self.evt_row, bg=bg, highlightbackground=col, highlightthickness=1)
        tk.Label(pill, text=label, bg=bg, fg=col, font=(FNT,9,"bold"), padx=8, pady=3).pack()
        pill.pack(side="left", padx=(0,5), pady=2)
        self._epills.append(pill)
        if len(self._epills) > 8:
            old = self._epills.pop(0)
            try: old.destroy()
            except: pass
        self.lbl_lv.configure(text="● %s" % ts, fg=col)

    # ── Filter / refresh ──────────────────────────────────────────────────────

    def _set_filter(self, d):
        self._filter.set(d)
        for k, b in self._fbts.items():
            b.configure(bg=ACCENT if k==d else CARD, fg=BG if k==d else TEXT2)
        self._refresh()

    def _since_ts(self):
        d = self._filter.get()
        return 0 if d == 0 else time.time() - d*86400

    def _refresh(self):
        rows = [row_to_typed(r) for r in load_log(self._since_ts())]
        self._update_metrics(rows)
        self._update_concerns(rows)
        self._draw_chart(rows)
        self._populate_log(rows)
        self.lbl_lc.configure(text="%d records" % len(rows) if len(rows) <= 2000
                              else "latest 2000 of %d records" % len(rows))
        if self._last_result:
            self._update_layer_cards(self._last_result)
            self._update_top_verdict(self._last_result)
        elif rows:
            # Fresh launch (v6.1): show the latest logged verdict instead
            # of the placeholder, so the strip is useful before the first
            # new test runs. Filtered rows, so the strip matches the page.
            self._update_top_verdict(rows[-1])

    def _update_metrics(self, rows):
        if not rows:
            for v, u in self._mvars: v.configure(text="—"); u.configure(text="")
            return
        total   = len(rows)
        ok      = sum(1 for r in rows if r["success"])
        dl_vals = [r["dl_mbps"] for r in rows if r["dl_mbps"] and r["dl_mbps"] > OUTAGE_MBPS]
        pings   = [r["ping_ms"] for r in rows if r["ping_ms"]]
        uptime  = ok/total*100
        av, au  = fmt_speed(statistics.mean(dl_vals) if dl_vals else None)
        pv, pu  = fmt_speed(max(dl_vals)             if dl_vals else None)
        avg_p   = statistics.mean(pings) if pings else None
        data    = [(av,au),(pv,pu),(("%.0f"%avg_p if avg_p else "—"),"ms"),
                   ("%.1f"%uptime,"%"),(str(total),"tests")]
        cols    = [
            ACCENT if (statistics.mean(dl_vals) if dl_vals else 0) >= slow_threshold() else WARN,
            ACCENT,
            ACCENT if (avg_p or 999) < LAG_MS else WARN,
            ACCENT if uptime>=99 else WARN if uptime>=95 else DANGER,
            TEXT,
        ]
        for (v,u),(val,unit),c in zip(self._mvars, data, cols):
            v.configure(text=val, fg=c); u.configure(text=unit)

    def _update_concerns(self, rows):
        for w in self.cf.winfo_children(): w.destroy()
        for title, desc, col in concerns(rows):
            bg = GOOD_BG if col==ACCENT else WARN_BG if col in (WARN,BLUE,PURPLE) else BAD_BG
            row = tk.Frame(self.cf, bg=bg, highlightbackground=col, highlightthickness=1)
            row.pack(fill="x", pady=(0,3))
            inn = tk.Frame(row, bg=bg); inn.pack(fill="x", padx=10, pady=5)
            tk.Label(inn, text=title, bg=bg, fg=col,
                     font=(FNT,9,"bold")).pack(side="left", padx=(0,10))
            tk.Label(inn, text=desc, bg=bg, fg=TEXT, font=(FNT2,9)).pack(side="left")

    def _draw_chart(self, rows):
        c = self.chart; c.delete("all")
        self.update_idletasks()
        W = c.winfo_width() or 950; H = c.winfo_height() or 120
        pl,pr,pt,pb = 52,20,8,20
        if not rows:
            c.create_text(W//2,H//2,text="No data",fill=TEXT3,font=(FNT,10)); return
        # Downsample for long histories (v6): drawing 100k points is slow
        # and invisible. Cap the plotted series at 600 points, evenly sampled.
        if len(rows) > 600:
            _n = len(rows)
            rows = [rows[int(i * (_n - 1) / 599)] for i in range(600)]
        dl  = [r["dl_mbps"] or 0 for r in rows]
        mx  = max(max(dl), slow_threshold()*1.2)
        if PLAN_MBPS > 0:
            mx = max(mx, PLAN_MBPS * 1.05)
        xp  = lambda i: pl + (i/max(len(rows)-1,1))*(W-pl-pr)
        yp  = lambda v: pt + (1-v/mx)*(H-pt-pb)
        for frac in [0.25,0.5,0.75,1.0]:
            y = yp(mx*frac)
            c.create_line(pl,y,W-pr,y,fill=BORDER,dash=(2,4))
            c.create_text(pl-4,y,text="%.0f"%(mx*frac),anchor="e",fill=TEXT3,font=(FNT,7))
        c.create_line(pl,yp(slow_threshold()),W-pr,yp(slow_threshold()),fill=WARN,dash=(3,5),width=1)
        if PLAN_MBPS > 0:
            c.create_line(pl,yp(PLAN_MBPS),W-pr,yp(PLAN_MBPS),fill=BLUE,dash=(3,5),width=1)
            c.create_text(W-pr-4,yp(PLAN_MBPS)-8,text="plan",anchor="e",fill=BLUE,font=(FNT,7))
        if len(rows) > 1:
            pts = [p for i,v in enumerate(dl) for p in (xp(i),yp(v))]
            pts += [xp(len(dl)-1),H-pb,xp(0),H-pb]
            c.create_polygon(pts,fill=CHART_FILL,outline="")
        for i in range(1,len(rows)):
            layer = rows[i].get("layer","ok")
            col   = LAYER_COLOR.get(layer, ACCENT)
            c.create_line(xp(i-1),yp(dl[i-1]),xp(i),yp(dl[i]),fill=col,width=2)
        for i,r in enumerate(rows):
            layer = r.get("layer","ok")
            if layer != "ok":
                col = LAYER_COLOR.get(layer, WARN)
                x,y = xp(i),yp(dl[i])
                c.create_oval(x-4,y-4,x+4,y+4,fill=col,outline=BG,width=1)
        step = max(1,len(rows)//5)
        for i in range(0,len(rows),step):
            ts = datetime.fromtimestamp(rows[i]["timestamp"])
            c.create_text(xp(i),H-pb+9,text=ts.strftime("%m/%d %H:%M"),
                           fill=TEXT3,font=(FNT,7))
        d = self._filter.get()
        self.lbl_cr.configure(text="last %dd"%d if d else "all time")

    def _populate_log(self, rows):
        # Cap the visible table (v6): 100k rows in a Treeview is slow.
        # The full history stays in the CSV and in reports.
        self._tree_rows = list(rows[-2000:]) if len(rows) > 2000 else list(rows)
        self._sort_col = None
        self._sort_rev = False
        self._render_tree()

    def _render_tree(self):
        for i in self.tree.get_children(): self.tree.delete(i)
        issue_short = {"outage":"⛔ OUTAGE","no_ping":"⛔ NO PING",
                       "slow":"⚠ SLOW","lag":"⚠ LAG","jitter":"〰 JITTER",
                       "loss":"📉 LOSS","dns":"🔤 DNS"}
        # Default order is newest first (v6 fix: the old loop inserted at
        # index 0 while iterating reversed rows, which actually displayed
        # oldest first despite Help saying newest first). A sorted view
        # displays in its sorted order.
        disp = list(reversed(self._tree_rows)) if self._sort_col is None else self._tree_rows
        for r in disp:
            dv,du = fmt_speed(r["dl_mbps"])
            issues = "  ".join(issue_short.get(f,f.upper()) for f in r["flags"]) or "✓ OK"
            layer  = r.get("layer","?")
            icon   = LAYER_ICON.get(layer,"?")
            sev    = severity(r["flags"])
            if layer == "wifi":   tag = "purple"
            elif sev == "danger": tag = "danger"
            elif sev == "warn":   tag = "warn"
            elif layer == "ok":   tag = "good"
            else:                 tag = "info"
            self.tree.insert("", "end", values=(
                r["datetime"], "%s %s"%(dv,du),
                fmt_ms(r.get("gw_ms")), fmt_ms(r.get("modem_ms")),
                fmt_ms(r.get("ping_ms")),
                "%s %s" % (icon, layer.upper()),
                issues
            ), tags=(tag,))
        for c, h in zip(("time","dl","gw","modem","cdn","layer","flags"),
                        ("Timestamp","Download","Router ms","Modem ms","CDN ms","Layer","Issues")):
            arrow = " ▲" if self._sort_col == c and not self._sort_rev else \
                    " ▼" if self._sort_col == c else ""
            self.tree.heading(c, text=h + arrow, command=lambda c=c: self._sort_tree(c))

    def _sort_tree(self, col):
        keys = {"time": lambda r: r["timestamp"],
                "dl": lambda r: r["dl_mbps"] if r["dl_mbps"] is not None else -1,
                "gw": lambda r: r["gw_ms"] if r["gw_ms"] is not None else -1,
                "modem": lambda r: r["modem_ms"] if r["modem_ms"] is not None else -1,
                "cdn": lambda r: r["ping_ms"] if r["ping_ms"] is not None else -1,
                "layer": lambda r: r.get("layer",""),
                "flags": lambda r: len(r["flags"])}
        if self._sort_col == col:
            self._sort_rev = not self._sort_rev
        else:
            self._sort_col = col
            self._sort_rev = False
        self._tree_rows.sort(key=keys[col], reverse=self._sort_rev)
        self._render_tree()

    # ── Progress ──────────────────────────────────────────────────────────────

    def _show_progress(self, step, total, msg):
        def _do():
            if not self.prog_frame.winfo_ismapped():
                self.prog_frame.pack(fill="x", before=self.lbl_sb.master)
            self.prog_bar.configure(value=step)
            self.prog_lbl.configure(text=msg)
        self.after(0, _do)

    def _hide_progress(self):
        def _do():
            self.prog_frame.pack_forget()
        self.after(0, _do)


    # ── Help window ───────────────────────────────────────────────────────────

    def _show_help(self):
        win = tk.Toplevel(self)
        win.title("ISP Monitor — Help & Instructions")
        win.geometry("680x720")
        win.minsize(560, 500)
        win.configure(bg=BG)
        win.resizable(True, True)

        # Header
        hdr = tk.Frame(win, bg=SURFACE); hdr.pack(fill="x")
        tk.Label(hdr, text="ISP MONITOR  —  Help & Instructions",
                 bg=SURFACE, fg=ACCENT, font=(FNT,12,"bold")).pack(side="left", padx=18, pady=12)
        tk.Label(hdr, text="created by SBB",
                 bg=SURFACE, fg=TEXT3, font=(FNT2,9)).pack(side="left")
        tk.Button(hdr, text="X  Close", command=win.destroy,
                  bg=CARD2, fg=TEXT2, font=(FNT,9), relief="flat",
                  padx=10, pady=4, cursor="hand2").pack(side="right", padx=14)

        # Scrollable content
        outer = tk.Frame(win, bg=BG); outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        content = tk.Frame(canvas, bg=BG)
        cwin = canvas.create_window((0,0), window=content, anchor="nw")

        def on_resize(e):
            canvas.itemconfig(cwin, width=e.width)
        canvas.bind("<Configure>", on_resize)
        content.bind("<Configure>", lambda e: canvas.configure(
            scrollregion=canvas.bbox("all")))

        def _scroll(e):
            canvas.yview_scroll(int(-1*(e.delta/120)), "units")
        # Bind on the window itself: the toplevel is in every descendant's
        # bindtags chain, so this covers the whole help page without
        # bind_all (which used to steal/replace the main window's wheel).
        win.bind("<MouseWheel>", _scroll)

        def section(title, color=ACCENT):
            tk.Frame(content, height=6, bg=BG).pack()
            f = tk.Frame(content, bg=BG); f.pack(fill="x", padx=18, pady=(10,4))
            tk.Label(f, text=title, bg=BG, fg=color,
                     font=(FNT,10,"bold")).pack(anchor="w")
            tk.Frame(content, bg=color, height=1).pack(fill="x", padx=18, pady=(0,6))

        def helprow(label, desc, label_col=TEXT):
            f = tk.Frame(content, bg=CARD,
                          highlightbackground=BORDER, highlightthickness=1)
            f.pack(fill="x", padx=18, pady=2)
            f.columnconfigure(0, minsize=220)
            f.columnconfigure(1, weight=1)
            tk.Label(f, text=label, bg=CARD, fg=label_col,
                     font=(FNT,9,"bold"), anchor="nw",
                     wraplength=200, justify="left").grid(
                     row=0, column=0, sticky="nw", padx=(12,16), pady=10)
            tk.Frame(f, bg=BORDER, width=1).grid(row=0, column=0,
                     sticky="nse", padx=(0,0), pady=6)
            tk.Label(f, text=desc, bg=CARD, fg=TEXT2,
                     font=(FNT2,9), anchor="nw",
                     wraplength=430, justify="left").grid(
                     row=0, column=1, sticky="nw", padx=(16,12), pady=10)

        def note(text, col=TEXT2):
            tk.Label(content, text=text, bg=BG, fg=col,
                     font=(FNT2,9), anchor="w", wraplength=620,
                     justify="left").pack(fill="x", padx=22, pady=(2,0))

        # GETTING STARTED
        section("GETTING STARTED")
        note("Two ways to use this tool:", TEXT)
        helprow("TEST NOW  (lightning bolt)",
            "Runs one full test right now (~10-20 sec; a flagged test adds "
            "a traceroute). Measures latency, download speed, diagnoses all "
            "4 network layers, and traces the route when a problem is found. "
            "Results appear immediately in all panels.", ACCENT)
        helprow("START MONITORING  (play button)",
            "Runs a test every 5 minutes automatically. Leave it running "
            "in the background — it builds history so you can spot patterns "
            "like evening slowdowns, outages, or ISP throttling.", ACCENT)
        helprow("LINE SPEED  (signal icon)",
            "Uses Speedtest.net infrastructure to measure your line's raw "
            "MAXIMUM download and upload capacity — the same numbers you see "
            "on speedtest.net. Results are saved to peak_log.csv. "
            "After measuring, the panel shows exactly what you can do with "
            "that speed (streaming, gaming, downloads, etc).", BLUE)
        note("Tip: Start monitoring, minimise the window, and check back "
             "in an hour. You will have 12+ data points and the chart "
             "shows real trends.", WARN)

        # WHAT'S NEW IN V6
        section("WHAT'S NEW IN V6", ACCENT)
        helprow("Packet loss",
            "Every TEST NOW runs 10 extra probes and reports loss %. 20% or more "
            "flags LOSS — the number ISPs take seriously for calls and games.", ACCENT)
        helprow("WiFi signal",
            "On WiFi, the signal % shows on the WiFi card. Below 40% the test "
            "names weak signal as the fault.", ACCENT)
        helprow("DNS check",
            "A separate DNS test separates 'DNS is broken' from 'internet is "
            "broken'. A DNS fault names switching to 8.8.8.8 / 1.1.1.1.", ACCENT)
        helprow("Verdict strip",
            "The latest verdict sits at the very top of the window, so you see "
            "healthy / issue without scrolling. On launch it shows your most "
            "recent logged test, not a placeholder.", ACCENT)
        helprow("Downtime totals",
            "The text report now totals downtime (~minutes across outages), "
            "lists each outage window, and names your slowest hours.", ACCENT)
        helprow("Copy ISP summary",
            "The 📋 button copies a short complaint-ready summary to the "
            "clipboard — paste it straight into ISP chat.", ACCENT)
        helprow("Autostart + sound",
            "Settings can start the app with Windows, and play a sound + flash "
            "the taskbar when an outage starts.", ACCENT)
        helprow("Theme",
            "Dark (default) or Light in Settings. A theme change applies on "
            "the next launch.", ACCENT)
        helprow("Sortable log",
            "Click any log column header to sort (click again to reverse). "
            "The log shows newest first and caps at the latest 2000 rows "
            "for speed.", ACCENT)

        # WHAT'S NEW IN V5.1 & V5.2
        section("WHAT'S NEW IN V5.1 & V5.2", ACCENT)
        helprow("Window scrolls",
            "The main window now scrolls with a scrollbar and mouse wheel, "
            "and opens sized to your screen — the chart, log, and status bar "
            "are no longer cut off on smaller screens.", ACCENT)
        helprow("No more flashing console windows",
            "Tests used to pop up a console window for every ping and "
            "traceroute (about 15 per test). All helper tools now run "
            "hidden — tests run silently in the background.", ACCENT)

        # WHAT'S NEW IN V5
        section("WHAT'S NEW IN V5", ACCENT)
        helprow("Real ping + traceroute",
            "Tests now use real ICMP ping first (TCP connect as fallback), and "
            "when a test flags a problem it traces the route hop by hop to show "
            "exactly where traffic stops or slows — saved to trace_log.txt.", ACCENT)
        helprow("Your plan speed (settings row)",
            "Enter your plan speed in the settings row under the filters. "
            "SLOW now flags when you fall below your chosen % of plan "
            "(default 50%), instead of only below a fixed 10 Mbps.", ACCENT)
        helprow("Upload + logged line speed",
            "LINE SPEED now measures upload too, and every LINE SPEED result "
            "is saved to peak_log.csv and included in the text report — so "
            "your capacity evidence is in the file you send your ISP.", BLUE)
        helprow("Better routine speed test",
            "The routine download test now runs for up to 8 seconds / 25 MB "
            "instead of one quick pass over small files, so it measures real "
            "throughput on fast lines instead of mostly connection setup.", TEXT2)

        # UNDERSTANDING YOUR SPEED NUMBERS
        section("UNDERSTANDING YOUR SPEED NUMBERS", BLUE)
        note("This is the most important section — it explains why you see "
             "different numbers in different places.", TEXT)

        helprow("Why LINE SPEED ≠ TEST NOW speed",
            "LINE SPEED (e.g. 556 Mbps) = your pipe's maximum raw capacity, "
            "measured by flooding the line with many parallel streams from "
            "dedicated test servers near you.  "
            "TEST NOW (e.g. 20 Mbps) = a single file download from a CDN "
            "server — which is how most web browsing actually works. "
            "Both numbers are correct. They measure different things.", BLUE)

        helprow("Why Google / Speedtest shows 500+ Mbps",
            "Speed test sites use 8-16 simultaneous connections to dedicated "
            "servers that exist purely for testing, often in the same city. "
            "This is the best possible scenario for your line. "
            "It tells you the CEILING of your connection.", BLUE)

        helprow("Why normal browsing feels slower",
            "A webpage loads 20-50 small files one by one. Each file uses "
            "one connection to a server that's shared with millions of people "
            "worldwide. You're only using a tiny fraction of your line at any "
            "moment. This is completely normal.", TEXT2)

        helprow("Why the gap is NOT a problem",
            "Having 556 Mbps line speed but 20 Mbps single-stream CDN speed "
            "is like having a 6-lane highway but only driving in one lane. "
            "When you download a game on Steam or watch Netflix in 4K, "
            "multiple lanes open up and you use much more of that 556 Mbps.", ACCENT)

        helprow("When the gap IS a problem",
            "If your LINE SPEED is 20 Mbps and Speedtest also only shows "
            "20 Mbps — your actual ISP plan may be slow, or there could be "
            "congestion. Use the monitoring feature to track it over time "
            "and show the data to your ISP.", WARN)

        # WHAT YOUR SPEED CAN DO
        section("WHAT YOUR LINE SPEED CAN DO", ACCENT)
        note("After running LINE SPEED, the panel shows 6 usage cards. "
             "Here is what each one means:", TEXT)

        helprow("🎬  4K Streaming  (needs 25 Mbps)",
            "One 4K Netflix / YouTube / Disney+ stream. "
            "With 556 Mbps you could run 22 of these simultaneously. "
            "HD (1080p) only needs 5 Mbps.", ACCENT)
        helprow("🎮  Online Gaming  (needs 5 Mbps)",
            "Speed barely matters for gaming — a game uses 1-5 Mbps. "
            "What matters is LATENCY (ping) and JITTER. "
            "This tool monitors both — check the Avg Latency card.", ACCENT)
        helprow("📹  Video Call HD  (needs 5 Mbps)",
            "Zoom / Teams / FaceTime in 1080p. Low jitter is more important "
            "than raw speed. If calls are choppy, check your Jitter score.", ACCENT)
        helprow("💾  Game Download  (70%% of line speed)",
            "Steam, Epic, and PlayStation use multiple parallel connections "
            "and typically saturate 60-80%% of your line. "
            "With 556 Mbps, a 50 GB game downloads in about 10 minutes.", ACCENT)
        helprow("🎵  Music Streaming  (needs 0.5 Mbps)",
            "Spotify, Apple Music, Tidal. Uses almost no bandwidth — "
            "virtually any connection handles this fine.", ACCENT)
        helprow("👥  Whole household  (100 Mbps recommended)",
            "4K TV + 2 video calls + gaming + browsing simultaneously. "
            "100 Mbps comfortably handles a typical family. "
            "With 556 Mbps you have plenty of headroom.", ACCENT)

        note("GREEN card = your line handles this easily.  "
             "AMBER = marginal — may work but could buffer.  "
             "RED = your line cannot handle this activity.", TEXT)

        # WHAT EACH TEST MEASURES
        section("WHAT EACH TEST MEASURES")
        helprow("Download Speed  (TEST NOW)",
            "Fetches real files from public CDN servers — one at a time, "
            "for up to 8 seconds, the same way your browser loads a website. "
            "Bypasses router firmware so it reflects actual ISP throughput.", BLUE)
        helprow("Router Ping  (gw ms)",
            "Pings your local gateway (e.g. 192.168.1.1). Measures "
            "WiFi or cable signal quality between your PC and router. "
            "Should be under 5ms on ethernet, under 20ms on WiFi.", PURPLE)
        helprow("Modem Ping  (modem ms)",
            "Pings Google DNS (8.8.8.8) and Cloudflare (1.1.1.1) directly "
            "by IP — bypasses DNS so it only tests the modem handoff. "
            "If this fails, the problem is between router and internet.", WARN)
        helprow("Packet Loss  (loss %)",
            "10 quick probes to the CDN host on every test. 0–10% is normal "
            "noise; 20% or more flags LOSS and names the ISP as the fault "
            "when latency is otherwise healthy.", BLUE)
        helprow("DNS  (dns ms)",
            "Times an independent resolver (8.8.8.8) and checks whether your "
            "own DNS resolves at all. If names fail but IP pings work, the "
            "verdict is a DNS problem, not a dead connection.", BLUE)
        helprow("CDN Latency  (cdn ms)",
            "Pings Cloudflare content servers. Measures ISP routing quality. "
            "Under 50ms = excellent.  50-150ms = acceptable.  "
            "Over 150ms = flagged as lag.", ACCENT)
        helprow("Jitter",
            "Standard deviation of ping times. High jitter = unstable "
            "connection even if average ping looks OK. Causes choppy "
            "video calls and lag spikes in gaming. Under 10ms is ideal.", TEXT2)

        # NETWORK LAYER CARDS
        section("NETWORK LAYER CARDS  (the 4 boxes)")
        note("Each card measures a DIFFERENT, INDEPENDENT hop in your connection. "
             "They are NOT additive — a card rating of GOOD does not mean the "
             "next card will be worse. Each is judged on its own scale.", TEXT)

        helprow("Why WiFi shows GOOD but ISP shows EXCELLENT",
            "Each card has DIFFERENT thresholds because each hop is held to "
            "a different standard.\n\n"
            "WiFi EXCELLENT = under 5 ms  (a local wireless hop should be near-instant)\n"
            "ISP  EXCELLENT = under 30 ms (crossing the whole internet takes longer)\n\n"
            "So WiFi at 11ms = GOOD, and ISP at 10ms = EXCELLENT — even though 11ms "
            "is actually a higher raw number. The WiFi is perfectly healthy, it just "
            "doesn't hit the stricter 5ms local-hop threshold. "
            "This does NOT mean your WiFi is slow.", BLUE)

        helprow("What each card actually measures",
            "📶  WiFi / Cable  —  ping from your PC to your router (e.g. 192.168.1.1). "
            "This is purely the wireless or cable hop. Nothing to do with ISP.\n\n"
            "🔁  Router  —  same ping, assessed for router response health.\n\n"
            "📡  Modem / ISP  —  ping to 8.8.8.8 (Google) by raw IP. "
            "Crosses WiFi + router + modem + ISP backbone. "
            "Tests the modem handoff to the internet.\n\n"
            "🌐  Internet / ISP  —  ping to Cloudflare CDN servers. "
            "Tests ISP routing quality to the wider internet, plus download speed.", ACCENT)

        helprow("Is ISP speed spread through WiFi/router?",
            "No — your ISP speed is NOT divided or shared between the cards. "
            "Each card tests a specific hop independently.\n\n"
            "Think of it like testing a road trip:\n"
            "WiFi = your driveway  |  Router = your street  |  "
            "Modem = the on-ramp  |  ISP = the highway\n\n"
            "A slow driveway does not slow down the highway. "
            "But if your driveway is slow, you will never reach highway speed "
            "regardless of how fast the highway is.", TEXT2)

        note("After each test the 4 cards light up and show a plain-English "
             "rating based on the measured response time:", TEXT)

        helprow("📶  WiFi / Cable\n(Signal to router)",
            "Measures the ping from your PC to your router.\n"
            "EXCELLENT = under 5 ms  (ethernet or strong WiFi)\n"
            "GOOD      = under 20 ms  (normal WiFi)\n"
            "FAIR      = under 50 ms  (weak signal, move closer)\n"
            "SLOW      = over 50 ms   (poor WiFi or cable issue)\n"
            "Fix: move closer to router, or try an ethernet cable.", PURPLE)

        helprow("🔁  Router\n(Local gateway)",
            "Measures how fast your router responds to requests.\n"
            "EXCELLENT = under 5 ms   (router is healthy)\n"
            "GOOD      = under 20 ms  (normal)\n"
            "FAIR      = under 50 ms  (router under load)\n"
            "SLOW      = over 50 ms   (router overloaded)\n"
            "Fix: restart your router. If still slow, may need replacing.", WARN)

        helprow("📡  Modem / ISP\n(External IP reach)",
            "Pings 8.8.8.8 (Google) and 1.1.1.1 (Cloudflare) by raw IP "
            "— no DNS, pure modem handoff test.\n"
            "EXCELLENT = under 20 ms  (modem is healthy)\n"
            "GOOD      = under 60 ms  (normal)\n"
            "FAIR      = under 120 ms (ISP congestion possible)\n"
            "SLOW      = over 120 ms  (modem or ISP problem)\n"
            "NO RESPONSE = modem cannot reach the internet at all.\n"
            "Fix: restart modem. If still failing, contact your ISP.", WARN)

        helprow("🌐  Internet / ISP\n(CDN & download speed)",
            "Pings Cloudflare CDN servers — measures ISP routing quality "
            "to the broader internet.\n"
            "EXCELLENT = under 30 ms  (excellent ISP routing)\n"
            "GOOD      = under 80 ms  (normal)\n"
            "FAIR      = under 150 ms (some congestion)\n"
            "SLOW      = over 150 ms  (ISP routing issue — flagged as LAG)\n"
            "Also shows your download speed from this CDN.\n"
            "Fix: contact ISP, check for outages in your area.", DANGER)

        note("A health bar fills left to right — short green bar = fast, "
             "longer amber = moderate, full red = problem. "
             "If a card shows ISSUE FOUND it means that layer is "
             "identified as the fault source for that test.", ACCENT)

        # READING THE DASHBOARD
        section("READING THE DASHBOARD")
        helprow("Metric cards (top row)",
            "Averages across ALL tests in the selected time window. "
            "Updates after every new test.", TEXT)
        helprow("Live Events strip",
            "Each pill = one test. Colour = fault layer. "
            "Green = all OK, amber = slow or lag, red = outage, "
            "purple = WiFi issue. Newest pill on the right.", TEXT)
        helprow("Concerns banner",
            "Appears automatically when a pattern is found — e.g. outages "
            "more than 5%% of the time, or avg speed far below peak "
            "(possible throttling).", TEXT)
        helprow("Download speed chart",
            "Line chart of every test. Colour of each segment = fault layer. "
            "Amber dashed line = 10 Mbps threshold. Dots = flagged events.", TEXT)
        helprow("Test log table",
            "Full history with Router ms, Modem ms, CDN ms, Layer, Issues. "
            "Newest tests at the top.", TEXT)

        # TIME FILTER
        section("TIME FILTER  (SHOW buttons)")
        note("The 24 H / 3 D / 7 D / 14 D / 30 D / ALL buttons filter "
             "everything on screen to that window — chart, metrics, "
             "concerns, and log all update instantly.", TEXT)

        # SAVING YOUR DATA
        section("SAVING YOUR DATA")
        helprow("isp_log.csv  (auto)",
            "Every test is saved automatically next to the .exe. "
            "Open it in Excel or Notepad any time — no action needed.", ACCENT)
        helprow("Text report button",
            "Saves a full .txt report — summary, layer breakdown, concerns, "
            "flagged events, and full log. Opens in Notepad automatically.", TEXT)
        helprow("Open CSV log button",
            "Opens isp_log.csv directly in Excel or Notepad.", TEXT)

        # COLOUR GUIDE
        section("COLOUR GUIDE")
        helprow("Green   checkmark",  "All healthy — no issues detected",          ACCENT)
        helprow("Amber   warning",    "Warning — slow speed or high latency",       WARN)
        helprow("Red     stop",       "Critical — outage or no connectivity",       DANGER)
        helprow("Purple  signal",     "WiFi-specific issue detected",               PURPLE)
        helprow("Blue    wave",       "Jitter — unstable connection",               BLUE)

        # THRESHOLDS
        section("ALERT THRESHOLDS  (full breakdown)")
        note("These are the exact values used to rate each card and flag issues:", TEXT)

        helprow("WiFi / Router ping",
            "Excellent: under 5 ms\n"
            "Good:      under 20 ms\n"
            "Fair:      under 50 ms\n"
            "Slow:      over 50 ms  → flagged", PURPLE)

        helprow("Modem ping  (8.8.8.8)",
            "Excellent: under 20 ms\n"
            "Good:      under 60 ms\n"
            "Fair:      under 120 ms\n"
            "Slow:      over 120 ms  → flagged", WARN)

        helprow("CDN / ISP latency",
            "Excellent: under 30 ms\n"
            "Good:      under 80 ms\n"
            "Fair:      under 150 ms\n"
            "Slow:      over 150 ms  → LAG flag", WARN)

        helprow("Download speed",
            "Normal:  above 10 Mbps\n"
            "Slow:    below 10 Mbps  → SLOW flag\n"
            "Outage:  below 0.5 Mbps → OUTAGE flag", DANGER)

        helprow("Jitter",
            "Good:     under 10 ms  (stable)\n"
            "Moderate: under 40 ms  (acceptable)\n"
            "High:     over 40 ms   → JITTER flag\n"
            "High jitter causes choppy video calls and lag spikes in gaming "
            "even when average ping looks fine.", BLUE)

        helprow("Monitor interval",
            "Runs automatically every interval you set (default 5 minutes).\n"
            "Each test takes about 10–20 seconds; a flagged test adds a "
            "traceroute (up to ~30 s more).", TEXT2)

        # FAQ
        section("COMMON QUESTIONS", TEXT2)
        helprow("Why is my speed 20 Mbps here but 500 on speedtest.net?",
            "Both are correct — they measure different things. See "
            "'Understanding Your Speed Numbers' above for the full explanation. "
            "Short answer: speedtest uses 16 parallel connections to a nearby "
            "dedicated server. This tool uses 1 connection to a real CDN.", BLUE)
        helprow("Is a big gap between LINE SPEED and TEST NOW bad?",
            "No — the gap is always expected and is completely normal. "
            "It only becomes a concern if your LINE SPEED itself is far "
            "below what your ISP plan promises (e.g. paying for 500 Mbps "
            "but LINE SPEED shows 50 Mbps).", TEXT2)
        helprow("My ping is fine but video calls are still choppy — why?",
            "Check your JITTER score. High jitter (above 20-30ms) causes "
            "choppy calls even with a low average ping. Also check if "
            "someone else is downloading in the background.", TEXT2)
        helprow("What should I show my ISP if I have a problem?",
            "Run TEXT REPORT after a few hours of monitoring. It includes "
            "a full log with timestamps of every outage, slow period, and "
            "layer diagnosis. ISPs take timestamped data seriously.", ACCENT)

        tk.Frame(content, height=24, bg=BG).pack()

    # ── Export ────────────────────────────────────────────────────────────────

    def _get_rows(self):
        return [row_to_typed(r) for r in load_log(self._since_ts())]

    def _exp_text(self):
        rows = self._get_rows()
        if not rows: return messagebox.showinfo("No data","No tests in selected period.")
        path = filedialog.asksaveasfilename(defaultextension=".txt",
            filetypes=[("Text file","*.txt")], initialfile="isp_report.txt")
        if not path: return
        try:
            export_text(rows, path)
            if os.name=="nt":            os.startfile(path)
            elif sys.platform=="darwin": os.system("open '%s'"%path)
            else:                        os.system("xdg-open '%s'"%path)
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _copy_summary(self):
        """Copy a short complaint-ready summary to the clipboard (v6)."""
        rows = self._get_rows()
        if not rows:
            self.lbl_sb.configure(text="Nothing to copy yet — run a test first", fg=TEXT3)
            return
        total = len(rows)
        ok = sum(1 for r in rows if r["success"])
        dl_vals = [r["dl_mbps"] for r in rows if r["dl_mbps"] and r["dl_mbps"] > OUTAGE_MBPS]
        av, au = fmt_speed(statistics.mean(dl_vals) if dl_vals else None)
        pv, pu = fmt_speed(max(dl_vals) if dl_vals else None)
        wins = outage_windows(rows)
        layers = {}
        for r in rows:
            if r.get("layer") not in ("ok", "unknown"):
                layers[r["layer"]] = layers.get(r["layer"], 0) + 1
        main_layer = max(layers, key=layers.get) if layers else "none"
        hours = slowest_hours(rows)
        lines = ["ISP Monitor summary (%s to %s)" % (rows[0]["datetime"], rows[-1]["datetime"]),
                 "Uptime: %.1f%% (%d tests)" % (ok/total*100, total),
                 "Avg download: %s %s | Peak: %s %s" % (av, au, pv, pu)]
        if wins:
            lines.append("Downtime: ~%.0f min across %d outage(s), longest ~%.0f min" % (
                sum(w[3] for w in wins), len(wins), max(w[3] for w in wins)))
        else:
            lines.append("Downtime: none recorded")
        lines.append("Main fault layer: %s" % main_layer.upper())
        if hours:
            hv, hu = fmt_speed(hours[0][1])
            lines.append("Slowest hour: %02d:00 (avg %s %s)" % (hours[0][0], hv, hu))
        lines.append("Full log available on request.")
        try:
            self.clipboard_clear()
            self.clipboard_append("\n".join(lines))
            self.lbl_sb.configure(text="ISP summary copied to clipboard ✓", fg=ACCENT)
        except Exception as e:
            self.lbl_sb.configure(text="Could not copy: %s" % e, fg=DANGER)

    def _on_theme_change(self, value):
        try:
            update_config_value("theme", value.lower())
            self.lbl_settings.configure(text="Theme saved — restart the app to apply it", fg=ACCENT)
        except Exception as e:
            self.lbl_settings.configure(text="Could not save theme: %s" % e, fg=DANGER)

    def _on_sound_change(self):
        try:
            update_config_value("sound_on_outage", self._sound_var.get())
        except Exception:
            pass

    def _on_autostart_change(self):
        ok = set_autostart(bool(self._auto_var.get()))
        if ok:
            msg = "Autostart on ✓" if self._auto_var.get() else "Autostart off ✓"
            self.lbl_settings.configure(text=msg, fg=ACCENT)
        else:
            self.lbl_settings.configure(text="Could not change autostart", fg=DANGER)

    def _dismiss_hint(self):
        try:
            update_config_value("hint_dismissed", 1)
        except Exception:
            pass
        if self.hint_frame is not None:
            try: self.hint_frame.destroy()
            except Exception: pass
            self.hint_frame = None

    def _open_csv(self):
        p = str(log_path())
        try:
            if os.name=="nt":            os.startfile(p)
            elif sys.platform=="darwin": os.system("open '%s'"%p)
            else:                        os.system("xdg-open '%s'"%p)
        except Exception:
            messagebox.showinfo("CSV location", p)

    def _open_url(self, url):
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            messagebox.showinfo("Open in browser", url)

    # ── Monitoring ────────────────────────────────────────────────────────────

    # ── Busy indicator (v5.3) ─────────────────────────────────────────
    # A test takes 10-60 s, and all the old progress feedback lived at the
    # bottom of a scrollable page — invisible from the top bar. The spinner
    # + text live next to the buttons, and disabling both test buttons
    # while busy means a click can never vanish silently (the old LINE
    # SPEED "click twice" bug: a click during another run was dropped
    # with no message at all).
    def _set_busy(self, on, msg=""):
        self._busy = on
        if on:
            self.btn_test.configure(state="disabled")
            self.btn_peak.configure(state="disabled")
            self._spin_idx = 0
            self._tick_spinner(msg)
        else:
            if self._spin_after is not None:
                try: self.after_cancel(self._spin_after)
                except Exception: pass
                self._spin_after = None
            self.lbl_busy.configure(text="")
            self.btn_test.configure(state="normal")
            self.btn_peak.configure(state="normal")

    def _tick_spinner(self, msg):
        if not self._busy:
            return
        frames = "◐◓◑◒"
        self.lbl_busy.configure(text="%s %s" % (frames[self._spin_idx % 4], msg))
        self._spin_idx += 1
        self._spin_after = self.after(120, lambda: self._tick_spinner(msg))

    def _save_settings(self):
        try:
            plan = float(self.ent_plan.get() or 0)
            pct  = float(self.ent_pct.get() or 50)
            interval = float(self.ent_int.get() or 5)
        except ValueError:
            return messagebox.showerror("Settings", "Please enter numbers only.")
        if plan < 0 or not (10 <= pct <= 95) or not (1 <= interval <= 60):
            return messagebox.showerror("Settings",
                "Plan must be 0 or more, slow alert 10–95%, interval 1–60 min.")
        save_config(plan, pct, interval)
        self.lbl_settings.configure(text="Saved \u2713  (applies to the next test)", fg=ACCENT)

    def _toggle_mon(self):
        if self._active:
            self._active = False
            self.btn_mon.configure(text="▶  START MONITORING", bg=ACCENT, fg=BG)
            self.lbl_lv.configure(text="● STOPPED", fg=TEXT3)
        else:
            self._active = True
            self._next_ts = time.time()
            self.btn_mon.configure(text="⏹  STOP MONITORING", bg=DANGER, fg="white")
            self._mon_loop()

    def _mon_loop(self):
        if not self._active: return
        if time.time() >= self._next_ts:
            self._next_ts = time.time() + interval_seconds()
            if not self._testing:
                self._set_busy(True, "Testing…")
                threading.Thread(target=self._do_test, daemon=True).start()
        self.after(1000, self._mon_loop)

    def _start_countdown(self):
        def tick():
            if self._active:
                s = max(0, int(self._next_ts - time.time()))
                m, s2 = divmod(s, 60)
                self.lbl_next.configure(text="next %02d:%02d"%(m,s2))
            else:
                self.lbl_next.configure(text="")
            self.after(1000, tick)
        tick()

    def _manual_test(self):
        if self._testing: return
        self.lbl_live2.configure(text="● TESTING…")
        self.lbl_lv.configure(text="● RUNNING…", fg=ACCENT)
        self._set_busy(True, "Testing…")
        self.after(0, self._reset_layer_cards)
        threading.Thread(target=lambda: self._do_test(deep=True), daemon=True).start()

    def _do_test(self, deep=False):
        self._testing = True
        try:
            result = run_test(progress_cb=self._show_progress, deep=deep)
        except Exception as e:
            def _err():
                self._testing = False
                self._set_busy(False)
                self.lbl_live2.configure(text="")
                self.lbl_sb.configure(text="Test failed: %s" % e, fg=DANGER)
            self.after(0, _err)
            return
        self._last_result = result
        dv, du = fmt_speed(result["dl_mbps"])
        flags  = result["flags"]
        layer  = result.get("layer","ok")
        col    = LAYER_COLOR.get(layer, ACCENT)
        msg    = "%s %s  [%s]" % (dv, du, layer.upper()) if layer != "ok" else "%s %s  ✓ OK" % (dv, du)
        def _update():
            self._push_event(result)
            self._update_layer_cards(result)
            self._update_top_verdict(result)
            # Outage alert (v6): banner on every outage result; sound +
            # taskbar flash only when an outage STARTS, not every 5 min
            # while one drags on.
            if "outage" in flags:
                self.lbl_alert.configure(text="⛔ OUTAGE — %s" % result["datetime"][11:19])
                if not self._was_outage and SOUND_ON_OUTAGE:
                    try: self.bell()
                    except Exception: pass
                    self._flash_taskbar()
                self._was_outage = True
            else:
                self.lbl_alert.configure(text="")
                self._was_outage = False
            self._refresh()
            self._hide_progress()
            self.lbl_live2.configure(text="")
            self.lbl_sb.configure(
                text="Last: %s  |  %s" % (result["datetime"], msg), fg=col)
            self._testing = False
            self._set_busy(False)
        self.after(0, _update)

    def _flash_taskbar(self):
        """Flash the taskbar button (Windows only) without stealing focus."""
        try:
            if sys.platform == "win32":
                import ctypes
                class FLASHWINFO(ctypes.Structure):
                    _fields_ = [("cbSize", ctypes.c_uint), ("hwnd", ctypes.c_void_p),
                                ("dwFlags", ctypes.c_uint), ("uCount", ctypes.c_uint),
                                ("dwTimeout", ctypes.c_uint)]
                info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), self.winfo_id(), 0x00000002, 3, 0)
                ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
        except Exception:
            pass

    def _run_peak_test(self):
        """Run the line speed test."""
        if self._testing: return
        self._testing = True
        self.lbl_peak_val.configure(text="…", fg=BLUE)
        self.lbl_peak_unit.configure(text="run LINE SPEED to measure", fg=TEXT3)
        self.lbl_peak_run.configure(text="● RUNNING…", fg=BLUE)
        self.lbl_gap_explain.configure(text="Measuring your line capacity…", fg=ACCENT)
        self._set_busy(True, "Measuring line speed…")

        def _worker():
            try:
                def prog(msg, pct):
                    self.after(0, lambda m=msg: self.lbl_peak_run.configure(text=m, fg=BLUE))

                peak_dl, peak_ul = peak_speed_test(progress_cb=prog)
                if peak_dl is not None:
                    append_peak_log(peak_dl, peak_ul)
                pv, pu = fmt_speed(peak_dl)
                up_txt = ("%.0f Mbps up" % peak_ul) if peak_ul else ""

                def _update():
                    try:
                        self.lbl_peak_val.configure(text=pv, fg=BLUE)
                        self.lbl_peak_unit.configure(
                            text=(pu + " down  •  " + up_txt) if up_txt else pu, fg=TEXT2)
                        self.lbl_peak_run.configure(text="Measured via Speedtest.net", fg=TEXT3)
                        self._build_scenarios(peak_dl)
                    finally:
                        self._testing = False
                        self._set_busy(False)
                self.after(0, _update)
            except Exception as e:
                def _err():
                    self._testing = False
                    self._set_busy(False)
                    self.lbl_peak_run.configure(text="Line speed failed: %s" % e, fg=DANGER)
                self.after(0, _err)

        threading.Thread(target=_worker, daemon=True).start()

    def _build_scenarios(self, peak_mbps):
        """Replace placeholder with what-can-you-do usage cards based on line speed."""
        for w in self._scenario_frame.winfo_children():
            w.destroy()

        if peak_mbps is None or peak_mbps <= 0:
            tk.Label(self._scenario_frame, text="Could not measure line speed.",
                     bg=CARD, fg=WARN, font=(FNT2,9)).pack(pady=10)
            return

        def streams(needed):
            return int(peak_mbps / needed) if needed > 0 else 999

        def dl_time_str(size_gb):
            """How long to download size_gb at 70% of line speed."""
            mbps   = peak_mbps * 0.7
            secs   = (size_gb * 8 * 1024) / mbps   # GB → Gb → sec
            if secs < 60:   return "~%.0f seconds" % secs
            if secs < 3600: return "~%.0f minutes" % (secs / 60)
            return "~%.1f hours" % (secs / 3600)

        n4k    = streams(25)
        n_hd   = streams(5)
        n_call = streams(5)
        n_music= streams(0.5)
        n_house= streams(100)

        # Card definitions: (icon, title, subtitle, big_text, small_text, colour, bg)
        # Using plain sentences, no symbols
        def card_color(count):
            if count >= 4: return ACCENT, GOOD_BG
            if count >= 2: return WARN,   WARN_BG
            if count >= 1: return WARN,   WARN_BG
            return DANGER, BAD_BG

        fg4k, bg4k     = card_color(n4k)
        fghd, bghd     = card_color(n_hd)
        fgcl, bgcl     = card_color(n_call)
        fgmu, bgmu     = card_color(n_music)
        fgdl, bgdl     = (WARN, WARN_BG) if peak_mbps < 100 else (ACCENT, GOOD_BG)
        fgho, bgho     = card_color(n_house)

        scenarios = [
            # icon, title, need_desc, big_line, detail_line, fg, bg
            ("🎬", "4K Streaming",
             "Netflix / YouTube / Disney+  needs 25 Mbps each",
             "Up to %d screens" % min(n4k, 99) if n4k < 99 else "Unlimited screens",
             "Your line supports %d simultaneous 4K streams" % n4k if n4k < 50
                 else "More than enough for the whole household",
             fg4k, bg4k),

            ("🎮", "Online Gaming",
             "Gaming uses only 1-5 Mbps — speed is not the issue",
             "Your speed is fine",
             "Focus on your LATENCY (%s) and JITTER — not speed" % "see Avg Latency card",
             ACCENT, GOOD_BG),

            ("📹", "Video Call HD",
             "Zoom / Teams 1080p needs ~5 Mbps per person",
             "Up to %d calls at once" % min(n_call, 99) if n_call < 50 else "No limit",
             "If calls are choppy, check your Jitter score — not speed",
             fgcl, bgcl),

            ("🎵", "Music Streaming",
             "Spotify / Apple Music needs 0.3-0.5 Mbps",
             "No problem at all",
             "Music streaming uses almost no bandwidth on any connection",
             ACCENT, GOOD_BG),

            ("💾", "Game / File Download",
             "Steam / Epic / Windows Update use multiple connections",
             dl_time_str(50) + " for a 50 GB game",
             "Downloads run at ~70%% of your line speed  (%.0f Mbps)" % (peak_mbps * 0.7),
             fgdl, bgdl),

            ("👥", "Whole Household",
             "4K TV + video calls + gaming + browsing at the same time",
             "Handles %d people easily" % max(1, n_house) if n_house >= 1 else "May be tight",
             "100 Mbps recommended for a typical family of 4",
             fgho, bgho),
        ]

        tk.Label(self._scenario_frame,
                 text="Based on YOUR %.0f Mbps line — what you can actually do:" % peak_mbps,
                 bg=CARD, fg=ACCENT, font=(FNT,8,"bold")).grid(
                 row=0, column=0, columnspan=3, sticky="w", padx=8, pady=(6,4))

        for i, (icon, title, need_desc, big_line, detail_line, fg, bg) in enumerate(scenarios):
            col = i % 3
            row = (i // 3) + 1
            self._scenario_frame.columnconfigure(col, weight=1)

            card = tk.Frame(self._scenario_frame, bg=bg,
                             highlightbackground=fg, highlightthickness=1)
            card.grid(row=row, column=col, sticky="ew", padx=3, pady=3)

            tk.Label(card, text=icon + "  " + title, bg=bg, fg=TEXT,
                     font=(FNT2,9,"bold")).pack(anchor="w", padx=8, pady=(7,1))
            tk.Label(card, text=need_desc, bg=bg, fg=TEXT2,
                     font=(FNT2,7)).pack(anchor="w", padx=8)
            tk.Label(card, text=big_line, bg=bg, fg=fg,
                     font=(FNT2,11,"bold")).pack(anchor="w", padx=8, pady=(4,1))
            tk.Label(card, text=detail_line, bg=bg, fg=TEXT2,
                     font=(FNT2,7), wraplength=240).pack(anchor="w", padx=8, pady=(0,7))

        dv, du = fmt_speed(peak_mbps * 0.7)
        self.lbl_gap_explain.configure(
            text="Your line capacity is %.0f Mbps. "
                 "Downloads and streaming will use %.0f Mbps (%s %s effective). "
                 "Web browsing uses only one connection at a time, so TEST NOW is usually "
                 "lower than LINE SPEED — that is normal and does not mean your internet is slow." % (
                     peak_mbps, peak_mbps * 0.7, dv, du),
            fg=TEXT2)


if __name__ == "__main__":
    App().mainloop()
