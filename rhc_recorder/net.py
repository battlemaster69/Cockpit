"""The one door every request goes through: a minimum gap per source, retries with backoff, a call count and a
hard cap per run. When a cap is hit, or a source keeps answering HTTP 429, that source is stopped for the rest of
the run (SourceStopped) and the run finishes with the data it has. It never loops or waits for hours.

Named net.py, not http.py: a module called http would hide Python's own http package, which requests needs."""

import time

import requests


class SourceStopped(Exception):
    """A source hit its cap or its 429 limit; callers keep what they have and move on."""


class Net:
    def __init__(self, cfg):
        self.gap, self.cap = cfg["min_gap_s"], cfg["max_calls_per_run"]
        self.retries, self.backoff, self.stop_after = cfg["max_retries"], cfg["backoff_s"], cfg["stop_after_429s"]
        self.wait_429 = cfg.get("wait_after_429_s", 30)
        self.calls = {s: 0 for s in self.cap}
        self.last, self.r429, self.stopped = {}, {}, {}
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "rhc-recorder/1.0 (github.com/battlemaster69/Cockpit)"

    def request(self, source, method, url, **kw):
        """JSON from one request. Retries network errors, 429s and 5xx; any other 4xx raises at once."""
        err = None
        for attempt in range(self.retries + 1):
            if source in self.stopped:
                raise SourceStopped(f"{source}: {self.stopped[source]}")
            if self.calls[source] >= self.cap[source]:
                self.stopped[source] = f"cap of {self.cap[source]} calls reached"
                raise SourceStopped(f"{source}: {self.stopped[source]}")
            wait = self.gap[source] - (time.time() - self.last.get(source, 0))
            if wait > 0:
                time.sleep(wait)
            self.last[source] = time.time()
            self.calls[source] += 1
            try:
                r = self.session.request(method, url, timeout=45, **kw)
            except requests.RequestException as e:
                err = str(e)[:200]
            else:
                if r.status_code == 429:
                    # 429s in a row (reset by any success): wait for the rate window to reset, stop after a few
                    self.r429[source] = self.r429.get(source, 0) + 1
                    if self.r429[source] >= self.stop_after:
                        self.stopped[source] = f"HTTP 429 x{self.r429[source]} in a row"
                        raise SourceStopped(f"{source}: {self.stopped[source]}")
                    time.sleep(self.wait_429 * self.r429[source])
                    continue
                elif r.status_code >= 500:
                    err = f"HTTP {r.status_code}"
                else:
                    r.raise_for_status()
                    self.r429[source] = 0
                    return r.json() if r.text.strip() else None
            if attempt < self.retries:
                time.sleep(self.backoff[min(attempt, len(self.backoff) - 1)])
        raise RuntimeError(f"{source}: {err} after {self.retries} retries")

    def stopped_note(self):
        return "; ".join(f"{s}: {why}" for s, why in self.stopped.items())
