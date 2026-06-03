#!/usr/bin/env python3
"""Polls the deployed instance's /healthz and metric counters for ~5 minutes.

Exit codes:
    0   healthy
    1   the URL is reachable but /healthz reports degraded/down past --grace
    2   error rate (failed jobs / total) exceeds --max-error-rate during the window
    3   we could never reach the URL at all
    4   bad arguments

Used by the rollback workflow to decide whether to revert a deploy.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Optional

try:
    import urllib.request
    import urllib.error
except ImportError:  # pragma: no cover
    sys.exit("urllib not available")


def fetch(url: str, timeout: float = 5.0, headers: Optional[dict] = None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() if e.fp else b""
    except (urllib.error.URLError, TimeoutError) as e:
        return 0, str(e).encode()


def parse_prom(text: str) -> dict[str, float]:
    """Extract `name{labels} value` lines as a flat dict (sum across labels)."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        try:
            if "{" in line:
                name = line.split("{", 1)[0]
                value = float(line.rsplit(" ", 1)[1])
            else:
                parts = line.split()
                name, value = parts[0], float(parts[1])
            out[name] = out.get(name, 0.0) + value
        except (IndexError, ValueError):
            continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="Base URL of the deployed instance")
    ap.add_argument("--admin-token", default="", help="Admin bearer (for /metrics)")
    ap.add_argument("--timeout", type=int, default=300, help="Total seconds to watch")
    ap.add_argument("--grace", type=int, default=60, help="Seconds before degraded counts")
    ap.add_argument("--interval", type=int, default=10, help="Poll interval seconds")
    ap.add_argument("--max-error-rate", type=float, default=0.20,
                    help="Failed jobs / total above this triggers rollback")
    ap.add_argument("--min-samples", type=int, default=5,
                    help="Minimum jobs observed before computing error rate")
    args = ap.parse_args()

    deadline = time.time() + args.timeout
    grace_until = time.time() + args.grace
    headers = {"Authorization": f"Bearer {args.admin_token}"} if args.admin_token else {}

    first_total: Optional[float] = None
    first_failed: Optional[float] = None
    last_status = None
    last_health: dict = {}
    reachable_once = False

    while time.time() < deadline:
        # 1. /healthz
        status, body = fetch(f"{args.url}/healthz", timeout=5.0)
        last_status = status
        if status == 200:
            reachable_once = True
            try:
                last_health = json.loads(body.decode())
            except Exception:
                last_health = {"raw": body.decode(errors="replace")}
            print(f"[{int(time.time())}] /healthz={status} status={last_health.get('status')} "
                  f"q={last_health.get('queue_depth')} dlq={last_health.get('dead_letter')}")
            if last_health.get("status") != "ok" and time.time() > grace_until:
                print(f"::error::Degraded for >{args.grace}s: {last_health}")
                return 1
        else:
            print(f"[{int(time.time())}] /healthz={status} body={body[:200]!r}")
            if time.time() > grace_until and not reachable_once:
                print("::error::Never reached /healthz")
                return 3

        # 2. /metrics — compute error rate
        if args.admin_token:
            m_status, m_body = fetch(f"{args.url}/metrics", headers=headers, timeout=5.0)
            if m_status == 200:
                metrics = parse_prom(m_body.decode(errors="replace"))
                total = sum(v for k, v in metrics.items()
                            if k.startswith("deepagent_jobs_total"))
                # Re-parse to split by status — fetch was lossy on labels.
                # Cheap second pass:
                failed = 0.0
                for line in m_body.decode(errors="replace").splitlines():
                    if "deepagent_jobs_total" in line and (
                        'status="failed"' in line or 'status="dead"' in line
                    ):
                        try:
                            failed += float(line.rsplit(" ", 1)[1])
                        except ValueError:
                            continue
                if first_total is None:
                    first_total, first_failed = total, failed
                    print(f"[{int(time.time())}] baseline jobs={total:.0f} failed={failed:.0f}")
                else:
                    delta_total = total - first_total
                    delta_failed = failed - (first_failed or 0)
                    if delta_total >= args.min_samples:
                        rate = delta_failed / max(delta_total, 1)
                        print(f"[{int(time.time())}] +jobs={delta_total:.0f} "
                              f"+failed={delta_failed:.0f} rate={rate:.2%}")
                        if rate > args.max_error_rate:
                            print(f"::error::Error rate {rate:.2%} > {args.max_error_rate:.2%}")
                            return 2

        time.sleep(args.interval)

    if not reachable_once:
        print("::error::URL never responded 200 in the entire window")
        return 3

    print(f"::notice::Healthy after {args.timeout}s window. final={last_health}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
