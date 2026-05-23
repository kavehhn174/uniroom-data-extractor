"""Serial photo processing queue for the Telegram bot."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from process_bulletin_board import process_image, resolve_nim_timeout

if TYPE_CHECKING:
    from telegram import Bot

log = logging.getLogger("uniroom.telegram.queue")

HEARTBEAT_INTERVAL_SEC = int(os.environ.get("TELEGRAM_HEARTBEAT_SEC", "45"))
# Extra slack for compression, MongoDB, etc.
PROCESS_TIMEOUT_SLACK_SEC = int(os.environ.get("TELEGRAM_PROCESS_SLACK_SEC", "120"))


@dataclass
class PhotoJob:
    chat_id: int
    path: Path
    user_message_id: int
    status_message_id: int


@dataclass
class QueueStatus:
    processing: PhotoJob | None
    waiting: int
    total_pending: int


class PhotoProcessingQueue:
    """Process one photo at a time; additional uploads wait in line."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot
        self._queue: asyncio.Queue[PhotoJob | None] = asyncio.Queue()
        self._processing: PhotoJob | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()

    @property
    def status(self) -> QueueStatus:
        waiting = self._queue.qsize()
        processing = self._processing is not None
        return QueueStatus(
            processing=self._processing,
            waiting=waiting,
            total_pending=waiting + (1 if processing else 0),
        )

    async def start(self) -> None:
        if self._worker_task is not None:
            return
        self._stopped.clear()
        self._worker_task = asyncio.create_task(
            self._worker(), name="telegram-photo-queue"
        )
        log.info("Photo processing queue started")

    async def stop(self) -> None:
        self._stopped.set()
        await self._queue.put(None)
        if self._worker_task is not None:
            await self._worker_task
            self._worker_task = None
        log.info("Photo processing queue stopped")

    def queue_position_for_new_job(self) -> int:
        """1-based position when enqueueing right now."""
        return self._queue.qsize() + (1 if self._processing else 0) + 1

    async def enqueue(self, job: PhotoJob) -> int:
        position = self.queue_position_for_new_job()
        await self._queue.put(job)
        log.info(
            "Queued %s chat=%s position=%d queue_size=%d",
            job.path.name,
            job.chat_id,
            position,
            self._queue.qsize(),
        )
        return position

    async def _worker(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                if job is None:
                    if self._stopped.is_set():
                        break
                    continue
                self._processing = job
                await self._run_job(job)
            finally:
                self._processing = None
                self._queue.task_done()

    async def _run_job(self, job: PhotoJob) -> None:
        filename = job.path.name
        log.info("Processing queued photo %s chat=%s", filename, job.chat_id)

        await self._edit_status(
            job,
            f"▶️ Processing now: {filename}\n"
            "Step 1/2: preparing image…",
        )

        started = time.monotonic()
        heartbeat_stop = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._heartbeat(job, started, heartbeat_stop),
            name=f"heartbeat-{filename}",
        )

        process_timeout = resolve_nim_timeout() + PROCESS_TIMEOUT_SLACK_SEC
        try:
            await self._edit_status(
                job,
                f"▶️ Processing: {filename}\n"
                f"Step 2/2: analyzing with NIM (up to {resolve_nim_timeout() // 60} min)…",
            )
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    process_image,
                    job.path,
                    force=False,
                    nim_stream=False,
                ),
                timeout=process_timeout,
            )
            from telegram_bot import format_result_message

            text = format_result_message(result)
            await self._edit_status(job, text)
            log.info(
                "Finished %s chat=%s status=%s inserted=%d updated=%d (%.1fs)",
                filename,
                job.chat_id,
                result["status"],
                result["inserted"],
                result["updated"],
                time.monotonic() - started,
            )
        except asyncio.TimeoutError:
            mins = process_timeout // 60
            log.error("Timed out processing %s after %ds", filename, process_timeout)
            await self._edit_status(
                job,
                f"⏱ Timed out after {mins} minutes while analyzing {filename}.\n\n"
                "The vision API did not finish in time. Try again with a clearer photo, "
                "or increase NIM_TIMEOUT_SECONDS in .env.",
            )
        except EnvironmentError as exc:
            log.error("Configuration error: %s", exc)
            await self._edit_status(job, f"Server configuration error: {exc}")
        except Exception as exc:
            log.exception("Processing failed for %s", filename)
            await self._edit_status(job, f"Processing failed: {exc}")
        finally:
            heartbeat_stop.set()
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

        await self._notify_queue_advanced()

    async def _heartbeat(
        self,
        job: PhotoJob,
        started: float,
        stop: asyncio.Event,
    ) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_INTERVAL_SEC)
                return
            except asyncio.TimeoutError:
                pass
            elapsed = int(time.monotonic() - started)
            mins, secs = divmod(elapsed, 60)
            waiting = self._queue.qsize()
            extra = f"\n{waiting} more photo(s) waiting in queue." if waiting else ""
            try:
                await self._edit_status(
                    job,
                    f"⏳ Still analyzing {job.path.name}… ({mins}m {secs:02d}s)\n"
                    "NIM is working — not frozen." + extra,
                )
            except Exception:
                log.debug("Heartbeat edit failed for %s", job.path.name, exc_info=True)

    async def _notify_queue_advanced(self) -> None:
        if self._queue.qsize() == 0:
            return
        # Next job will update its own status when picked up.
        log.info("%d photo(s) still waiting in queue", self._queue.qsize())

    async def _edit_status(self, job: PhotoJob, text: str) -> None:
        await self._bot.edit_message_text(
            chat_id=job.chat_id,
            message_id=job.status_message_id,
            text=text,
        )


def format_queue_reply(position: int, filename: str, status: QueueStatus) -> str:
    if position == 1 and status.processing is None:
        return (
            f"📥 Queued: {filename}\n"
            "Starting now — one photo is processed at a time."
        )
    ahead = position - 1
    if ahead == 1:
        ahead_text = "1 photo ahead of yours"
    else:
        ahead_text = f"{ahead} photos ahead of yours"
    return (
        f"📥 Queued: {filename}\n"
        f"Position {position} in line ({ahead_text}).\n"
        "You'll be notified when processing starts."
    )


def format_queue_status_message(status: QueueStatus) -> str:
    if status.total_pending == 0:
        return "✅ Queue is empty — send a photo to process."

    lines = ["📋 Photo queue", ""]
    if status.processing:
        lines.append(f"▶️ Now: {status.processing.path.name}")
    if status.waiting:
        lines.append(f"⏳ Waiting: {status.waiting} photo(s)")
    lines.append("")
    lines.append(f"Total in pipeline: {status.total_pending}")
    return "\n".join(lines)
