"""Open-loop load generator with HdrHistogram recording (methodology §2, §5).

The cardinal rule: requests are issued on a fixed pre-computed schedule at a
target arrival rate; we do NOT wait for response i before sending i+1. Latency is
measured from each request's *intended* send time, so if the harness falls behind
schedule that lateness is counted in the latency (not discarded) -- this is what
avoids coordinated omission.

We also instrument the harness itself so we can prove it is not the bottleneck:
scheduler lateness (actual spawn time vs intended), max in-flight, and achieved
vs target throughput. If the generator can't keep its schedule, the run is
invalid and must be reported as such -- never silently trusted.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass

from hdrh.histogram import HdrHistogram

# Histogram range: 1 microsecond .. 60 seconds, 3 significant figures.
_HIST_MIN_US = 1
_HIST_MAX_US = 60_000_000
_HIST_SIGFIG = 3


def new_hist() -> HdrHistogram:
    return HdrHistogram(_HIST_MIN_US, _HIST_MAX_US, _HIST_SIGFIG)


@dataclass
class LoadResult:
    """Outcome of one open-loop run (one cell)."""

    latency_us: HdrHistogram  # measured request latencies (post-warmup)
    sched_lateness_us: HdrHistogram  # scheduler spawn lateness (harness health)
    target_rate: float  # requests/sec we aimed to send
    measured: int  # requests counted (post-warmup, successful)
    errors: int  # requests that raised
    measure_window_s: float  # wall-clock duration of the measured window
    max_inflight: int  # peak concurrent in-flight requests
    latency_by_tag: dict[str, HdrHistogram] | None = None  # per-category latencies

    @property
    def achieved_rate(self) -> float:
        return self.measured / self.measure_window_s if self.measure_window_s else 0.0

    @property
    def throughput_ratio(self) -> float:
        """Achieved / target. A large shortfall is the coordinated-omission tell."""
        return self.achieved_rate / self.target_rate if self.target_rate else 0.0


def _schedule(n: int, rate: float, poisson: bool, rng: random.Random) -> list[float]:
    """Relative intended send times (seconds from t0)."""
    if poisson:  # exponential inter-arrival gaps -> Poisson process
        out, t = [], 0.0
        for _ in range(n):
            t += rng.expovariate(rate)
            out.append(t)
        return out
    return [i / rate for i in range(n)]  # fixed interval


async def open_loop(
    request_fn,
    *,
    target_rate: float,
    n_requests: int,
    warmup_s: float = 0.0,
    poisson: bool = True,
    seed: int = 0,
    tags: list[str] | None = None,
) -> LoadResult:
    """Drive ``request_fn`` open-loop at ``target_rate`` for ``n_requests``.

    ``request_fn`` is an async callable taking the request index and returning
    anything (or raising). Latency is ``completed - intended_send_time``. Requests
    whose intended time is within the warmup window are issued but not recorded.
    ``tags`` (optional, per request) splits latency into per-category histograms.
    """
    rng = random.Random(seed)
    intended = _schedule(n_requests, target_rate, poisson, rng)

    latency = new_hist()
    lateness = new_hist()
    by_tag: dict[str, HdrHistogram] = {}
    tasks: list[asyncio.Task] = []
    state = {"inflight": 0, "max_inflight": 0, "measured": 0, "errors": 0}

    async def run_one(i: int, intended_rel: float) -> None:
        state["inflight"] += 1
        state["max_inflight"] = max(state["max_inflight"], state["inflight"])
        try:
            await request_fn(i)
            ok = True
        except Exception:
            ok = False
        finally:
            state["inflight"] -= 1
        done_rel = time.perf_counter() - t0
        if intended_rel < warmup_s:
            return  # warmup: issued but not measured
        if ok:
            # Latency from INTENDED send time -- includes any harness lateness.
            raw_us = round((done_rel - intended_rel) * 1_000_000)
            lat_us = min(max(1, raw_us), _HIST_MAX_US)
            latency.record_value(lat_us)
            if tags is not None:
                by_tag.setdefault(tags[i], new_hist()).record_value(lat_us)
            state["measured"] += 1
        else:
            state["errors"] += 1

    t0 = time.perf_counter()
    for i, intended_rel in enumerate(intended):
        now = time.perf_counter() - t0
        gap = intended_rel - now
        if gap > 0:
            await asyncio.sleep(gap)
        spawn_rel = time.perf_counter() - t0
        if intended_rel >= warmup_s:  # only count lateness in the measured window
            late_us = max(1, round((spawn_rel - intended_rel) * 1_000_000))
            lateness.record_value(min(late_us, _HIST_MAX_US))
        tasks.append(asyncio.create_task(run_one(i, intended_rel)))

    if tasks:
        await asyncio.gather(*tasks)

    # Measured window = from end of warmup to the last intended send.
    window = max(intended[-1] - warmup_s, 1e-9) if intended else 1e-9
    return LoadResult(
        latency_us=latency,
        sched_lateness_us=lateness,
        target_rate=target_rate,
        measured=state["measured"],
        errors=state["errors"],
        measure_window_s=window,
        max_inflight=state["max_inflight"],
        latency_by_tag=by_tag or None,
    )
