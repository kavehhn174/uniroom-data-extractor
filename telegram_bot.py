#!/usr/bin/env python3
"""Telegram bot: send bulletin board photos (or image files) → MongoDB listings."""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from process_bulletin_board import resolve_nim_timeout

from logging_config import setup_logging
from process_bulletin_board import SUPPORTED_EXTENSIONS, fetch_telegram_user_data
from telegram_queue import (
    PhotoJob,
    PhotoProcessingQueue,
    format_queue_reply,
    format_queue_status_message,
)

log = logging.getLogger("uniroom.telegram")

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_UPLOAD_DIR = PROJECT_ROOT / "input" / "telegram"
TELEGRAM_MESSAGE_LIMIT = 4096

MIME_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
}

STATUS_EMOJI = {
    "completed": "✅",
    "processing": "⏳",
    "failed": "❌",
    "skipped": "⏭",
}

TYPE_EMOJI = {
    "house": "🏠",
    "room": "🛏",
}


def upload_dir() -> Path:
    path = Path(os.environ.get("TELEGRAM_UPLOAD_DIR", str(DEFAULT_UPLOAD_DIR)))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _ext_from_mime(mime: str | None) -> str:
    if not mime:
        return ".jpg"
    mime = mime.lower().split(";")[0].strip()
    return MIME_TO_EXT.get(mime, ".jpg")


def _is_supported_document(filename: str | None, mime: str | None) -> bool:
    if filename:
        suffix = Path(filename).suffix.lower()
        if suffix in SUPPORTED_EXTENSIONS:
            return True
    if mime and mime.lower().startswith("image/"):
        return True
    return False


def _save_path(chat_id: int, message_id: int, suffix: str) -> Path:
    suffix = suffix if suffix.startswith(".") else f".{suffix}"
    return upload_dir() / f"tg_{chat_id}_{message_id}{suffix}"


def format_result_message(result: dict) -> str:
    lines = [
        "Done processing your photo.",
        "",
        f"Status: {result['status']}",
        f"Listings found: {result['extracted']}",
        f"New in database: {result['inserted']}",
        f"Duplicates updated: {result['updated']}",
    ]
    if result.get("with_missing_data"):
        lines.append(f"Incomplete listings: {result['with_missing_data']}")
    if result["status"] == "skipped":
        lines.append("")
        lines.append(
            "This filename was already processed. Send a new photo or ask an admin to use --force on CLI."
        )
    errors = result.get("errors") or []
    if errors:
        lines.append("")
        lines.append("Warnings:")
        for err in errors[:5]:
            lines.append(f"• {err}")
        if len(errors) > 5:
            lines.append(f"• … and {len(errors) - 5} more")
    return "\n".join(lines)


def _format_dt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%d %b %Y")
    if isinstance(value, str):
        return value[:10] if len(value) >= 10 else value
    return str(value)


def _format_rent(listing: dict) -> str:
    price = listing.get("rentPrice")
    if price is None:
        return "Rent: unknown"
    bills = "all-inclusive" if listing.get("isAllInclusive") else "bills extra"
    return f"€{price}/mo · {bills}"


def _format_location(listing: dict) -> str:
    parts = [
        listing.get("city") or "",
        listing.get("neighborhood") or "",
        listing.get("street") or "",
    ]
    location = ", ".join(p for p in parts if p)
    return location or "Location unknown"


def _format_contact(listing: dict) -> str | None:
    contact = listing.get("contactDetails") or {}
    for key in ("phone", "whatsapp", "email", "telegram"):
        value = contact.get(key)
        if value:
            return f"{key}: {value}"
    return None


def _listing_belongs_to_chat(listing: dict, chat_id: int) -> bool:
    prefix = f"tg_{chat_id}_"
    primary = listing.get("source_photo_filename") or ""
    if primary.startswith(prefix):
        return True
    for name in listing.get("source_photo_filenames") or []:
        if isinstance(name, str) and name.startswith(prefix):
            return True
    return False


def _source_photos_for_chat(listing: dict, chat_id: int) -> list[str]:
    prefix = f"tg_{chat_id}_"
    names: list[str] = []
    primary = listing.get("source_photo_filename")
    if isinstance(primary, str) and primary.startswith(prefix):
        names.append(primary)
    for name in listing.get("source_photo_filenames") or []:
        if isinstance(name, str) and name.startswith(prefix) and name not in names:
            names.append(name)
    return names


def _short_filename(name: str) -> str:
    if len(name) <= 28:
        return name
    return f"{name[:12]}…{name[-12:]}"


def format_listing_line(listing: dict, index: int) -> list[str]:
    listing_type = listing.get("type") or "?"
    emoji = TYPE_EMOJI.get(listing_type, "📋")
    type_label = listing_type.capitalize() if listing_type in ("house", "room") else "Listing"
    lines = [
        f"{index}. {emoji} {type_label} · {_format_location(listing)}",
        f"   {_format_rent(listing)}",
    ]
    area = listing.get("area")
    if area:
        lines.append(f"   {area} m²")
    avail = listing.get("availabilityDate")
    if avail:
        lines.append(f"   Available: {_format_dt(avail)}")
    if listing.get("has_missing_data"):
        missing = listing.get("missing_fields") or []
        preview = ", ".join(missing[:4])
        if len(missing) > 4:
            preview += f" (+{len(missing) - 4} more)"
        lines.append(f"   ⚠️ Incomplete: {preview or 'see details'}")
    contact = _format_contact(listing)
    if contact:
        lines.append(f"   📞 {contact}")
    desc = (listing.get("description") or "").strip()
    if desc:
        short = desc if len(desc) <= 120 else desc[:117] + "…"
        lines.append(f"   “{short}”")
    return lines


def format_user_data_messages(chat_id: int, data: dict) -> list[str]:
    """Build one or more Telegram messages (≤4096 chars each)."""
    files: list[dict] = data.get("files") or []
    listings: list[dict] = data.get("listings") or []
    user_listings = [l for l in listings if _listing_belongs_to_chat(l, chat_id)]

    houses = [l for l in user_listings if l.get("type") == "house"]
    rooms = [l for l in user_listings if l.get("type") == "room"]
    other = [l for l in user_listings if l.get("type") not in ("house", "room")]

    status_counts: dict[str, int] = {}
    for doc in files:
        status = doc.get("status") or "unknown"
        status_counts[status] = status_counts.get(status, 0) + 1

    lines = [
        "📊 Your Uniroom data",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"🗄 Database: {data.get('database', '—')}",
        f"📁 Listings: {data.get('listings_collection', 'housing_listings')}",
        f"📷 Photo log: {data.get('files_collection', 'reviewed_files')}",
        "",
        "━━━ Summary ━━━",
        f"Photos you sent: {len(files)}",
    ]

    if status_counts:
        parts = [
            f"{STATUS_EMOJI.get(k, '•')} {v} {k}"
            for k, v in sorted(status_counts.items())
        ]
        lines.append("   " + " · ".join(parts))
    else:
        lines.append("   No photos processed yet — send a bulletin board image!")

    lines.extend(
        [
            "",
            f"Listings from your photos: {len(user_listings)}",
            f"   🏠 {len(houses)} whole properties",
            f"   🛏 {len(rooms)} rooms",
        ]
    )
    if other:
        lines.append(f"   📋 {len(other)} other / unspecified")

    if files:
        lines.extend(["", "━━━ Your photos ━━━"])
        for i, doc in enumerate(files[:15], start=1):
            status = doc.get("status") or "unknown"
            emoji = STATUS_EMOJI.get(status, "•")
            name = _short_filename(doc.get("filename") or "?")
            extracted = doc.get("listings_extracted", 0)
            inserted = doc.get("listings_inserted", 0)
            updated = doc.get("listings_updated", 0)
            reviewed = _format_dt(doc.get("reviewed_at"))
            lines.append(f"{i}. {emoji} {name}")
            lines.append(
                f"   {reviewed} · {extracted} found · {inserted} new · {updated} updated"
            )
            if status == "failed" and doc.get("error_message"):
                err = str(doc["error_message"])[:80]
                lines.append(f"   ❌ {err}")
        if len(files) > 15:
            lines.append(f"   … and {len(files) - 15} more photos")

    if houses:
        lines.extend(["", "━━━ 🏠 Houses (whole properties) ━━━"])
        for i, listing in enumerate(houses[:20], start=1):
            lines.extend(format_listing_line(listing, i))
            sources = _source_photos_for_chat(listing, chat_id)
            if sources:
                lines.append(f"   📷 {_short_filename(sources[0])}")
        if len(houses) > 20:
            lines.append(f"   … and {len(houses) - 20} more houses")

    if rooms:
        lines.extend(["", "━━━ 🛏 Rooms ━━━"])
        show = min(len(rooms), 10 if houses else 20)
        for i, listing in enumerate(rooms[:show], start=1):
            lines.extend(format_listing_line(listing, i))
            sources = _source_photos_for_chat(listing, chat_id)
            if sources:
                lines.append(f"   📷 {_short_filename(sources[0])}")
        if len(rooms) > show:
            lines.append(f"   … and {len(rooms) - show} more rooms")

    if not files and not user_listings:
        lines.extend(
            [
                "",
                "Nothing here yet. Send a bulletin board photo to get started!",
                "Use /help for instructions.",
            ]
        )
    elif not user_listings and files:
        lines.extend(
            [
                "",
                "Photos are logged but no listings were saved yet.",
                "Check processing status above or send a clearer photo.",
            ]
        )

    lines.append("")
    lines.append("Tip: /list anytime to refresh · send a photo to add more")

    return _split_message("\n".join(lines))


def _split_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for line in text.split("\n"):
        line_len = len(line) + 1
        if current and current_len + line_len > limit:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += line_len

    if current:
        chunks.append("\n".join(current))
    return chunks or [text[: limit - 3] + "…"]


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "Uniroom bulletin board extractor\n\n"
        "Send a photo of a university housing bulletin board:\n"
        "• as a compressed photo, or\n"
        "• as an image file (HEIC, JPEG, PNG, WebP)\n\n"
        "I'll extract listings and save them to the database.\n\n"
        "Commands:\n"
        "/list — your photos, houses & rooms in the database\n"
        "/queue — see photos waiting to be processed\n"
        "/help — how to use the bot"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "How to use:\n"
        "1. Take or forward a clear photo of the bulletin board.\n"
        "2. Send it here as a photo or as a document (file).\n"
        "3. Photos are processed one at a time (queue). Send several — "
        "they'll run in order.\n"
        "4. Wait for the summary: listings found, inserted, duplicates.\n\n"
        "/queue — check your position while waiting.\n"
        "/list — view your uploaded photos and all houses & rooms "
        "extracted from them (grouped and easy to read).\n\n"
        "Supported formats: JPEG, PNG, WebP, HEIC/HEIF.\n"
        "Output is stored in English in MongoDB."
    )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    chat_id = update.effective_chat.id if update.effective_chat else 0
    status_msg = await update.message.reply_text("Loading your data…")

    try:
        data = fetch_telegram_user_data(chat_id)
        messages = format_user_data_messages(chat_id, data)
        await status_msg.edit_text(messages[0])
        for extra in messages[1:]:
            await update.message.reply_text(extra)
        log.info(
            "Telegram /list chat=%s files=%d listings=%d",
            chat_id,
            len(data.get("files") or []),
            len(data.get("listings") or []),
        )
    except EnvironmentError as exc:
        log.error("Configuration error: %s", exc)
        await status_msg.edit_text(f"Server configuration error: {exc}")
    except Exception as exc:
        log.exception("Failed /list for chat=%s", chat_id)
        await status_msg.edit_text(f"Could not load your data: {exc}")


def _photo_queue(context: ContextTypes.DEFAULT_TYPE) -> PhotoProcessingQueue:
    queue = context.application.bot_data.get("photo_queue")
    if queue is None:
        raise RuntimeError("Photo queue not initialized")
    return queue


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    queue = _photo_queue(context)
    await update.message.reply_text(format_queue_status_message(queue.status))


async def _enqueue_saved_image(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    path: Path,
) -> None:
    if not update.message:
        return

    chat_id = update.effective_chat.id if update.effective_chat else 0
    queue = _photo_queue(context)
    position = queue.queue_position_for_new_job()

    status_msg = await update.message.reply_text(
        format_queue_reply(position, path.name, queue.status)
    )
    job = PhotoJob(
        chat_id=chat_id,
        path=path,
        user_message_id=update.message.message_id,
        status_message_id=status_msg.message_id,
    )
    await queue.enqueue(job)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.photo:
        return

    photo = update.message.photo[-1]
    chat_id = update.effective_chat.id if update.effective_chat else 0
    dest = _save_path(chat_id, update.message.message_id, ".jpg")

    tg_file = await context.bot.get_file(photo.file_id)
    await tg_file.download_to_drive(custom_path=str(dest))
    log.info("Downloaded Telegram photo -> %s", dest)
    await _enqueue_saved_image(update, context, dest)


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.document:
        return

    doc = update.message.document
    filename = doc.file_name or ""
    mime = doc.mime_type

    if not _is_supported_document(filename, mime):
        await update.message.reply_text(
            "Unsupported file. Send an image (JPEG, PNG, WebP, HEIC) "
            "as a photo or as a document."
        )
        return

    chat_id = update.effective_chat.id if update.effective_chat else 0
    if filename and Path(filename).suffix.lower() in SUPPORTED_EXTENSIONS:
        suffix = Path(filename).suffix.lower()
    else:
        suffix = _ext_from_mime(mime)

    dest = _save_path(chat_id, update.message.message_id, suffix)
    tg_file = await context.bot.get_file(doc.file_id)
    await tg_file.download_to_drive(custom_path=str(dest))
    log.info("Downloaded Telegram document -> %s", dest)
    await _enqueue_saved_image(update, context, dest)


async def _start_photo_queue(application: Application) -> None:
    queue = PhotoProcessingQueue(application.bot)
    await queue.start()
    application.bot_data["photo_queue"] = queue
    log.info("Photo queue worker ready")


async def _stop_photo_queue(application: Application) -> None:
    queue: PhotoProcessingQueue | None = application.bot_data.get("photo_queue")
    if queue is not None:
        await queue.stop()


def build_application(token: str) -> Application:
    app = (
        Application.builder()
        .token(token)
        .post_init(_start_photo_queue)
        .post_shutdown(_stop_photo_queue)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    return app


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    setup_logging(os.environ.get("LOG_LEVEL"))

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print(
            "Error: set TELEGRAM_BOT_TOKEN in .env (see .env.example)",
            file=sys.stderr,
        )
        return 1

    upload_dir()
    log.info("Telegram upload directory: %s", upload_dir())
    log.info(
        "Starting Uniroom Telegram bot (polling, serial photo queue, NIM timeout=%ss)",
        resolve_nim_timeout(),
    )

    app = build_application(token)
    app.run_polling(allowed_updates=Update.ALL_TYPES)
    return 0


if __name__ == "__main__":
    sys.exit(main())
