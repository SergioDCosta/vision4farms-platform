import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

from django.conf import settings
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import UploadedFile

from apps.messaging.exceptions import MessagingServiceError


MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
ALLOWED_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".txt",
}
ALLOWED_MIME_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/plain",
}


def normalize_attachment_name(original_name):
    filename = Path(original_name or "").name.strip()
    if not filename:
        return "anexo"
    if len(filename) <= 255:
        return filename

    stem = Path(filename).stem
    suffix = Path(filename).suffix
    max_stem = max(1, 255 - len(suffix))
    return f"{stem[:max_stem]}{suffix}"


def build_attachment_path(conversation_id, attachment_name):
    return f"messaging/attachments/{conversation_id}/{uuid.uuid4().hex}_{attachment_name}"


def extract_cloudinary_storage_path(url_value):
    parsed = urlparse(str(url_value or "").strip())
    if "res.cloudinary.com" not in (parsed.netloc or "").lower():
        return None
    if "/upload/" not in parsed.path:
        return None

    suffix = parsed.path.split("/upload/", 1)[1]
    segments = [segment for segment in suffix.split("/") if segment]
    if not segments:
        return None

    public_id_start = 0
    for idx, segment in enumerate(segments):
        if segment.startswith("v") and segment[1:].isdigit():
            public_id_start = idx + 1
            break
    public_id_segments = segments[public_id_start:] if public_id_start < len(segments) else segments
    if not public_id_segments:
        return None
    return unquote("/".join(public_id_segments)).lstrip("/")


def normalize_attachment_storage_value(stored_value):
    raw = str(stored_value or "").strip()
    if not raw:
        return ""

    lower_raw = raw.lower()
    if lower_raw.startswith("http://") or lower_raw.startswith("https://"):
        extracted = extract_cloudinary_storage_path(raw)
        return extracted or raw

    media_url = str(getattr(settings, "MEDIA_URL", "") or "").strip()
    if media_url and raw.startswith(media_url):
        raw = raw[len(media_url):]

    return raw.lstrip("/")


def resolve_attachment_url(stored_value):
    normalized = normalize_attachment_storage_value(stored_value)
    if not normalized:
        return ""

    lower = normalized.lower()
    if lower.startswith("http://") or lower.startswith("https://"):
        return normalized

    try:
        return default_storage.url(normalized)
    except Exception:
        media_url = str(getattr(settings, "MEDIA_URL", "/media/") or "/media/")
        return f"{media_url.rstrip('/')}/{normalized.lstrip('/')}"


def validate_attachment(uploaded_file):
    if not isinstance(uploaded_file, UploadedFile):
        raise MessagingServiceError("Ficheiro inválido.")

    file_size = getattr(uploaded_file, "size", 0) or 0
    if file_size <= 0:
        raise MessagingServiceError("Ficheiro vazio.")
    if file_size > MAX_ATTACHMENT_BYTES:
        raise MessagingServiceError("Ficheiro demasiado grande. Máximo 10MB.")

    attachment_name = normalize_attachment_name(uploaded_file.name)
    extension = Path(attachment_name).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        extension_label = extension or "(sem extensão)"
        raise MessagingServiceError(f"Extensão '{extension_label}' não permitida.")

    content_type = (
        (getattr(uploaded_file, "content_type", None) or "")
        .strip()
        .lower()
        .split(";", 1)[0]
        .strip()
    )
    if not content_type or content_type not in ALLOWED_MIME_TYPES:
        raise MessagingServiceError("Tipo de ficheiro não permitido.")

    return attachment_name, content_type
