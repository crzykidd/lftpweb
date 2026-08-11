"""The one WebSocket (DESIGN.md §2, §9): a full model snapshot on connect, deltas thereafter.

"Delta" here means "the fresh reconciled state for one queue" — published by
`core/engine.py` every time that queue finishes a scan — not a row-level diff against the
previous snapshot. The client (`frontend/src/hooks/useFilesSocket.ts`) merges each
`queue_snapshot` into its local map keyed by `queue_id`, so a queue that hasn't rescanned yet
keeps showing its last-known state. See docs/decisions.md for why this reading of "delta" was
chosen for phase 2.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/api/ws")
async def files_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    engine = websocket.app.state.engine
    subscription = engine.events.subscribe()

    try:
        await websocket.send_json({"type": "snapshot", "queues": engine.snapshot()})

        async def sender() -> None:
            while True:
                message = await subscription.get()
                await websocket.send_json(message)

        async def receiver() -> None:
            # The client sends nothing meaningful today; this exists purely so a client
            # disconnect is detected promptly instead of only surfacing on the next send.
            while True:
                await websocket.receive_text()

        sender_task = asyncio.create_task(sender())
        receiver_task = asyncio.create_task(receiver())
        try:
            done, pending = await asyncio.wait(
                {sender_task, receiver_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            for task in done:
                exc = task.exception()
                if exc is not None and not isinstance(exc, WebSocketDisconnect):
                    raise exc
        finally:
            sender_task.cancel()
            receiver_task.cancel()
    except WebSocketDisconnect:
        pass
    finally:
        engine.events.unsubscribe(subscription)
