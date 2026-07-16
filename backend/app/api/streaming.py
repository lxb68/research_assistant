"""Bounded bridge for exposing synchronous worker progress as NDJSON."""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Callable, Iterator

from fastapi.responses import StreamingResponse


StreamProducer = Callable[[Callable[[dict], None]], None]


def ndjson_worker_response(producer: StreamProducer, *, queue_size: int = 256) -> StreamingResponse:
    """Run one producer in a daemon worker and stream its events with bounded buffering."""
    def event_stream() -> Iterator[str]:
        events: queue.Queue[dict] = queue.Queue(maxsize=max(1, queue_size))

        def emit(event: dict) -> None:
            events.put(event)

        def run() -> None:
            try:
                producer(emit)
            except Exception as error:
                emit({"type": "error", "message": str(error)})
            finally:
                emit({"type": "done"})

        threading.Thread(target=run, daemon=True, name="api-stream-worker").start()
        while True:
            event = events.get()
            yield json.dumps(event, ensure_ascii=False) + "\n"
            if event.get("type") == "done":
                return

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


__all__ = ["ndjson_worker_response"]
