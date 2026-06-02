import io
import json
import uuid
from pathlib import Path

from django.conf import settings
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import InMemoryUploadedFile, UploadedFile
from PIL import Image, ImageOps

from apps.settings_app.models import UserPreference


def _listing_photo_url(photo_path):
    if not photo_path:
        return None

    photo_path = str(photo_path).strip()
    if not photo_path:
        return None

    if photo_path.startswith(("http://", "https://")):
        return photo_path

    if photo_path.startswith(settings.MEDIA_URL):
        photo_path = photo_path[len(settings.MEDIA_URL):]

    normalized_path = photo_path.lstrip("/").strip()
    if not normalized_path:
        return None

    try:
        return default_storage.url(normalized_path)
    except Exception:
        return f"{settings.MEDIA_URL}{normalized_path}"


def _producer_profile_photo_url(user):
    if not user:
        return None

    preference = (
        UserPreference.objects
        .filter(user=user)
        .only("profile_photo")
        .first()
    )
    if not preference:
        return None

    return _listing_photo_url(preference.profile_photo)


def _save_listing_photo(producer, uploaded_file):
    if not isinstance(uploaded_file, UploadedFile):
        raise ValueError("O ficheiro enviado para o anúncio é inválido.")

    extension = Path(uploaded_file.name).suffix.lower() or ".jpg"
    filename = (
        f"marketplace/listings/{producer.id}/"
        f"{uuid.uuid4().hex}{extension}"
    )
    return default_storage.save(filename, uploaded_file)


def _delete_uploaded_file(file_path):
    if not file_path:
        return
    file_path = str(file_path).strip()
    if not file_path:
        return
    if file_path.startswith(("http://", "https://")):
        return
    if file_path.startswith(settings.MEDIA_URL):
        file_path = file_path[len(settings.MEDIA_URL):]
    file_path = file_path.lstrip("/").strip()
    if not file_path:
        return
    try:
        if default_storage.exists(file_path):
            default_storage.delete(file_path)
    except Exception:
        return


def _parse_photo_crop_payload(payload):
    if not payload:
        return None

    try:
        parsed = json.loads(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None

    try:
        x = float(parsed.get("x", 0))
        y = float(parsed.get("y", 0))
        w = float(parsed.get("w", 0))
        h = float(parsed.get("h", 0))
    except (TypeError, ValueError):
        return None

    x = max(0.0, min(x, 1.0))
    y = max(0.0, min(y, 1.0))
    w = max(0.0, min(w, 1.0))
    h = max(0.0, min(h, 1.0))

    if w <= 0 or h <= 0:
        return None

    if x + w > 1.0:
        w = 1.0 - x
    if y + h > 1.0:
        h = 1.0 - y

    if w <= 0 or h <= 0:
        return None

    return x, y, w, h


def _maybe_crop_uploaded_photo(uploaded_file, crop_payload):
    crop_data = _parse_photo_crop_payload(crop_payload)
    if not crop_data:
        try:
            uploaded_file.seek(0)
        except Exception:
            pass
        return uploaded_file

    try:
        uploaded_file.seek(0)
        with Image.open(uploaded_file) as image:
            image = ImageOps.exif_transpose(image)
            width, height = image.size

            left = int(round(crop_data[0] * width))
            top = int(round(crop_data[1] * height))
            right = int(round((crop_data[0] + crop_data[2]) * width))
            bottom = int(round((crop_data[1] + crop_data[3]) * height))

            left = max(0, min(left, width - 1))
            top = max(0, min(top, height - 1))
            right = max(left + 1, min(right, width))
            bottom = max(top + 1, min(bottom, height))

            if left == 0 and top == 0 and right == width and bottom == height:
                uploaded_file.seek(0)
                return uploaded_file

            cropped = image.crop((left, top, right, bottom))

            output = io.BytesIO()
            source_format = (image.format or "JPEG").upper()
            save_format = source_format if source_format in {"JPEG", "JPG", "PNG", "WEBP"} else "JPEG"

            if save_format in {"JPEG", "JPG"} and cropped.mode not in {"RGB", "L"}:
                cropped = cropped.convert("RGB")
                save_format = "JPEG"

            save_kwargs = {"format": save_format}
            if save_format == "JPEG":
                save_kwargs["quality"] = 90
                save_kwargs["optimize"] = True

            cropped.save(output, **save_kwargs)
            output.seek(0)

            content_type_map = {
                "JPEG": "image/jpeg",
                "JPG": "image/jpeg",
                "PNG": "image/png",
                "WEBP": "image/webp",
            }
            content_type = content_type_map.get(save_format, uploaded_file.content_type or "image/jpeg")

            return InMemoryUploadedFile(
                file=output,
                field_name=getattr(uploaded_file, "field_name", None),
                name=uploaded_file.name,
                content_type=content_type,
                size=output.getbuffer().nbytes,
                charset=None,
            )
    except Exception:
        try:
            uploaded_file.seek(0)
        except Exception:
            pass
        return uploaded_file
