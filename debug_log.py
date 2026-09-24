"""
The server's last 5 minutes, kept in memory for the reader's debug-log button
(POST /api/admin/debug-log in main.py writes it to {data_path}/debug_logs/).

Three sources, every line stamped in UTC to the millisecond so it lines up
with the device's own log file:
  - everything printed to stdout/stderr, plus uvicorn's own error logging
  - one line when each HTTP request arrives and one when its response has
    been fully sent (status, bytes, time to headers, total time) -- a request
    that never gets its second line was never answered
  - a resource sample every SAMPLE_INTERVAL_SECONDS: NAS CPU/iowait/RAM/swap,
    this process's CPU and RSS, and the container's cgroup memory and CPU
    throttling (the CPU cap has caused slowness here before)
"""
import collections
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone

WINDOW_SECONDS = 300
SAMPLE_INTERVAL_SECONDS = 2

_lines = collections.deque()  # (unix time, text)
_lock = threading.Lock()
_installed = False


def ts(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def log(msg: str, t: float = None):
    t = time.time() if t is None else t
    with _lock:
        _lines.append((t, msg))
        cutoff = time.time() - WINDOW_SECONDS
        while _lines and _lines[0][0] < cutoff:
            _lines.popleft()


def dump() -> str:
    cutoff = time.time() - WINDOW_SECONDS
    with _lock:
        items = [x for x in _lines if x[0] >= cutoff]
    items.sort(key=lambda x: x[0])
    return "\n".join(f"{ts(t)} {m}" for t, m in items)


# ── stdout / stderr / logging capture ──

class _Tee:
    """Passes writes through to the real stream and records complete lines."""
    def __init__(self, stream, tag):
        self._stream = stream
        self._tag = tag
        self._buf = ""
        self._buf_lock = threading.Lock()

    def write(self, s):
        n = self._stream.write(s)
        with self._buf_lock:
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line.strip():
                    log(f"[{self._tag}] {line}")
        return n

    def flush(self):
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


class _Handler(logging.Handler):
    def emit(self, record):
        try:
            log(f"[{record.name} {record.levelname}] {self.format(record)}", record.created)
        except Exception:
            pass


def install():
    """Start capturing output. Safe to call more than once (python main.py
    imports main.py twice: as __main__ and again as uvicorn's "main:app")."""
    global _installed
    if _installed:
        return
    _installed = True
    sys.stdout = _Tee(sys.stdout, "out")
    sys.stderr = _Tee(sys.stderr, "err")
    # uvicorn's own handlers keep the original streams, so its error log
    # (tracebacks from failed requests) is captured here instead. Its access
    # log isn't needed: RequestLogMiddleware logs every request in more detail.
    handler = _Handler()
    logging.getLogger("uvicorn").addHandler(handler)
    logging.getLogger().addHandler(handler)


# ── request log ──

class RequestLogMiddleware:
    """Plain ASGI middleware: sees the real end of the response body, which a
    BaseHTTPMiddleware can't. Also sends Timing-Allow-Origin so the device's
    own network timings (sizes, status) aren't blanked for the native app,
    which fetches from a different origin."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        t0 = time.time()
        qs = scope.get("query_string", b"")
        target = scope.get("path", "") + ("?" + qs.decode("latin-1") if qs else "")
        method = scope.get("method", "?")
        client = scope.get("client")
        log(f"[req] -> {method} {target} from {client[0] if client else '?'}", t0)
        state = {"status": None, "bytes": 0, "t_headers": None, "done": False}

        async def send_wrapper(message):
            kind = message["type"]
            if kind == "http.response.start":
                state["status"] = message["status"]
                state["t_headers"] = time.time()
                message = {**message, "headers": list(message.get("headers", [])) + [(b"timing-allow-origin", b"*")]}
            elif kind == "http.response.body":
                state["bytes"] += len(message.get("body", b""))
                if not message.get("more_body", False):
                    state["done"] = True
            elif kind in ("http.response.pathsend", "http.response.zerocopysend"):
                state["done"] = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            t1 = time.time()
            headers_ms = f"{(state['t_headers'] - t0) * 1000:.0f}ms" if state["t_headers"] else "-"
            end = "" if state["done"] else "  INCOMPLETE (client went away or the server failed mid-response)"
            log(f"[req] <- {method} {target} {state['status']} {state['bytes']}B "
                f"headers={headers_ms} total={(t1 - t0) * 1000:.0f}ms{end}")


# ── resource sampler ──

def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def _meminfo():
    text = _read("/proc/meminfo")
    if not text:
        return None
    out = {}
    for line in text.splitlines():
        k, _, v = line.partition(":")
        out[k] = int(v.split()[0]) * 1024  # kB -> bytes
    return out


def _cpu_times():
    """(total, idle, iowait) jiffies for the whole NAS, from /proc/stat."""
    text = _read("/proc/stat")
    if not text:
        return None
    f = [int(x) for x in text.splitlines()[0].split()[1:]]
    return sum(f), f[3] + f[4], f[4]


def _cgroup_cpu():
    text = _read("/sys/fs/cgroup/cpu.stat")
    if not text:
        return None
    return {k: int(v) for k, v in (line.split() for line in text.splitlines() if line.strip())}


def _rss():
    text = _read("/proc/self/status")
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) * 1024
    return None


def _gb(n):
    return f"{n / 1024**3:.2f}GB"


def _mb(n):
    return f"{n / 1024**2:.0f}MB"


def _sampler():
    prev_cpu = _cpu_times()
    prev_proc = (time.time(), time.process_time())
    prev_cg = _cgroup_cpu()
    while True:
        time.sleep(SAMPLE_INTERVAL_SECONDS)
        try:
            parts = []
            cpu = _cpu_times()
            if cpu and prev_cpu and cpu[0] > prev_cpu[0]:
                dt = cpu[0] - prev_cpu[0]
                busy = 100 * (dt - (cpu[1] - prev_cpu[1])) / dt
                iowait = 100 * (cpu[2] - prev_cpu[2]) / dt
                parts.append(f"NAS cpu {busy:.1f}% iowait {iowait:.1f}%")
            prev_cpu = cpu
            if hasattr(os, "getloadavg"):
                parts.append("load " + " ".join(f"{x:.2f}" for x in os.getloadavg()))
            mem = _meminfo()
            if mem:
                total, avail = mem.get("MemTotal", 0), mem.get("MemAvailable", 0)
                swap_used = mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)
                parts.append(f"NAS ram used {_gb(total - avail)}/{_gb(total)} avail {_gb(avail)} "
                             f"swap {_gb(swap_used)}/{_gb(mem.get('SwapTotal', 0))}")
            now = (time.time(), time.process_time())
            proc_pct = 100 * (now[1] - prev_proc[1]) / max(1e-6, now[0] - prev_proc[0])
            prev_proc = now
            rss = _rss()
            parts.append(f"kinsho cpu {proc_pct:.1f}% of one core" + (f" rss {_mb(rss)}" if rss else ""))
            cg_mem = _read("/sys/fs/cgroup/memory.current")
            if cg_mem:
                parts.append(f"container mem {_mb(int(cg_mem))}")
            cg = _cgroup_cpu()
            if cg and prev_cg:
                parts.append(f"throttled +{cg.get('nr_throttled', 0) - prev_cg.get('nr_throttled', 0)} periods "
                             f"+{(cg.get('throttled_usec', 0) - prev_cg.get('throttled_usec', 0)) / 1000:.0f}ms")
            prev_cg = cg
            log("[res] " + " | ".join(parts))
        except Exception as e:
            log(f"[res] sample failed: {e!r}")


def start_sampler():
    threading.Thread(target=_sampler, name="debug-log-sampler", daemon=True).start()
