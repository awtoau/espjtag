"""espjtag.timing — high-resolution, assume-nothing timing instrumentation.

The philosophy: measure EVERYTHING at the lowest level (every USB write/read,
every drain, every scan), with the highest-resolution clock available, and
attribute time to named buckets — so we never guess where the time goes. When
disabled (the default) it is zero-overhead (a single `is None` check).

    from espjtag import EspUsbJtag
    from espjtag.timing import Timer
    j = EspUsbJtag(path)
    j.timing = Timer()                 # turn on recording
    j.read_mem(0x42000000, 256)
    print(j.timing.report())           # per-bucket count / total / mean / min / max

Uses time.perf_counter_ns() — the highest-resolution monotonic clock Python
exposes (nanoseconds, not subject to NTP steps). We do NOT assume it's accurate
to the ns; report() shows the raw counts so you can judge resolution yourself.
"""
import time


class Timer:
    """Accumulates named timing spans. Thread-unsafe by design (one JTAG session
    is single-threaded); keep it that way for zero locking overhead."""

    def __init__(self):
        # bucket -> [count, total_ns, min_ns, max_ns]
        self._b = {}
        self.t0 = time.perf_counter_ns()
        self._events = []          # optional ordered (bucket, dt_ns) trace

    def add(self, bucket, dt_ns, trace=False):
        e = self._b.get(bucket)
        if e is None:
            self._b[bucket] = [1, dt_ns, dt_ns, dt_ns]
        else:
            e[0] += 1
            e[1] += dt_ns
            if dt_ns < e[2]:
                e[2] = dt_ns
            if dt_ns > e[3]:
                e[3] = dt_ns
        if trace:
            self._events.append((bucket, dt_ns))

    def span(self, bucket, trace=False):
        return _Span(self, bucket, trace)

    def buckets(self):
        return dict(self._b)

    def report(self, sort="total"):
        """Human report. Columns: count, total_ms, mean_us, min_us, max_us."""
        idx = {"total": 1, "count": 0, "mean": -1, "max": 3}.get(sort, 1)
        rows = sorted(self._b.items(),
                      key=lambda kv: (kv[1][1] / kv[1][0]) if idx == -1
                      else kv[1][idx], reverse=True)
        wall_ns = time.perf_counter_ns() - self.t0
        out = [f"  {'bucket':22}{'count':>8}{'total_ms':>11}"
               f"{'mean_us':>11}{'min_us':>10}{'max_us':>10}"]
        for name, (n, tot, mn, mx) in rows:
            out.append(f"  {name:22}{n:>8}{tot/1e6:>11.3f}"
                       f"{tot/n/1e3:>11.2f}{mn/1e3:>10.2f}{mx/1e3:>10.2f}")
        out.append(f"  {'(wall clock)':22}{'':>8}{wall_ns/1e6:>11.3f}")
        return "\n".join(out)

    def events(self):
        return list(self._events)


class _Span:
    __slots__ = ("t", "b", "name", "trace")

    def __init__(self, timer, name, trace):
        self.t = timer
        self.name = name
        self.trace = trace

    def __enter__(self):
        self.b = time.perf_counter_ns()
        return self

    def __exit__(self, *exc):
        self.t.add(self.name, time.perf_counter_ns() - self.b, self.trace)
        return False
