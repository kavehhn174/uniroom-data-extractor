#!/usr/bin/env python3
"""
Process university housing bulletin board photos into MongoDB.

Pipeline: HEIC/JPEG load -> compress -> NVIDIA NIM extraction (English) ->
validate -> deduplicate -> insert listings -> record reviewed file.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from openai import OpenAI

from logging_config import setup_logging

log_image = logging.getLogger("uniroom.image")
log_nim = logging.getLogger("uniroom.nim")
log_mongo = logging.getLogger("uniroom.mongo")
log_dedup = logging.getLogger("uniroom.dedup")
log_pipeline = logging.getLogger("uniroom.pipeline")
from PIL import Image
from pydantic import BaseModel, Field, ValidationError
from pymongo import ASCENDING, MongoClient
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError, OperationFailure
from rapidfuzz import fuzz

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIC_SUPPORTED = True
except ImportError:
    HEIC_SUPPORTED = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ListingType = Literal["room", "house"]
ListingStatus = Literal["active", "expired", "paused", "deleted"]
ReviewStatus = Literal["processing", "completed", "failed", "skipped"]

NIM_MODEL = os.environ.get("NIM_VL_MODEL", "nvidia/nemotron-nano-12b-v2-vl")
NIM_MAX_TOKENS = int(os.environ.get("NIM_MAX_TOKENS", "8192"))
FUZZY_DUPLICATE_THRESHOLD = 85
COLLECTION_NAME = os.environ.get("MONGO_COLLECTION", "housing_listings")
FILES_COLLECTION_NAME = os.environ.get("MONGO_FILES_COLLECTION", "reviewed_files")

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
OutputFormat = Literal["jpeg", "png"]


def resolve_output_format(explicit: OutputFormat | None = None) -> OutputFormat:
    if explicit is not None:
        return explicit
    env = os.environ.get("IMAGE_OUTPUT_FORMAT", "jpeg").lower()
    return "png" if env == "png" else "jpeg"


def resolve_nim_stream(explicit: bool | None = None) -> bool:
    """Whether to stream NIM tokens to the terminal (default: on)."""
    if explicit is not None:
        return explicit
    return os.environ.get("NIM_STREAM", "1").lower() not in ("0", "false", "no", "off")


def resolve_nim_thinking(explicit: bool | None = None) -> bool:
    """Kimi K2.6 reasoning mode (default: off — avoids NIM 500 stream bugs)."""
    if explicit is not None:
        return explicit
    return os.environ.get("NIM_THINKING", "0").lower() in ("1", "true", "yes", "on")


def _is_kimi_model(model: str) -> bool:
    name = model.lower()
    return "kimi" in name or name.startswith("moonshotai/")

# ---------------------------------------------------------------------------
# Pydantic schemas (aligned with Uniroom Mongoose Listing model)
# ---------------------------------------------------------------------------


class ContactDetails(BaseModel):
    phone: Optional[str] = None
    email: Optional[str] = None
    whatsapp: Optional[str] = None
    telegram: Optional[str] = None
    sms: Optional[str] = None
    other: Optional[str] = None


class GeoLocation(BaseModel):
    type: Literal["Point"] = "Point"
    coordinates: list[float] = Field(
        default_factory=list,
        description="[longitude, latitude] when known from the ad",
    )


class ListingExtracted(BaseModel):
    """Fields returned by NIM — matches app Listing schema where possible."""

    type: Optional[ListingType] = None
    area: Optional[float] = None
    rentPrice: Optional[int] = None
    isAllInclusive: Optional[bool] = None
    approximateBillsCost: Optional[float] = None
    hasResidenza: Optional[bool] = None
    hasPrivateBathroom: Optional[bool] = None
    city: str = ""
    neighborhood: Optional[str] = None
    street: Optional[str] = None
    location: Optional[GeoLocation] = None
    totalPeopleInHouse: Optional[int] = None
    peopleInRoom: Optional[int] = None
    availabilityDate: Optional[str] = None
    hasAgencyFee: Optional[bool] = None
    depositAmount: Optional[float] = None
    description: Optional[str] = None
    contactDetails: ContactDetails = Field(default_factory=ContactDetails)
    missing_fields: list[str] = Field(
        default_factory=list,
        description="Schema field names that were illegible or not on the flyer",
    )


class ListingRecord(ListingExtracted):
    """Listing document stored in MongoDB (plus extractor metadata)."""

    source_photo_filename: str = ""
    has_missing_data: bool = False
    status: ListingStatus = "active"
    source: str = "bulletin_board"


class BulletinBoardExtraction(BaseModel):
    listings: list[ListingExtracted] = Field(default_factory=list)


# Recognize a listing object from NIM (new or legacy field names).
_LISTING_FIELD_NAMES = frozenset(
    {
        "type",
        "rentPrice",
        "price_eur",
        "isAllInclusive",
        "bills_included",
        "city",
        "neighborhood",
        "street",
        "location",
        "description",
        "raw_text_summary",
        "contactDetails",
        "contact_info",
        "area",
        "totalPeopleInHouse",
        "peopleInRoom",
        "availabilityDate",
        "hasPrivateBathroom",
        "hasResidenza",
        "approximateBillsCost",
        "depositAmount",
        "hasAgencyFee",
        "missing_fields",
    }
)


class ReviewedFileRecord(BaseModel):
    """Tracks which image files have been processed."""

    filename: str
    file_path: str
    file_format: str
    file_size_bytes: int
    status: ReviewStatus
    reviewed_at: datetime
    listings_extracted: int = 0
    listings_inserted: int = 0
    listings_updated: int = 0
    listings_with_missing_data: int = 0
    error_message: Optional[str] = None


# ---------------------------------------------------------------------------
# Step 1: Image loading (HEIC), compression & encoding
# ---------------------------------------------------------------------------


def _ensure_heic_support(path: Path) -> None:
    suffix = path.suffix.lower()
    if suffix in {".heic", ".heif"} and not HEIC_SUPPORTED:
        raise ImportError(
            "HEIC/HEIF support requires pillow-heif. Install with: pip install pillow-heif"
        )


def load_image(path: Path) -> Image.Image:
    _ensure_heic_support(path)
    with Image.open(path) as img:
        return img.convert("RGB")


def _resize_image(img: Image.Image, max_dimension: int) -> Image.Image:
    width, height = img.size
    if width > max_dimension or height > max_dimension:
        scale = max_dimension / max(width, height)
        new_size = (int(width * scale), int(height * scale))
        img = img.resize(new_size, Image.Resampling.LANCZOS)
        log_image.info("Resized to: %dx%d", new_size[0], new_size[1])
    return img


def _encode_image_bytes(img: Image.Image, output_format: OutputFormat) -> tuple[bytes, str]:
    buffer = io.BytesIO()
    if output_format == "png":
        img.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue(), "image/png"
    img.save(buffer, format="JPEG", quality=85, optimize=True)
    return buffer.getvalue(), "image/jpeg"


def save_image_as_png(
    image_path: str | Path,
    max_dimension: int = 2048,
    output_path: str | Path | None = None,
) -> Path:
    """
    Convert an image (HEIC, JPEG, etc.) to PNG on disk.
    Default output: input/converted/{stem}.png next to project input folder,
    or {source_parent}/converted/{stem}.png.
    """
    path = Path(image_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Image not found: {path}")

    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported format '{path.suffix}'. Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )

    img = load_image(path)
    log_image.info(
        "Converting to PNG: %s (original %dx%d)",
        path.name,
        img.size[0],
        img.size[1],
    )
    img = _resize_image(img, max_dimension)

    if output_path is None:
        converted_dir = path.parent / "converted"
        converted_dir.mkdir(parents=True, exist_ok=True)
        out = converted_dir / f"{path.stem}.png"
    else:
        out = Path(output_path).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)

    img.save(out, format="PNG", optimize=True)
    log_image.info("Saved PNG: %s (%d KB)", out, out.stat().st_size // 1024)
    return out


def compress_and_encode_image(
    image_path: str | Path,
    max_dimension: int = 2048,
    output_format: OutputFormat | None = None,
) -> tuple[str, str]:
    """
    Open image (including HEIC), resize if needed, return (base64, mime_type).
    output_format: jpeg (default) or png for the NIM API payload.
    """
    fmt = resolve_output_format(output_format)
    path = Path(image_path)
    log_image.info("Loading image: %s (encode as %s)", path, fmt.upper())
    if not path.is_file():
        raise FileNotFoundError(f"Image not found: {path}")

    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported format '{path.suffix}'. Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )

    img = load_image(path)
    width, height = img.size
    log_image.info("Original size: %dx%d (%s bytes)", width, height, path.stat().st_size)
    img = _resize_image(img, max_dimension)

    raw_bytes, mime_type = _encode_image_bytes(img, fmt)
    encoded = base64.b64encode(raw_bytes).decode("ascii")
    log_image.info(
        "Encoded %s: %d KB (base64 length %d)",
        fmt.upper(),
        len(raw_bytes) // 1024,
        len(encoded),
    )
    return encoded, mime_type


# ---------------------------------------------------------------------------
# Step 2: NVIDIA NIM structured extraction
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You extract student housing listings from university bulletin board photos for a rental app.

RULES:
1. ENGLISH ONLY: Translate Italian (or other) text to English in all string fields.
2. ONE OBJECT PER FLYER: Each distinct flyer/note is one element in "listings".
3. PARTIAL ADS: Extract only readable data. Use null for unknown values. List every field
   you could not read in missing_fields (use exact camelCase names below).
4. type: "room" (single room or bed in shared flat) or "house" (entire apartment/property).
5. rentPrice: integer monthly rent in EUR.
6. isAllInclusive: true if bills/utilities/internet are included; false if tenant pays extras.
7. approximateBillsCost: estimated monthly bills in EUR when not all-inclusive.
8. area: size in square meters if stated.
9. hasResidenza: true if "residenza universitaria" / registered student housing is mentioned.
10. hasPrivateBathroom: true/false/null.
11. city: required when visible (e.g. Padova, Milan).
12. neighborhood, street: split address when possible.
13. location: only if coordinates are explicit; else null. Format:
    {"type":"Point","coordinates":[longitude, latitude]}
14. totalPeopleInHouse: total tenants the flat is for (e.g. "4 posti letto" -> 4).
15. peopleInRoom: beds in the advertised room (1 for single, 2 for double, etc.).
16. availabilityDate: ISO date YYYY-MM-DD if a move-in date is shown, else null.
17. hasAgencyFee, depositAmount: agency fee and deposit in EUR if stated.
18. description: up to 2000 chars, English summary of the ad.
19. contactDetails: object with phone, email, whatsapp, telegram, sms, other (split channels).

OUTPUT FORMAT (required):
{"listings": [{ "type": "room", "rentPrice": 350, "city": "Padova", ... }]}
Always use the "listings" array. Never return a single listing at the top level."""


def get_nim_client() -> OpenAI:
    api_key = os.environ.get("NVIDIA_API_KEY") or os.environ.get("NVAPI_KEY")
    if not api_key:
        raise EnvironmentError(
            "Set NVIDIA_API_KEY or NVAPI_KEY with your NVIDIA NIM API key."
        )
    return OpenAI(
        base_url=os.environ.get(
            "NIM_BASE_URL", "https://integrate.api.nvidia.com/v1"
        ),
        api_key=api_key,
    )


def _nim_request_messages(data_url: str) -> list[dict]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "Extract every housing listing on this board. "
                        "Output English only. Flag unreadable fields in missing_fields."
                    ),
                },
                {
                    "type": "image_url",
                    "image_url": {"url": data_url},
                },
            ],
        },
    ]


def _nim_response_format(schema: dict, model: str) -> dict:
    """
    Structured output format for NIM.

    Kimi K2.6 on integrate.api.nvidia.com returns HTTP 500
    (unhashable type: 'dict') when given Pydantic json_schema with $defs/anyOf.
    Use json_object + the system prompt instead; other models keep strict schema.
    """
    if _is_kimi_model(model):
        return {"type": "json_object"}
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "BulletinBoardExtraction",
            "schema": schema,
            "strict": True,
        },
    }


def _nim_extra_body(model: str, thinking: bool) -> dict[str, Any]:
    """Model-specific request fields forwarded via OpenAI extra_body."""
    if not _is_kimi_model(model):
        return {}
    return {"chat_template_kwargs": {"thinking": thinking}}


def _log_nim_usage(usage: object | None) -> None:
    if not usage:
        return
    log_nim.info(
        "NIM token usage: prompt=%s completion=%s total=%s",
        getattr(usage, "prompt_tokens", "?"),
        getattr(usage, "completion_tokens", "?"),
        getattr(usage, "total_tokens", "?"),
    )
    completion = getattr(usage, "completion_tokens", None)
    if completion is not None and completion >= NIM_MAX_TOKENS:
        log_nim.warning(
            "Completion tokens hit max_tokens=%d — response may be truncated JSON",
            NIM_MAX_TOKENS,
        )


def _consume_nim_stream(stream: object) -> tuple[str, object | None]:
    """Print tokens as they arrive; return full text and optional usage."""
    parts: list[str] = []
    usage = None
    chars_since_log = 0

    log_nim.info("NIM stream started (writing tokens below)")
    print("\n--- NIM stream ---", flush=True)

    for chunk in stream:
        if getattr(chunk, "usage", None):
            usage = chunk.usage

        if not chunk.choices:
            continue

        delta = chunk.choices[0].delta
        text = getattr(delta, "content", None) if delta else None
        if not text:
            continue

        parts.append(text)
        chars_since_log += len(text)
        sys.stdout.write(text)
        sys.stdout.flush()

        if chars_since_log >= 400:
            log_nim.debug("NIM stream progress: %d characters received", len(parts))
            chars_since_log = 0

    print("\n--- end stream ---\n", flush=True)
    raw = "".join(parts)
    log_nim.info("NIM stream finished: %d characters", len(raw))
    return raw, usage


def _looks_like_listing(obj: Any) -> bool:
    return isinstance(obj, dict) and bool(_LISTING_FIELD_NAMES & set(obj.keys()))


def _normalize_nim_payload(payload: Any) -> dict[str, Any]:
    """
    Coerce common LLM JSON shapes into {"listings": [...]}.
    Some models return one listing object without the listings array.
    """
    if isinstance(payload, list):
        log_nim.warning("NIM returned a top-level JSON array; wrapping as listings")
        return {"listings": payload}

    if not isinstance(payload, dict):
        raise ValueError(f"NIM payload must be object or array, got {type(payload).__name__}")

    listings = payload.get("listings")
    if isinstance(listings, list):
        return payload

    if isinstance(payload.get("listing"), dict):
        log_nim.warning("NIM returned singular 'listing' key; normalizing to listings[]")
        return {"listings": [payload["listing"]]}

    if _looks_like_listing(payload):
        log_nim.warning(
            "NIM returned one listing without 'listings' wrapper; normalizing"
        )
        return {"listings": [payload]}

    for key in ("data", "result", "ads", "items", "properties"):
        value = payload.get(key)
        if isinstance(value, list):
            log_nim.warning("NIM returned listings under %r key; normalizing", key)
            return {"listings": value}

    log_nim.error(
        "Could not find listings in NIM payload (keys: %s)",
        list(payload.keys()),
    )
    return payload


def _contact_details_from_string(text: str) -> ContactDetails:
    """Best-effort split of a single contact line into structured fields."""
    text = (text or "").strip()
    if not text:
        return ContactDetails()
    lower = text.lower()
    if "whatsapp" in lower or "wa " in lower:
        return ContactDetails(whatsapp=text)
    if "telegram" in lower or "t.me" in lower:
        return ContactDetails(telegram=text)
    if "@" in text and "." in text:
        return ContactDetails(email=text)
    if any(c.isdigit() for c in text):
        return ContactDetails(phone=text)
    return ContactDetails(other=text)


def _migrate_legacy_listing_item(item: dict[str, Any]) -> dict[str, Any]:
    """Map old extractor field names to Uniroom Listing schema."""
    if item.get("rentPrice") is not None or item.get("type") in ("room", "house"):
        migrated = dict(item)
    else:
        room_type = item.get("room_type", "unknown")
        listing_type: ListingType = "house" if room_type == "apartment" else "room"
        loc = item.get("location")
        migrated = {
            "type": listing_type,
            "rentPrice": item.get("price_eur"),
            "isAllInclusive": item.get("bills_included"),
            "city": item.get("city") or "",
            "description": item.get("raw_text_summary") or item.get("description"),
            "missing_fields": list(item.get("missing_fields") or []),
        }
        if isinstance(loc, str) and loc.strip():
            migrated["street"] = loc.strip()
        elif isinstance(loc, dict):
            migrated["location"] = loc
        if item.get("contact_info"):
            migrated["contactDetails"] = _contact_details_from_string(
                str(item["contact_info"])
            ).model_dump()

    if isinstance(migrated.get("contactDetails"), str):
        migrated["contactDetails"] = _contact_details_from_string(
            migrated["contactDetails"]
        ).model_dump()
    if isinstance(migrated.get("location"), str):
        street = migrated.pop("location", "").strip()
        if street and not migrated.get("street"):
            migrated["street"] = street

    geo = migrated.get("location")
    if isinstance(geo, dict):
        coords = geo.get("coordinates") or []
        if not (isinstance(coords, list) and len(coords) == 2):
            migrated["location"] = None

    return migrated


def _validate_listings_payload(payload: dict[str, Any]) -> BulletinBoardExtraction:
    """Validate listings, keeping valid items and logging rejects."""
    raw_listings = payload.get("listings")
    if not isinstance(raw_listings, list):
        raise ValueError("Normalized payload missing 'listings' array")

    valid: list[ListingExtracted] = []
    for i, item in enumerate(raw_listings):
        if not isinstance(item, dict):
            log_nim.warning("Skipping listing[%d]: not an object", i)
            continue
        try:
            migrated = _migrate_legacy_listing_item(item)
            valid.append(ListingExtracted.model_validate(migrated))
        except ValidationError as exc:
            log_nim.warning("Skipping listing[%d]: %s", i, exc)

    if not valid and raw_listings:
        raise ValueError("No valid listings after normalization")

    return BulletinBoardExtraction(listings=valid)


def _parse_nim_response(raw: str) -> BulletinBoardExtraction:
    if not raw:
        log_nim.error("NIM returned empty content")
        raise ValueError("NIM returned empty content")

    log_nim.info("NIM full response:\n%s", raw)
    try:
        payload = json.loads(raw)
        log_nim.info(
            "NIM parsed JSON:\n%s",
            json.dumps(payload, indent=2, ensure_ascii=False),
        )
    except json.JSONDecodeError as exc:
        log_nim.error("Failed to parse NIM JSON: %s", exc)
        raise

    normalized = _normalize_nim_payload(payload)
    if normalized is not payload:
        log_nim.info(
            "NIM normalized JSON:\n%s",
            json.dumps(normalized, indent=2, ensure_ascii=False),
        )

    extraction = _validate_listings_payload(normalized)
    log_nim.info("NIM extracted %d listing(s)", len(extraction.listings))
    return extraction


def extract_listings_from_image(
    image_b64: str,
    mime_type: str = "image/jpeg",
    client: OpenAI | None = None,
    stream: bool | None = None,
) -> BulletinBoardExtraction:
    """Call NIM vision model; structured JSON via schema or json_object (Kimi)."""
    client = client or get_nim_client()
    use_stream = resolve_nim_stream(stream)
    thinking = resolve_nim_thinking()
    data_url = f"data:{mime_type};base64,{image_b64}"
    schema = BulletinBoardExtraction.model_json_schema()
    messages = _nim_request_messages(data_url)
    response_format = _nim_response_format(schema, NIM_MODEL)
    extra_body = _nim_extra_body(NIM_MODEL, thinking)

    log_nim.info(
        "Calling NVIDIA NIM model=%s max_tokens=%d stream=%s thinking=%s response=%s",
        NIM_MODEL,
        NIM_MAX_TOKENS,
        use_stream,
        thinking,
        response_format["type"],
    )
    if response_format["type"] == "json_schema":
        log_nim.debug("Request: strict JSON schema (BulletinBoardExtraction)")
    else:
        log_nim.debug("Request: json_object (prompt-defined BulletinBoardExtraction)")

    common_kwargs: dict[str, Any] = {
        "model": NIM_MODEL,
        "messages": messages,
        "max_tokens": NIM_MAX_TOKENS,
        "temperature": 0.2,
        "response_format": response_format,
    }
    if extra_body:
        common_kwargs["extra_body"] = extra_body

    if use_stream:
        stream_kwargs: dict[str, Any] = {"stream": True}
        if not _is_kimi_model(NIM_MODEL):
            stream_kwargs["stream_options"] = {"include_usage": True}
        nim_stream = client.chat.completions.create(**common_kwargs, **stream_kwargs)
        raw, usage = _consume_nim_stream(nim_stream)
        _log_nim_usage(usage)
        return _parse_nim_response(raw)

    response = client.chat.completions.create(**common_kwargs)
    _log_nim_usage(response.usage)
    raw = response.choices[0].message.content or ""
    return _parse_nim_response(raw)


def enrich_listing(
    extracted: ListingExtracted,
    source_photo_filename: str,
) -> ListingRecord:
    """Attach filename and compute has_missing_data for MongoDB."""
    missing = sorted({f.strip() for f in extracted.missing_fields if f.strip()})
    data = extracted.model_dump()
    data.update(
        {
            "source_photo_filename": source_photo_filename,
            "missing_fields": missing,
            "has_missing_data": len(missing) > 0,
            "status": "active",
            "source": "bulletin_board",
        }
    )
    return ListingRecord(**data)


# ---------------------------------------------------------------------------
# Step 3: Deduplication
# ---------------------------------------------------------------------------


def _normalize_contact(contact: str) -> str:
    return "".join(c for c in contact.lower() if c.isalnum())


def _contact_fingerprint(details: ContactDetails | dict[str, Any]) -> str:
    if isinstance(details, dict):
        details = ContactDetails.model_validate(details)
    parts = [
        details.phone,
        details.whatsapp,
        details.email,
        details.telegram,
        details.sms,
        details.other,
    ]
    combined = " ".join(p.strip() for p in parts if p and p.strip())
    return _normalize_contact(combined)


def _doc_contact_fingerprint(doc: dict[str, Any]) -> str:
    cd = doc.get("contactDetails")
    if isinstance(cd, dict):
        fp = _contact_fingerprint(cd)
        if fp:
            return fp
    return _normalize_contact(str(doc.get("contact_info") or ""))


def _parse_availability_date(value: Optional[str]) -> Optional[datetime]:
    if not value or not str(value).strip():
        return None
    text = str(value).strip()
    try:
        if len(text) == 10:
            return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        log_mongo.warning("Could not parse availabilityDate: %r", value)
        return None


def _listing_to_mongo_doc(listing: ListingRecord) -> dict[str, Any]:
    """Build MongoDB document aligned with Mongoose Listing schema."""
    doc = listing.model_dump()
    avail = _parse_availability_date(listing.availabilityDate)
    if avail:
        doc["availabilityDate"] = avail
    else:
        doc.pop("availabilityDate", None)

    geo = doc.get("location")
    if not (
        isinstance(geo, dict)
        and geo.get("type") == "Point"
        and isinstance(geo.get("coordinates"), list)
        and len(geo["coordinates"]) == 2
    ):
        doc.pop("location", None)

    doc.update(
        {
            "views": 0,
            "liveDaysUsed": 0,
        }
    )
    return doc


def is_duplicate(
    new_listing: ListingRecord,
    db_collection: Collection,
) -> tuple[bool, Optional[object]]:
    contact_fp = _contact_fingerprint(new_listing.contactDetails)
    if contact_fp:
        for doc in db_collection.find({}, {"_id": 1, "contactDetails": 1, "contact_info": 1}):
            if _doc_contact_fingerprint(doc) == contact_fp:
                log_dedup.info(
                    "Duplicate (contact match) -> existing _id=%s",
                    doc["_id"],
                )
                return True, doc["_id"]
        return False, None

    city = (new_listing.city or "").strip()
    if not city:
        log_dedup.debug("No contact or city; treating as new listing")
        return False, None

    candidates = db_collection.find(
        {
            "city": city,
            "type": new_listing.type or "room",
            "rentPrice": new_listing.rentPrice,
        },
        {"_id": 1, "description": 1, "raw_text_summary": 1},
    )

    summary = (new_listing.description or "").strip()
    if not summary:
        return False, None

    for doc in candidates:
        existing_summary = (
            doc.get("description") or doc.get("raw_text_summary") or ""
        ).strip()
        if not existing_summary:
            continue
        score = fuzz.token_sort_ratio(summary, existing_summary)
        if score > FUZZY_DUPLICATE_THRESHOLD:
            log_dedup.info(
                "Duplicate (fuzzy %d%%): city=%r type=%s rent=%s -> _id=%s",
                score,
                city,
                new_listing.type,
                new_listing.rentPrice,
                doc["_id"],
            )
            return True, doc["_id"]

    log_dedup.debug("No duplicate found for city=%r", city)
    return False, None


def upsert_listing(
    listing: ListingRecord,
    db_collection: Collection,
) -> str:
    """Insert new listing or update last_seen_date for duplicate."""
    now = datetime.now(timezone.utc)
    filename = listing.source_photo_filename
    dup, existing_id = is_duplicate(listing, db_collection)

    if dup and existing_id is not None:
        db_collection.update_one(
            {"_id": existing_id},
            {
                "$set": {"last_seen_date": now},
                "$addToSet": {"source_photo_filenames": filename},
            },
        )
        log_mongo.info("Updated duplicate listing _id=%s (photo=%s)", existing_id, filename)
        return "updated"

    doc = _listing_to_mongo_doc(listing)
    doc.update(
        {
            "first_seen_date": now,
            "last_seen_date": now,
            "source_photo_filenames": [filename],
        }
    )
    try:
        result = db_collection.insert_one(doc)
        log_mongo.info(
            "Inserted listing _id=%s city=%r rent=%s photo=%s",
            result.inserted_id,
            listing.city,
            listing.rentPrice,
            filename,
        )
    except DuplicateKeyError:
        db_collection.update_one(
            {"_id": existing_id} if existing_id else {"city": listing.city},
            {"$set": {"last_seen_date": now}},
        )
        log_mongo.warning("Insert conflict; updated last_seen_date")
        return "updated"
    return "inserted"


# ---------------------------------------------------------------------------
# Step 4: MongoDB setup
# ---------------------------------------------------------------------------


def get_mongo_client(uri: str | None = None) -> MongoClient:
    mongo_uri = uri or os.environ.get("MONGO_URI")
    if not mongo_uri:
        raise EnvironmentError("Set MONGO_URI for MongoDB connection.")
    timeout_ms = int(os.environ.get("MONGO_TIMEOUT_MS", "5000"))
    log_mongo.debug("Connecting to MongoDB (timeout=%dms)", timeout_ms)
    return MongoClient(
        mongo_uri,
        serverSelectionTimeoutMS=timeout_ms,
        connectTimeoutMS=timeout_ms,
    )


def get_database(uri: str | None = None) -> Database:
    db_name = os.environ.get("MONGO_DB", "uniroom")
    log_mongo.info("Using database: %s", db_name)
    return get_mongo_client(uri)[db_name]


def fetch_review_status_map(filenames: list[str]) -> dict[str, str]:
    """Read review status for menu display. Does not create indexes."""
    if not filenames or not os.environ.get("MONGO_URI"):
        return {}
    try:
        collection = get_database()[FILES_COLLECTION_NAME]
        return {
            doc["filename"]: doc.get("status", "")
            for doc in collection.find(
                {"filename": {"$in": filenames}},
                {"filename": 1, "status": 1},
            )
        }
    except Exception:
        return {}


def get_listings_collection(db: Database | None = None) -> Collection:
    database = get_database() if db is None else db
    collection = database[COLLECTION_NAME]
    ensure_listing_indexes(collection)
    return collection


def get_reviewed_files_collection(db: Database | None = None) -> Collection:
    database = get_database() if db is None else db
    collection = database[FILES_COLLECTION_NAME]
    ensure_reviewed_file_indexes(collection)
    return collection


def _is_index_key_conflict(exc: BaseException) -> bool:
    return isinstance(exc, OperationFailure) and exc.code == 86


def _ensure_named_index(
    collection: Collection,
    keys: list[tuple[str, Any]],
    *,
    name: str,
    **kwargs: Any,
) -> None:
    """Create a named index, replacing it when key specs changed (schema migration)."""
    desired_key = list(keys)
    existing = collection.index_information().get(name)
    if existing is not None:
        existing_key = list(existing.get("key") or [])
        if existing_key != desired_key:
            log_mongo.info(
                "Replacing stale index %r on %s: %s -> %s",
                name,
                collection.name,
                existing_key,
                desired_key,
            )
            collection.drop_index(name)

    try:
        collection.create_index(keys, name=name, **kwargs)
    except OperationFailure as exc:
        if not _is_index_key_conflict(exc):
            raise
        log_mongo.info(
            "Index key conflict for %r on %s; dropping and recreating",
            name,
            collection.name,
        )
        collection.drop_index(name)
        collection.create_index(keys, name=name, **kwargs)


def ensure_listing_indexes(collection: Collection) -> None:
    log_mongo.debug("Ensuring indexes on %s", COLLECTION_NAME)
    _ensure_named_index(
        collection,
        [("contactDetails.phone", ASCENDING)],
        name="idx_contact_phone",
    )
    _ensure_named_index(collection, [("rentPrice", ASCENDING)], name="idx_rent_price")
    _ensure_named_index(collection, [("city", ASCENDING)], name="idx_city")
    _ensure_named_index(
        collection,
        [("source_photo_filename", ASCENDING)],
        name="idx_source_photo_filename",
    )
    _ensure_named_index(
        collection,
        [("has_missing_data", ASCENDING)],
        name="idx_has_missing_data",
    )
    _ensure_named_index(
        collection,
        [("city", ASCENDING), ("type", ASCENDING), ("rentPrice", ASCENDING)],
        name="idx_dedup_fuzzy",
    )
    _ensure_named_index(collection, [("status", ASCENDING)], name="idx_status")
    try:
        _ensure_named_index(
            collection,
            [("location", "2dsphere")],
            name="idx_location_geo",
        )
    except Exception as exc:
        log_mongo.debug("2dsphere index skipped: %s", exc)


def ensure_reviewed_file_indexes(collection: Collection) -> None:
    log_mongo.debug("Ensuring indexes on %s", FILES_COLLECTION_NAME)
    _ensure_named_index(
        collection,
        [("filename", ASCENDING)],
        unique=True,
        name="idx_filename_unique",
    )
    _ensure_named_index(collection, [("status", ASCENDING)], name="idx_review_status")
    _ensure_named_index(
        collection,
        [("reviewed_at", ASCENDING)],
        name="idx_reviewed_at",
    )


def is_file_already_reviewed(filename: str, files_collection: Collection) -> bool:
    doc = files_collection.find_one(
        {"filename": filename, "status": "completed"},
        {"_id": 1},
    )
    return doc is not None


def record_reviewed_file(
    record: ReviewedFileRecord,
    files_collection: Collection,
) -> None:
    doc = record.model_dump()
    doc["reviewed_at"] = record.reviewed_at
    files_collection.update_one(
        {"filename": record.filename},
        {"$set": doc},
        upsert=True,
    )
    log_mongo.info(
        "Reviewed file record: %s status=%s extracted=%d inserted=%d updated=%d",
        record.filename,
        record.status,
        record.listings_extracted,
        record.listings_inserted,
        record.listings_updated,
    )


# ---------------------------------------------------------------------------
# Pipeline orchestration
# ---------------------------------------------------------------------------


def process_image(
    image_path: str | Path,
    listings_collection: Collection | None = None,
    files_collection: Collection | None = None,
    client: OpenAI | None = None,
    force: bool = False,
    max_dimension: int = 2048,
    output_format: OutputFormat | None = None,
    save_png: bool = False,
    png_output_path: str | Path | None = None,
    nim_stream: bool | None = None,
) -> dict:
    """Run full pipeline for one image."""
    image_path = Path(image_path).resolve()
    filename = image_path.name
    log_pipeline.info("=" * 60)
    fmt = resolve_output_format(output_format)
    log_pipeline.info(
        "Starting pipeline for %s (force=%s, format=%s, save_png=%s)",
        filename,
        force,
        fmt,
        save_png,
    )
    db = get_database()
    if listings_collection is None:
        listings_collection = get_listings_collection(db)
    if files_collection is None:
        files_collection = get_reviewed_files_collection(db)
    client = client or get_nim_client()

    if not force and is_file_already_reviewed(filename, files_collection):
        log_pipeline.info("Skipping %s — already reviewed (use --force to reprocess)", filename)
        record_reviewed_file(
            ReviewedFileRecord(
                filename=filename,
                file_path=str(image_path),
                file_format=image_path.suffix.lower().lstrip(".") or "unknown",
                file_size_bytes=image_path.stat().st_size,
                status="skipped",
                reviewed_at=datetime.now(timezone.utc),
                error_message="Already reviewed (use --force to reprocess)",
            ),
            files_collection,
        )
        return {
            "image": str(image_path),
            "filename": filename,
            "status": "skipped",
            "extracted": 0,
            "inserted": 0,
            "updated": 0,
            "with_missing_data": 0,
            "errors": [],
        }

    now = datetime.now(timezone.utc)
    file_format = image_path.suffix.lower().lstrip(".") or "unknown"
    file_size = image_path.stat().st_size

    record_reviewed_file(
        ReviewedFileRecord(
            filename=filename,
            file_path=str(image_path),
            file_format=file_format,
            file_size_bytes=file_size,
            status="processing",
            reviewed_at=now,
        ),
        files_collection,
    )

    inserted = 0
    updated = 0
    with_missing = 0
    errors: list[str] = []

    try:
        log_pipeline.info("[1/4] Compressing and encoding image")
        if save_png or fmt == "png":
            png_path = save_image_as_png(
                image_path,
                max_dimension=max_dimension,
                output_path=png_output_path,
            )
            log_pipeline.info("PNG file: %s", png_path)
        image_b64, mime_type = compress_and_encode_image(
            image_path,
            max_dimension=max_dimension,
            output_format=fmt,
        )
        log_pipeline.info(
            "[2/4] Extracting listings via NVIDIA NIM (stream=%s)",
            resolve_nim_stream(nim_stream),
        )
        extraction = extract_listings_from_image(
            image_b64, mime_type, client=client, stream=nim_stream
        )

        log_pipeline.info("[3/4] Validating and saving %d listing(s)", len(extraction.listings))
        for i, raw_listing in enumerate(extraction.listings):
            try:
                listing = enrich_listing(raw_listing, source_photo_filename=filename)
                ListingRecord.model_validate(listing.model_dump())
                log_pipeline.info(
                    "Listing[%d]: type=%s rent=%s city=%r missing=%s",
                    i,
                    listing.type,
                    listing.rentPrice,
                    listing.city,
                    listing.missing_fields or "none",
                )
                if listing.has_missing_data:
                    with_missing += 1
                action = upsert_listing(listing, listings_collection)
                log_pipeline.info("Listing[%d] -> %s", i, action)
                if action == "inserted":
                    inserted += 1
                else:
                    updated += 1
            except ValidationError as exc:
                log_pipeline.error("Listing[%d] validation failed: %s", i, exc)
                errors.append(f"listing[{i}]: {exc}")
            except Exception as exc:
                log_pipeline.error("Listing[%d] error: %s", i, exc)
                errors.append(f"listing[{i}]: {exc}")

        log_pipeline.info("[4/4] Recording reviewed file status")
        status: ReviewStatus = "failed" if errors and inserted == 0 and updated == 0 else "completed"
        error_message = "; ".join(errors) if errors else None

        record_reviewed_file(
            ReviewedFileRecord(
                filename=filename,
                file_path=str(image_path),
                file_format=file_format,
                file_size_bytes=file_size,
                status=status,
                reviewed_at=datetime.now(timezone.utc),
                listings_extracted=len(extraction.listings),
                listings_inserted=inserted,
                listings_updated=updated,
                listings_with_missing_data=with_missing,
                error_message=error_message,
            ),
            files_collection,
        )

        log_pipeline.info(
            "Pipeline complete: status=%s extracted=%d inserted=%d updated=%d incomplete=%d",
            status,
            len(extraction.listings),
            inserted,
            updated,
            with_missing,
        )
        log_pipeline.info("=" * 60)
        return {
            "image": str(image_path),
            "filename": filename,
            "status": status,
            "extracted": len(extraction.listings),
            "inserted": inserted,
            "updated": updated,
            "with_missing_data": with_missing,
            "errors": errors,
        }

    except Exception as exc:
        log_pipeline.exception("Pipeline failed for %s: %s", filename, exc)
        record_reviewed_file(
            ReviewedFileRecord(
                filename=filename,
                file_path=str(image_path),
                file_format=file_format,
                file_size_bytes=file_size,
                status="failed",
                reviewed_at=datetime.now(timezone.utc),
                error_message=str(exc),
            ),
            files_collection,
        )
        raise


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract housing listings from bulletin board photos into MongoDB.",
    )
    parser.add_argument(
        "image_path",
        type=Path,
        help="Path to a bulletin board photo (JPEG, PNG, WebP, HEIC)",
    )
    parser.add_argument(
        "--max-dimension",
        type=int,
        default=2048,
        help="Max width/height before downscaling (default: 2048)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reprocess even if this filename was already reviewed successfully",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        help="Logging level: DEBUG, INFO, WARNING (default: LOG_LEVEL env or INFO)",
    )
    parser.add_argument(
        "--format",
        choices=("jpeg", "png"),
        default=None,
        help="Encode image for NIM as JPEG or PNG (default: IMAGE_OUTPUT_FORMAT env or jpeg)",
    )
    parser.add_argument(
        "--save-png",
        action="store_true",
        help="Save converted PNG to input/converted/{name}.png (also enabled when --format png)",
    )
    stream_group = parser.add_mutually_exclusive_group()
    stream_group.add_argument(
        "--stream",
        action="store_true",
        default=None,
        help="Stream NIM response tokens to the terminal (default: on)",
    )
    stream_group.add_argument(
        "--no-stream",
        action="store_true",
        help="Wait for full NIM response before printing",
    )
    args = parser.parse_args()

    setup_logging(args.log_level)

    if not args.image_path.is_file():
        print(f"Error: file not found: {args.image_path}", file=sys.stderr)
        return 1

    output_fmt = resolve_output_format(args.format)
    save_png = args.save_png or output_fmt == "png"
    nim_stream = False if args.no_stream else (True if args.stream else None)

    try:
        result = process_image(
            args.image_path,
            force=args.force,
            max_dimension=args.max_dimension,
            output_format=output_fmt,
            save_png=save_png,
            nim_stream=nim_stream,
        )

        print("Success")
        print(f"  Image:           {result['image']}")
        print(f"  Filename:        {result['filename']}")
        print(f"  Status:          {result['status']}")
        print(f"  Extracted:       {result['extracted']} listing(s)")
        print(f"  Inserted:        {result['inserted']}")
        print(f"  Duplicates:      {result['updated']} (last_seen_date updated)")
        print(f"  Incomplete data: {result['with_missing_data']} listing(s) flagged")
        if result["errors"]:
            print(f"  Errors:          {len(result['errors'])}")
            for err in result["errors"]:
                print(f"    - {err}")
        return 0 if result["status"] != "failed" else 1

    except EnvironmentError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    except ImportError as exc:
        print(f"Missing dependency: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
