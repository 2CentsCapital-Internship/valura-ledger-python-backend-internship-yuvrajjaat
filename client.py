#!/usr/bin/env python3
"""A working arena client. It connects, it survives, it scores almost nothing.

Everything here is transport: subscribing to the stream, surviving the replay,
resuming from an offset, batching postings, answering checkpoints on time. It is
given to you finished so you can spend your time on the ledger instead, which is
the part we are actually assessing.

The one thing it does not do is keep a book. `book.py` is where you come in.

    pip install -r requirements.txt
    python client.py --url https://hiring-arena.twocc.in --key ak_... --mode practice

Read PROTOCOL.md first. It is the whole specification.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import httpx

from book import Book


def _log(path: str, obj) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, default=str) + "\n")


class ArenaClient:
    def __init__(self, url: str, key: str, mode: str,
                 batch: int = 100, flush_ms: int = 400,
                 debug: bool = False) -> None:
        self.url = url.rstrip("/")
        self.key = key
        self.mode = mode
        self.batch = batch
        self.flush_ms = flush_ms
        self.debug = debug
        self.book = Book()
        self.pending: list[dict] = []
        self.cursor = 0
        self.started = False
        self.stats = {"events": 0, "posted": 0, "checkpoints": 0,
                      "reconnects": 0, "resets": 0, "errors": 0}
        self.done = False

    # -- submitting ---------------------------------------------------------
    def flush(self, http: httpx.Client) -> None:
        """Postings go up in batches. One request per event works and is slow;
        at the burst rate it will put you behind the stream."""
        if not self.pending:
            return
        body, self.pending = {"postings": self.pending[:500]}, self.pending[500:]
        try:
            r = http.post(f"{self.url}/v1/postings", params={"mode": self.mode},
                          json=body, timeout=30)
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", 5)))
                self.pending = body["postings"] + self.pending
                return
            r.raise_for_status()
            self.stats["posted"] += len(body["postings"])
            if self.debug:
                try:
                    _log("debug_feedback.jsonl", r.json())
                except Exception:
                    pass
        except httpx.HTTPError:
            self.stats["errors"] += 1
            self.pending = body["postings"] + self.pending
            time.sleep(1)

    def checkpoint(self, http: httpx.Client, payload: dict) -> None:
        """Snapshot FIRST, send second.

        The reply must describe your book as at the checkpoint's place in the
        stream. Taking the snapshot after the network round trip, or from
        another thread while the stream keeps running, reports a later state
        than the one being asked about.
        """
        cp_id = payload["checkpoint_id"]
        snap = self.book.snapshot(as_of_event_id=payload.get("as_of_event_id"))
        self.flush(http)
        try:
            r = http.post(f"{self.url}/v1/checkpoint", params={"mode": self.mode},
                          json={"checkpoint_id": cp_id, **snap}, timeout=30)
            self.stats["checkpoints"] += 1
            if self.debug:
                try:
                    _log("debug_checkpoint.jsonl",
                         {"checkpoint_id": cp_id, "sent": snap, "resp": r.json()})
                except Exception:
                    pass
        except httpx.HTTPError:
            self.stats["errors"] += 1

    # -- consuming ----------------------------------------------------------
    def handle(self, ev: dict) -> None:
        legs = self.book.apply(ev)
        # An event you correctly reject still needs a submission, with no legs.
        self.pending.append({"event_id": ev["event_id"], "legs": legs or []})
        self.stats["events"] += 1
        if self.debug:
            _log("debug_events.jsonl",
                 {"offset": ev.get("offset"), "type": ev.get("type"),
                  "event_id": ev.get("event_id"),
                  "backdated_days": ev.get("backdated_days"),
                  "payload": ev.get("payload"), "our_legs": legs or []})

    def consume(self, http: httpx.Client, deadline: float) -> None:
        params = {"mode": self.mode, "from": self.cursor}
        if self.mode != "practice" and not self.started:
            params["new"] = "true"
        last_flush = time.time()
        with http.stream("GET", f"{self.url}/v1/stream", params=params,
                         timeout=httpx.Timeout(None, connect=20)) as r:
            r.raise_for_status()
            self.started = True
            etype = data = None
            for line in r.iter_lines():
                if time.time() > deadline:
                    return
                if line.startswith("event:"):
                    etype = line[6:].strip()
                elif line.startswith("data:"):
                    data = line[5:].strip()
                elif line == "" and data is not None:
                    ev = json.loads(data)

                    if etype == "stream_open":
                        nxt = ev.get("next_event_in_seconds")
                        print(f"  connected: run {ev.get('run_id')}, "
                              f"resumed at {ev.get('resumed_from')}, "
                              f"next event in {nxt}s", flush=True)
                    elif etype == "stream_reset":
                        # The server deliberately rewinds you and re-sends
                        # events you have already seen. Reconnect and carry on:
                        # if your book is idempotent this costs you nothing.
                        self.cursor = ev.get("resume_from", self.cursor)
                        self.stats["resets"] += 1
                        self.flush(http)
                        return
                    elif etype == "stream_end":
                        self.flush(http)
                        self.done = True
                        return
                    else:
                        self.cursor = max(self.cursor, ev.get("offset", 0) + 1)
                        if ev["type"] == "checkpoint_request":
                            if self.debug:
                                _log("debug_checkpoint_req.jsonl", ev["payload"])
                            self.checkpoint(http, ev["payload"])
                        else:
                            self.handle(ev)

                    if (len(self.pending) >= self.batch
                            or (time.time() - last_flush) * 1000 > self.flush_ms):
                        self.flush(http)
                        last_flush = time.time()
                    etype = data = None

    def run(self, max_seconds: float) -> dict:
        deadline = time.time() + max_seconds
        headers = {"Authorization": f"Bearer {self.key}"}
        with httpx.Client(headers=headers) as http:
            while time.time() < deadline and not self.done:
                try:
                    self.consume(http, deadline)
                except httpx.HTTPError as exc:
                    self.stats["reconnects"] += 1
                    print(f"  reconnecting after {type(exc).__name__}", flush=True)
                    time.sleep(1)
            self.flush(http)
            try:
                me = http.get(f"{self.url}/v1/me", params={"mode": self.mode},
                              timeout=20).json()
            except httpx.HTTPError:
                me = {}
        return {"stats": self.stats, "me": me}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://hiring-arena.twocc.in")
    ap.add_argument("--key", help="your API key (falls back to key.txt)")
    ap.add_argument("--mode", default="practice",
                    choices=["practice", "submission", "final"])
    ap.add_argument("--seconds", type=float, default=1500)
    ap.add_argument("--debug", action="store_true",
                    help="log events/feedback to debug_*.jsonl (dev only)")
    a = ap.parse_args()

    key = a.key
    if not key:
        try:
            key = open("key.txt", encoding="utf-8").read().strip()
        except OSError:
            pass
    if not key:
        ap.error("no API key: pass --key or put it in key.txt")

    if a.mode != "practice":
        print(f"\n  You are about to start a {a.mode.upper()} run.")
        print("  Attempts are limited and this one will count.")
        if input("  Type the mode name to continue: ").strip() != a.mode:
            print("  Cancelled.")
            return 1

    c = ArenaClient(a.url, key, a.mode, debug=a.debug)
    print(f"connecting to {a.url} as {a.mode} ...", flush=True)
    out = c.run(a.seconds)
    print("\nstats:", json.dumps(out["stats"]))
    todo = getattr(c.book, "todo", {})
    if todo:
        print(f"\nnot implemented yet ({sum(todo.values())} events skipped):")
        for t, n in sorted(todo.items(), key=lambda kv: -kv[1]):
            print(f"  {t:<30} {n:>5} events")

    me = out.get("me") or {}
    if me.get("score") is not None:
        print(f"score: {me['score']}")
        for k, v in (me.get("breakdown") or {}).items():
            print(f"  {k:<26} {v['points']:>6} / {v['max']}")
    else:
        print("score: withheld on this tier")
    return 0


if __name__ == "__main__":
    sys.exit(main())
