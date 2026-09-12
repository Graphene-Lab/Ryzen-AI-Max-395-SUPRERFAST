#!/usr/bin/env python3
"""Sample the SUPERFAST engine once, and report on what has been sampled.

Live mode (the default, run every 30 s by superfast-monitor.timer) appends one
JSON object per sample to ~/.local/share/superfast-monitor/samples.jsonl.
Report mode (`superfast-monitor.py --report [hours]`, 24 by default) prints
what those samples say about queueing, cache behaviour and memory pressure.

It reads the engine's loopback /health and /cache, plus /proc and /sys. No API
key, no writes outside the monitor directory, and the engine it samples is the
active profile — whichever one that is.

Why it exists: the engine's own log says how long each request took, but not
how many were waiting. On a machine with several agents that is the difference
between a slow answer and a queued one, and only the second is a configuration
problem: `/health`'s `in_flight` and `queued` are the numbers to watch. The
figures this tool collected on the reference host are in the README under
"Many agents at once".
"""

import datetime
import glob
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

HOME = pathlib.Path.home()
DIR = HOME / ".local/share/superfast-monitor"
LOG = DIR / "samples.jsonl"
API = "http://127.0.0.1:8731"
ROTATE_BYTES = 20 * 1024 * 1024


def get(path):
    """Read one engine endpoint, or None when the engine is not serving."""
    try:
        with urllib.request.urlopen(API + path, timeout=15) as r:
            return json.load(r)
    except (urllib.error.URLError, OSError, ValueError):
        return None


def first_number(pattern):
    """First whitespace-separated integer in the first file matching pattern."""
    for p in sorted(glob.glob(pattern)):
        try:
            return int(open(p).read().split()[0])
        except (OSError, ValueError, IndexError):
            pass
    return None


def sample():
    s = {"t": datetime.datetime.now().isoformat(timespec="seconds")}

    h = get("/health")
    if h is None:
        return None
    for k in ("model", "busy", "in_flight", "busy_for_s", "queued", "slots",
              "kv_pool_positions", "max_tokens_cap"):
        s[k] = h.get(k)
    s["cache_enabled"] = (h.get("prompt_cache") or {}).get("enabled")

    # Older engines have no /cache. A sample without it is still useful.
    c = get("/cache") or {}
    for k in ("hits", "misses", "stores", "evicted", "entries", "cap_bytes",
              "reserved_bytes", "prompt_tokens_saved", "store_ms_total",
              "restore_ms_total", "refused", "hit_rate"):
        s["cache_" + k] = c.get(k)

    s["mem_available_mb"] = 0
    for line in open("/proc/meminfo"):
        if line.startswith("MemAvailable:"):
            s["mem_available_mb"] = int(line.split()[1]) // 1024
    s["zram_used_mb"] = (first_number("/sys/block/zram*/mm_stat") or 0) // (1024 * 1024)
    s["load1"] = round(os.getloadavg()[0], 2)
    s["gpu_busy"] = first_number("/sys/class/drm/card*/device/gpu_busy_percent")
    s["gtt_used_mb"] = (first_number("/sys/class/drm/card*/device/mem_info_gtt_used") or 0) // (1024 * 1024)
    s["gpu_temp_c"] = (first_number("/sys/class/drm/card*/device/hwmon/hwmon*/temp1_input") or 0) / 1000.0
    s["gpu_power_w"] = (first_number("/sys/class/drm/card*/device/hwmon/hwmon*/power1_average") or 0) / 1e6
    return s


def append(s):
    DIR.mkdir(parents=True, exist_ok=True)
    if LOG.exists() and LOG.stat().st_size > ROTATE_BYTES:
        LOG.replace(str(LOG) + ".1")
    with LOG.open("a") as f:
        f.write(json.dumps(s) + "\n")


def load(hours):
    if not LOG.exists():
        return []
    cut = datetime.datetime.now() - datetime.timedelta(hours=hours)
    rows = []
    for line in LOG.read_text(errors="replace").splitlines():
        try:
            r = json.loads(line)
            r["ts"] = datetime.datetime.fromisoformat(r["t"])
        except (ValueError, KeyError):
            continue
        if r["ts"] >= cut:
            rows.append(r)
    return rows


def report(hours):
    rows = load(hours)
    if not rows:
        print("no samples in the last %g h (%s)" % (hours, LOG))
        print("is the sampler running? systemctl --user status superfast-monitor.timer")
        return
    span = (rows[-1]["ts"] - rows[0]["ts"]).total_seconds() / 3600
    print("%d samples over %.1f h (%s -> %s)" % (len(rows), span, rows[0]["t"], rows[-1]["t"]))
    print("model %s, pool %s positions, %s slots" % (
        rows[-1].get("model"), rows[-1].get("kv_pool_positions"), rows[-1].get("slots")))

    def col(k):
        return [r[k] for r in rows if isinstance(r.get(k), (int, float))] or [0]

    infl = col("in_flight")
    queued = col("queued")
    print("\n--- load ---")
    print("samples with in_flight > 0: %d (%.1f%%), max %d, mean %.2f" % (
        sum(1 for v in infl if v), 100.0 * sum(1 for v in infl if v) / len(rows),
        max(infl), sum(infl) / len(infl)))
    print("samples with queued > 0:    %d (%.1f%%), max %d" % (
        sum(1 for v in queued if v), 100.0 * sum(1 for v in queued if v) / len(rows),
        max(queued)))
    print("peak busy_for_s: %.0f s (longest single request in flight)" % max(col("busy_for_s")))
    print("a request that is queued is waiting for room in the KV pool; queued > 0")
    print("for long stretches means the pool is smaller than the working set.")

    hits = col("cache_hits")
    misses = col("cache_misses")
    tot = (hits[-1] - hits[0]) + (misses[-1] - misses[0])
    print("\n--- prompt cache (delta over the window) ---")
    if tot:
        print("requests %d, hits %d, misses %d, hit rate %.2f%%" % (
            tot, hits[-1] - hits[0], misses[-1] - misses[0],
            100.0 * (hits[-1] - hits[0]) / tot))
    print("prompt tokens served from cache: %d" % (col("cache_prompt_tokens_saved")[-1] -
                                                   col("cache_prompt_tokens_saved")[0]))
    print("snapshots stored %d, evicted %d, entries now %s" % (
        col("cache_stores")[-1] - col("cache_stores")[0],
        col("cache_evicted")[-1] - col("cache_evicted")[0], rows[-1].get("cache_entries")))
    print("a restart of the profile empties this cache: the engine is new.")

    print("\n--- GPU and memory ---")
    print("gpu busy mean %.0f%%, max %d%%   gtt %d MB   temp %.0f C   power %.0f W" % (
        sum(col("gpu_busy")) / len(rows), max(col("gpu_busy")),
        col("gtt_used_mb")[-1], max(col("gpu_temp_c")), max(col("gpu_power_w"))))
    print("MemAvailable min %d MB (now %d MB)   zram %d MB   load1 max %.1f" % (
        min(col("mem_available_mb")), col("mem_available_mb")[-1],
        max(col("zram_used_mb")), max(col("load1"))))
    print("MemAvailable is not the room left for the pool: the kernel counts the")
    print("engine's locked weights as reclaimable file cache, and they are not.")

    print("\n--- the 12 busiest samples ---")
    for r in sorted(rows, key=lambda r: -((r.get("queued") or 0) * 1000 + (r.get("in_flight") or 0)))[:12]:
        print("  %s  in_flight %s  queued %s  busy_for %ss  mem_avail %s MB  gpu %s%%" % (
            r["t"], r.get("in_flight"), r.get("queued"), r.get("busy_for_s"),
            r.get("mem_available_mb"), r.get("gpu_busy")))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--report":
        report(float(sys.argv[2]) if len(sys.argv) > 2 else 24.0)
    else:
        one = sample()
        if one is None:
            # No profile is serving. That is a normal state (none selected, or
            # switching), not a failure: a timer that reports failed on an idle
            # machine is noise.
            print("no engine answering on %s; no sample taken" % API)
        else:
            append(one)
