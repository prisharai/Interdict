"""Asynchronous, non-blocking audit log.

A query must NEVER wait on a log write. This log is also the
traffic corpus -- a record of real agent-generated SQL paired, where possible,
with the agent's stated task -- which feeds the red/green corpora and
intent-mismatch detection.

Design for the latency budget:

* ``record()`` is a *synchronous, non-blocking* call. It does one cheap thing --
  ``Queue.put_nowait`` -- and returns. It never awaits and never touches disk,
  so a query the agent is waiting on never pays for logging. The query path
  calls this and moves on.
* A single background consumer task drains the queue and writes JSONL. The
  actual disk write happens in a thread (``asyncio.to_thread``) so it never
  blocks the event loop, and therefore never adds latency to *other* in-flight
  queries either.
* If the queue is full we DROP the record (and count it) rather than block.
  Logging is fail-open by design: losing an audit line must never stall or fail
  a query. The dropped-count surfaces the rare case where the writer can't keep
  up so it's visible rather than silent.

When a separate control pool is supplied, each already-redacted event is also
written to an append-only table there.  The query path still only enqueues.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from engine.schema import PrincipalKind, principal_from_legacy
from engine.security import audit_safe

# Bound the queue so a misbehaving/backed-up writer can't grow memory without
# limit. Generously sized: at this depth we'd rather drop+count than block.
_DEFAULT_MAX_QUEUE = 10_000


class AuditLog:
    """Append-only, async JSONL audit log.

    Lifecycle: ``await start()`` once, ``record(...)`` per statement (cheap,
    sync, non-blocking), ``await stop()`` on shutdown to flush the tail.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_queue: int = _DEFAULT_MAX_QUEUE,
        control_pool=None,
        control_schema: str = "interdict_control",
    ) -> None:
        self._path = Path(path)
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max_queue)
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._control_pool = control_pool
        self._control_schema = control_schema
        # Observability for the fail-open drop path.
        self.dropped = 0
        self.last_drop_ts: float | None = None
        self.control_write_errors = 0

    @property
    def _control_table(self) -> str:
        schema = '"' + self._control_schema.replace('"', '""') + '"'
        return f"{schema}.audit_event"

    async def start(self) -> None:
        """Open the log file and launch the background consumer."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Touch the file so it exists even before the first flush.
        self._path.touch(exist_ok=True)
        if self._control_pool is not None:
            schema = '"' + self._control_schema.replace('"', '""') + '"'
            async with self._control_pool.acquire() as conn:
                await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
                await conn.execute(
                    f"""CREATE TABLE IF NOT EXISTS {self._control_table} (
                        event_id bigserial PRIMARY KEY,
                        recorded_at timestamptz NOT NULL DEFAULT now(),
                        payload jsonb NOT NULL
                    )"""
                )
        self._stopping = False
        self._task = asyncio.create_task(self._consume(), name="audit-consumer")

    def record(self, entry: dict[str, Any]) -> None:
        """Enqueue one audit entry. Synchronous, non-blocking, hot-path-safe.

        Stamps a wall-clock timestamp if the caller didn't. On a full queue the
        entry is dropped and counted -- never blocks, never raises onto the
        query path.
        """
        entry.setdefault("ts", time.time())
        entry.setdefault("principal", _principal_for_audit(entry))
        entry = audit_safe(entry)
        try:
            self._queue.put_nowait(entry)
        except asyncio.QueueFull:
            self.dropped += 1
            self.last_drop_ts = time.time()

    def status(self) -> dict[str, Any]:
        """Runtime health for the fail-open audit path."""
        return {
            "path": str(self._path),
            "queue_depth": self._queue.qsize(),
            "queue_max": self._queue.maxsize,
            "dropped": self.dropped,
            "last_drop_ts": self.last_drop_ts,
            "running": self._task is not None and not self._task.done(),
            "durable_control_store": self._control_pool is not None,
            "control_write_errors": self.control_write_errors,
        }

    async def _consume(self) -> None:
        """Drain the queue and append entries to the JSONL file in batches."""
        while True:
            try:
                first = await self._queue.get()
            except asyncio.CancelledError:
                return
            # Coalesce everything else already queued into one disk write to
            # keep executor/syscall overhead low under bursty traffic.
            batch = [first]
            while True:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                await self._flush_batch(batch)
            finally:
                for _ in batch:
                    self._queue.task_done()

    async def _flush_batch(self, batch: list[dict[str, Any]]) -> None:
        try:
            await asyncio.to_thread(self._write_batch, batch)
        except OSError:
            # Disk gone read-only/full/deleted: logging is fail-open, so count
            # the loss and keep the consumer alive.
            self.dropped += len(batch)
            self.last_drop_ts = time.time()
        if self._control_pool is not None:
            try:
                payloads = [(json.dumps(entry, default=str),) for entry in batch]
                async with self._control_pool.acquire() as conn:
                    await conn.executemany(
                        f"INSERT INTO {self._control_table} (payload) "
                        "VALUES ($1::jsonb)",
                        payloads,
                    )
            except Exception:
                # The local JSONL remains a second sink. Surface durable-store
                # failures without putting database availability on the query path.
                self.control_write_errors += len(batch)

    def _write_batch(self, batch: list[dict[str, Any]]) -> None:
        """Blocking disk write; runs in a worker thread, off the event loop."""
        lines = "".join(json.dumps(e, default=str) + "\n" for e in batch)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(lines)

    async def stop(self) -> None:
        """Flush remaining entries and stop the consumer cleanly."""
        if self._task is None:
            return
        self._stopping = True
        # Let the consumer finish every queued event before cancellation.
        await self._queue.join()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None


def _principal_for_audit(entry: dict[str, Any]) -> dict[str, Any]:
    """Derive a v2 principal for legacy audit call sites."""
    existing = entry.get("principal")
    if isinstance(existing, dict):
        return existing

    identity = entry.get("agent") or entry.get("actor") or entry.get("operator")
    kind = (
        PrincipalKind.HUMAN.value
        if entry.get("operator")
        else PrincipalKind.AGENT.value
    )
    return principal_from_legacy(
        identity,
        kind=kind,
        stated_task=entry.get("stated_task"),
    ).to_dict()
