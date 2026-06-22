"""Realistic mixed workload model (methodology §3).

~80% reads / ~18% writes / ~2% risky writes, drawn from a template set with a
*skewed* frequency so a few shapes are hot and there's a long tail -- this is
what makes the parse-cache hit rate realistic (target ~70-90%, measured and
reported). Bound parameters are inlined literals drawn from Zipfian
distributions over real id spaces, so:

* the full SQL strings have a long-tailed frequency -> a realistic parse-cache
  hit/miss mix (parameterized ``$1`` templates would be ~100% hot over ~10
  shapes -- dishonestly warm), and
* params vary across many ids/ranges -> many Postgres plans/pages, not one.

Reads hit read-only Pagila + large tables. Writes hit a dedicated
``bench_writes`` table the harness re-seeds per cell (so runs are repeatable and
real data never drifts). The 2% risky writes are *range* writes that trip the
gated simulation path in layer C.
"""

from __future__ import annotations

import bisect
import random
from dataclasses import dataclass

# Id spaces (match the seeded dataset; see db/ seed scripts).
_FILM_IDS = 1000
_CUSTOMER_IDS = 599
_SENSOR_IDS = 5000
BENCH_ROWS = 100_000  # rows seeded into bench_writes per cell


class Zipf:
    """Zipfian sampler over 1..n (skew s; higher = more concentrated)."""

    def __init__(self, n: int, s: float) -> None:
        cum, total = [], 0.0
        for k in range(1, n + 1):
            total += 1.0 / (k**s)
            cum.append(total)
        self._cum = cum
        self._total = total
        self._n = n

    def sample(self, rng: random.Random) -> int:
        return bisect.bisect_left(self._cum, rng.random() * self._total) + 1


@dataclass(frozen=True)
class Query:
    category: str  # "read" | "write" | "risky"
    sql: str


def _w(rng: random.Random) -> int:
    return rng.randint(0, 1_000_000)


class Workload:
    """Draws queries per the production-shaped mix. ``next()`` returns a Query."""

    def __init__(self, seed: int = 0, skew: float = 1.3) -> None:
        self._rng = random.Random(seed)
        self._film = Zipf(_FILM_IDS, skew)
        self._cust = Zipf(_CUSTOMER_IDS, skew)
        self._sensor = Zipf(_SENSOR_IDS, skew)
        self._row = Zipf(BENCH_ROWS, skew)
        # (category, weight, builder). Weights are themselves Zipf-ish and sum to
        # ~80/18/2; normalized in __init__.
        self._templates = [
            ("read", 0.30, self._film_point),
            ("read", 0.20, self._customer_point),
            ("read", 0.12, self._app_event_range),
            ("read", 0.10, self._cust_rental_join),
            ("read", 0.05, self._payment_agg),
            ("read", 0.03, self._metric_agg),
            ("write", 0.10, self._point_update),
            ("write", 0.05, self._point_delete),
            ("write", 0.03, self._point_insert),
            ("risky", 0.015, self._range_update),
            ("risky", 0.005, self._range_delete),
        ]
        total = sum(w for _, w, _ in self._templates)
        cum, acc = [], 0.0
        for _, w, _ in self._templates:
            acc += w / total
            cum.append(acc)
        self._cum = cum

    def next(self) -> Query:
        r = self._rng.random()
        idx = bisect.bisect_left(self._cum, r)
        idx = min(idx, len(self._templates) - 1)
        category, _, builder = self._templates[idx]
        return Query(category, builder())

    # --- read templates ------------------------------------------------------
    def _film_point(self) -> str:
        return (
            "SELECT film_id, title, rental_rate FROM film "
            f"WHERE film_id = {self._film.sample(self._rng)}"
        )

    def _customer_point(self) -> str:
        return (
            "SELECT customer_id, first_name, last_name, email FROM customer "
            f"WHERE customer_id = {self._cust.sample(self._rng)}"
        )

    def _app_event_range(self) -> str:
        return (
            "SELECT event_id, event_type, amount FROM app_event "
            f"WHERE customer_id = {self._cust.sample(self._rng)} "
            "ORDER BY created_at DESC LIMIT 20"
        )

    def _cust_rental_join(self) -> str:
        return (
            "SELECT c.first_name, r.rental_date FROM customer c "
            "JOIN rental r ON c.customer_id = r.customer_id "
            f"WHERE c.customer_id = {self._cust.sample(self._rng)} LIMIT 50"
        )

    def _payment_agg(self) -> str:
        thresh = self._rng.choice([50, 100, 150, 200])
        return (
            "SELECT customer_id, sum(amount), count(*) FROM payment "
            f"GROUP BY customer_id HAVING sum(amount) > {thresh} LIMIT 100"
        )

    def _metric_agg(self) -> str:
        return (
            "SELECT sensor_id, avg(value), count(*) FROM metric_sample "
            f"WHERE sensor_id = {self._sensor.sample(self._rng)} GROUP BY sensor_id"
        )

    # --- write templates (point writes -> pass policy, not simulated) --------
    def _point_update(self) -> str:
        return (
            f"UPDATE bench_writes SET val = {_w(self._rng)}, updated_at = now() "
            f"WHERE id = {self._row.sample(self._rng)}"
        )

    def _point_delete(self) -> str:
        return f"DELETE FROM bench_writes WHERE id = {self._row.sample(self._rng)}"

    def _point_insert(self) -> str:
        return (
            "INSERT INTO bench_writes (val, tag) VALUES " f"({_w(self._rng)}, 'bench')"
        )

    # --- risky writes (range -> trip the gated simulation path in C) ---------
    def _range_update(self) -> str:
        lo = self._row.sample(self._rng)
        return (
            "UPDATE bench_writes SET val = val "  # no-op value; still a real write
            f"WHERE id BETWEEN {lo} AND {lo + 200}"
        )

    def _range_delete(self) -> str:
        lo = self._row.sample(self._rng)
        return f"DELETE FROM bench_writes WHERE id BETWEEN {lo} AND {lo + 150}"


# The pure-overhead workload (methodology §6 view 1): trivial query where DB
# time ~= 0, so the A->C delta is almost entirely engine cost.
def trivial_query() -> Query:
    return Query("read", "SELECT 1")
