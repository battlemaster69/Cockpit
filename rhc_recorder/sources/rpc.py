"""Robinhood Chain's official RPC node (no key). Stands in for Blockscout, whose API sits behind a Cloudflare bot
check. Limits found 2026-10-05: eth_getLogs returns at most 10,000 results and spans at most 10,000,000 blocks
(about 11.6 days at 0.1 s blocks); batched requests are accepted."""

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
MAX_SPAN = 9_900_000


class TooManyLogs(Exception):
    """A token's transfer history needs more queries than allowed."""


class RPC:
    def __init__(self, net, url):
        self.net, self.url, self._bt = net, url, None

    def call(self, method, params):
        r = self.net.request("rpc", "POST", self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}) or {}
        if "error" in r:
            raise RuntimeError(f"{method}: {r['error'].get('message', r['error'])}")
        return r.get("result")

    def batch(self, calls):
        """[(method, params)] -> results in order (None where a call failed). One HTTP request per 100 calls."""
        out = []
        for i in range(0, len(calls), 100):
            chunk = calls[i:i + 100]
            r = self.net.request("rpc", "POST", self.url, json=[{"jsonrpc": "2.0", "id": j, "method": m, "params": p}
                                                                  for j, (m, p) in enumerate(chunk)]) or []
            by = {x.get("id"): x.get("result") for x in r} if isinstance(r, list) else {}
            out += [by.get(j) for j in range(len(chunk))]
        return out

    def head(self):
        return int(self.call("eth_blockNumber", []), 16)

    def block_time(self, n):
        return int(self.call("eth_getBlockByNumber", [hex(n), False])["timestamp"], 16)

    def block_at(self, ts):
        """The block nearest a unix time: estimated from the average block time, then refined twice."""
        head = self.head()
        head_ts = self.block_time(head)
        if self._bt is None:
            self._bt = max(0.01, (head_ts - self.block_time(max(1, head - 1_000_000))) / 1_000_000)
        n = max(1, min(head, head - int((head_ts - ts) / self._bt)))
        for _ in range(2):
            n = max(1, min(head, n - int((self.block_time(n) - ts) / self._bt)))
        return n, head

    def codes(self, addresses):
        res = self.batch([("eth_getCode", [a, "latest"]) for a in addresses])
        return {a: (c or "0x") for a, c in zip(addresses, res)}

    def transfers(self, token, start, end, max_calls, target=8000):
        """Every Transfer log of `token` between two blocks, as (block, from, to, amount). Scans forward in windows
        sized from the last window's density (aiming at `target` results a call, under the node's 10,000), and
        halves a window the node refuses as too full."""
        out, a, span, calls = [], start, MAX_SPAN, 0
        while a <= end:
            b = min(end, a + span - 1)
            calls += 1
            if calls > max_calls:
                raise TooManyLogs(f"over {max_calls} log queries")
            try:
                logs = self.call("eth_getLogs", [{"address": token, "topics": [TRANSFER], "fromBlock": hex(a), "toBlock": hex(b)}]) or []
            except RuntimeError as e:
                if ("exceeds" in str(e) or "limit" in str(e)) and b > a:
                    span = max(1, (b - a + 1) // 4)
                    continue
                raise
            for lg in logs:
                t = lg.get("topics") or []
                if len(t) == 3:  # ERC-20 Transfer (ERC-721 puts the amount in a 4th topic)
                    out.append((int(lg["blockNumber"], 16), "0x" + t[1][-40:], "0x" + t[2][-40:], int(lg["data"], 16) if lg.get("data") not in (None, "0x") else 0))
            width = b - a + 1
            span = min(MAX_SPAN, max(1, int(width * target / len(logs)))) if logs else min(MAX_SPAN, width * 4)
            a = b + 1
        return out
