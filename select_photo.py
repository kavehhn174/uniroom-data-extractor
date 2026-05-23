#!/usr/bin/env python3
"""Simple arrow-key menu to pick a photo from input/ and process it."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from simple_term_menu import TerminalMenu

from logging_config import setup_logging
from process_bulletin_board import (
    SUPPORTED_EXTENSIONS,
    process_image,
    resolve_output_format,
)

log = logging.getLogger("uniroom.menu")

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT_DIR = PROJECT_ROOT / "input"


def discover_photos(input_dir: Path) -> list[Path]:
    if not input_dir.is_dir():
        return []
    return sorted(
        p
        for p in input_dir.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
    )


def menu_label(path: Path) -> str:
    size_kb = path.stat().st_size / 1024
    return f"{path.name}  ({size_kb:.0f} KB)"


def print_result_summary(result: dict) -> None:
    print(f"  Status:    {result['status']}")
    print(f"  Extracted: {result['extracted']} listing(s)")
    print(f"  Inserted:  {result['inserted']}")
    print(f"  Updated:   {result['updated']}")
    if result.get("with_missing_data"):
        print(f"  Incomplete: {result['with_missing_data']} listing(s)")
    for err in result.get("errors") or []:
        print(f"  Error: {err}")


def process_all_photos(
    photos: list[Path],
    *,
    force: bool,
    output_fmt: str,
    save_png: bool,
    nim_stream: bool | None,
) -> list[dict]:
    results: list[dict] = []
    total = len(photos)

    print("\n--- All Photos ---")
    for index, photo in enumerate(photos, start=1):
        print(f"\n[{index}/{total}] {photo.name}")
        try:
            result = process_image(
                photo,
                force=force,
                output_format=output_fmt,
                save_png=save_png,
                nim_stream=nim_stream,
            )
        except Exception as exc:
            log.error("Failed: %s", exc)
            result = {
                "image": str(photo),
                "filename": photo.name,
                "status": "failed",
                "extracted": 0,
                "inserted": 0,
                "updated": 0,
                "with_missing_data": 0,
                "errors": [str(exc)],
            }
        results.append(result)
        print_result_summary(result)

    print("\n--- Batch Summary ---")
    totals = {
        "completed": 0,
        "skipped": 0,
        "failed": 0,
        "extracted": 0,
        "inserted": 0,
        "updated": 0,
        "with_missing_data": 0,
    }
    for result in results:
        status = result.get("status", "failed")
        if status in totals:
            totals[status] += 1
        totals["extracted"] += int(result.get("extracted", 0) or 0)
        totals["inserted"] += int(result.get("inserted", 0) or 0)
        totals["updated"] += int(result.get("updated", 0) or 0)
        totals["with_missing_data"] += int(result.get("with_missing_data", 0) or 0)

    print(f"  Completed: {totals['completed']}")
    print(f"  Skipped:   {totals['skipped']}")
    print(f"  Failed:    {totals['failed']}")
    print(f"  Extracted: {totals['extracted']} listing(s)")
    print(f"  Inserted:  {totals['inserted']}")
    print(f"  Updated:   {totals['updated']}")
    if totals["with_missing_data"]:
        print(f"  Incomplete: {totals['with_missing_data']} listing(s)")

    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pick a photo from input/ with arrow keys and process it.",
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help=f"Folder to browse (default: {DEFAULT_INPUT_DIR})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reprocess even if already completed",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        help="Logging level: DEBUG, INFO, WARNING",
    )
    parser.add_argument(
        "--format",
        choices=("jpeg", "png"),
        default=None,
        help="Encode for NIM as JPEG or PNG (default: jpeg)",
    )
    parser.add_argument(
        "--save-png",
        action="store_true",
        help="Save PNG to input/converted/ (auto when --format png)",
    )
    stream_group = parser.add_mutually_exclusive_group()
    stream_group.add_argument(
        "--stream",
        action="store_true",
        default=None,
        help="Stream NIM tokens to terminal (default: on)",
    )
    stream_group.add_argument(
        "--no-stream",
        action="store_true",
        help="Disable streaming; print full response when done",
    )
    args = parser.parse_args()
    setup_logging(args.log_level)
    output_fmt = resolve_output_format(args.format)
    save_png = args.save_png or output_fmt == "png"
    nim_stream = False if args.no_stream else (True if args.stream else None)

    input_dir = args.input_dir.resolve()
    if not input_dir.exists():
        input_dir.mkdir(parents=True, exist_ok=True)

    photos = discover_photos(input_dir)
    if not photos:
        print(f"No images in {input_dir}")
        print(f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}")
        return 1

    labels = ["All Photos"] + [menu_label(p) for p in photos]

    print(f"\n{len(photos)} photo(s) in {input_dir}")
    print("↑↓ move · Enter select · Esc cancel\n", flush=True)

    try:
        index = TerminalMenu(
            labels,
            title="Select a photo to process",
            cycle_cursor=True,
            clear_screen=False,
        ).show()
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 0

    if index is None:
        print("Cancelled.")
        return 0

    try:
        if index == 0:
            results = process_all_photos(
                photos,
                force=args.force,
                output_fmt=output_fmt,
                save_png=save_png,
                nim_stream=nim_stream,
            )
            return 0 if not any(result.get("status") == "failed" for result in results) else 1

        selected = photos[index - 1]
        log.info("Selected: %s", selected.name)
        result = process_image(
            selected,
            force=args.force,
            output_format=output_fmt,
            save_png=save_png,
            nim_stream=nim_stream,
        )
    except Exception as exc:
        log.error("Failed: %s", exc)
        return 1

    log.info("Done — status=%s", result["status"])
    print("\n--- Summary ---")
    print_result_summary(result)

    return 0 if result["status"] != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
