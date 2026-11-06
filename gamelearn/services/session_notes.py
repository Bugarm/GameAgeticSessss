"""Validate text, embedded pictures, and approved YouTube embeds in notes."""
import json
import base64
import binascii
import re
from urllib.parse import urlsplit

MAX_NOTES_LENGTH = 100000
MAX_NOTES_BYTES = 8 * 1024 * 1024
MAX_IMAGE_BYTES = 2 * 1024 * 1024


def valid_youtube_embed(value):
    return isinstance(value, str) and re.fullmatch(
        r"https://www\.youtube-nocookie\.com/embed/[A-Za-z0-9_-]{11}", value
    ) is not None


def valid_link(value):
    if not isinstance(value, str) or not value or len(value) > 2048 or any(character.isspace() for character in value):
        return False
    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https"}:
        return bool(parsed.netloc)
    if parsed.scheme == "mailto":
        return bool(parsed.path)
    return False


def validate_image(value):
    if not isinstance(value, str):
        raise ValueError("Invalid notes image.")
    match = re.fullmatch(r"data:image/(png|jpeg|gif|webp);base64,([A-Za-z0-9+/]*={0,2})", value)
    if not match:
        raise ValueError("Use embedded PNG, JPEG, GIF, or WebP pictures only.")
    try:
        data = base64.b64decode(match[2], validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("Invalid image encoding.") from None
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Each picture must be at most 2 MB.")
    signatures = {
        "png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        "jpeg": data.startswith(b"\xff\xd8\xff"),
        "gif": data.startswith((b"GIF87a", b"GIF89a")),
        "webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP",
    }
    if not signatures[match[1]]:
        raise ValueError("Picture contents do not match their image type.")



def validate_notes(raw):
    if raw is None:
        return None
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_NOTES_BYTES:
        raise ValueError("Notes including pictures must fit within 8 MB.")
    try:
        delta = json.loads(raw)
    except (ValueError, RecursionError):
        raise ValueError("Invalid notes format.") from None
    if not isinstance(delta, dict) or set(delta) != {"ops"} or not isinstance(delta["ops"], list):
        raise ValueError("Invalid notes format.")
    text = []
    image_chars = 0
    has_embed = False
    for op in delta["ops"]:
        if not isinstance(op, dict) or set(op) - {"insert", "attributes"}:
            raise ValueError("Invalid notes operation.")
        insert = op.get("insert")
        if isinstance(insert, str):
            text.append(insert)
        elif isinstance(insert, dict) and set(insert) == {"image"}:
            validate_image(insert["image"])
            image_chars += len(insert["image"])
            has_embed = True
        elif isinstance(insert, dict) and set(insert) == {"video"}:
            if not valid_youtube_embed(insert["video"]):
                raise ValueError("Use a valid YouTube video link only.")
            has_embed = True
        else:
            raise ValueError("Notes support text, embedded pictures, and YouTube videos only.")
        attrs = op.get("attributes", {})
        if not isinstance(attrs, dict):
            raise ValueError("Invalid notes formatting.")
        for key, value in attrs.items():
            valid = (
                (isinstance(insert, str) and key in {"bold", "italic", "underline", "strike", "blockquote", "code"} and type(value) is bool)
                or (isinstance(insert, str) and key == "header" and type(value) is int and value in {1, 2, 3})
                or (isinstance(insert, str) and key == "list" and isinstance(value, str) and value in {"ordered", "bullet"})
                or (isinstance(insert, str) and key == "link" and valid_link(value))
                or (isinstance(insert, dict) and "image" in insert and key == "width" and isinstance(value, str)
                    and value.isdigit() and 25 <= int(value) <= 100 and int(value) % 5 == 0)
                or (isinstance(insert, dict) and "image" in insert and key == "imageAlign" and value in {"left", "center", "right"})
            )
            if not valid:
                raise ValueError("Unsupported notes formatting.")
    if not has_embed and not "".join(text).strip():
        return None
    # Keep the same size budget for saved and imported notes.
    result = json.dumps(delta, ensure_ascii=False, separators=(",", ":"))
    if len(result) - image_chars > MAX_NOTES_LENGTH:
        raise ValueError("Notes are too long.")
    if len(result.encode("utf-8")) > MAX_NOTES_BYTES:
        raise ValueError("Notes including pictures must fit within 8 MB.")
    return result
