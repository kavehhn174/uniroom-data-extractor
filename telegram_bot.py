#!/usr/bin/env python3
"""Telegram bot: send bulletin board photos (or image files) → MongoDB listings."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from logging_config import setup_logging
from process_bulletin_board import SUPPORTED_EXTENSIONS, process_image

log = logging.getLogger("uniroom.telegram")

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_UPLOAD_DIR = PROJECT_ROOT / "input" / "telegram"

MIME_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
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


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "Uniroom bulletin board extractor\n\n"
        "Send a photo of a university housing bulletin board:\n"
        "• as a compressed photo, or\n"
        "• as an image file (HEIC, JPEG, PNG, WebP)\n\n"
        "I'll extract listings and save them to the database.\n\n"
        "Commands: /help"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "How to use:\n"
        "1. Take or forward a clear photo of the bulletin board.\n"
        "2. Send it here as a photo or as a document (file).\n"
        "3. Wait while the image is analyzed (usually 30–120 seconds).\n"
        "4. You'll get a summary: listings found, inserted, duplicates.\n\n"
        "Supported formats: JPEG, PNG, WebP, HEIC/HEIF.\n"
        "Output is stored in English in MongoDB."
    )


async def _process_saved_image(
    update: Update,
    path: Path,
    *,
    force: bool = False,
) -> None:
    if not update.message:
        return

    chat_id = update.effective_chat.id if update.effective_chat else 0
    status_msg = await update.message.reply_text(
        f"Processing {path.name}…\nThis may take up to a few minutes."
    )

    try:
        result = process_image(
            path,
            force=force,
            nim_stream=False,
        )
        text = format_result_message(result)
        await status_msg.edit_text(text)
        log.info(
            "Telegram chat=%s file=%s status=%s inserted=%d updated=%d",
            chat_id,
            path.name,
            result["status"],
            result["inserted"],
            result["updated"],
        )
    except EnvironmentError as exc:
        log.error("Configuration error: %s", exc)
        await status_msg.edit_text(f"Server configuration error: {exc}")
    except Exception as exc:
        log.exception("Processing failed for %s", path.name)
        await status_msg.edit_text(f"Processing failed: {exc}")


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.photo:
        return

    photo = update.message.photo[-1]
    chat_id = update.effective_chat.id if update.effective_chat else 0
    dest = _save_path(chat_id, update.message.message_id, ".jpg")

    tg_file = await context.bot.get_file(photo.file_id)
    await tg_file.download_to_drive(custom_path=str(dest))
    log.info("Downloaded Telegram photo -> %s", dest)
    await _process_saved_image(update, dest)


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
    await _process_saved_image(update, dest)


def build_application(token: str) -> Application:
    app = (
        Application.builder()
        .token(token)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
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
    log.info("Starting Uniroom Telegram bot (polling)")

    app = build_application(token)
    app.run_polling(allowed_updates=Update.ALL_TYPES)
    return 0


if __name__ == "__main__":
    sys.exit(main())
