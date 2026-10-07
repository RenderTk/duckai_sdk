"""Duck.ai's inline attachment protocol, observed in the live web client.

PDFs stay binary/base64; images are decoded, oriented, resized, and encoded as
WebP like the browser canvas. No document text is substituted for the attachment.
"""

import base64
import binascii
import io
import warnings
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from duckai.config import Settings
from duckai.errors import AttachmentError
from duckai.models import ChatRequest

PDF_BYTES = 5 * 1024 * 1024
PDF_COUNT = 3
PDF_PAGES = 15
IMAGE_MIMES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}


@dataclass(frozen=True)
class AttachmentProfile:
    max_dimension: int = 512
    images_per_message: int = 3
    images_per_conversation: int = 5
    supports_pdf: bool = True


# From entry.duckai.907f8c15e2149c6a12ec.js on October 6, 2026. Limits
# here are the anonymous/FREE tier; account-tier limits do not apply to this API.
PROFILES = {
    "gpt-6-luna": AttachmentProfile(),
    "gpt-5.4-mini": AttachmentProfile(),
    "claude-haiku-4-5": AttachmentProfile(),
    "gpt-5.6-terra": AttachmentProfile(1024, 3, 10),
    "gpt-5.6-sol": AttachmentProfile(1024, 3, 10),
    "claude-sonnet-4-6": AttachmentProfile(1024, 3, 10),
    "claude-opus-4-8": AttachmentProfile(1024, 3, 10),
    "tinfoil/gemma4-31b": AttachmentProfile(supports_pdf=False),
}


def fail(status: int, code: str, message: str):
    raise AttachmentError(status, detail={"error": code, "message": message})


def profile_for(model: str) -> AttachmentProfile:
    profile = PROFILES.get(model)
    if profile is None:
        fail(
            422, "attachments_model_unsupported", f"No attachment support was observed for {model}."
        )
    return profile


def decode_base64(value: Any) -> bytes:
    if not isinstance(value, str):
        fail(422, "invalid_attachment", "Attachment data must be a base64 string.")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        fail(422, "invalid_attachment", "Attachment data is not valid base64.")


class Attachments:
    def __init__(self, settings: Settings):
        self.settings = settings

    def limits(self, model: str) -> dict[str, Any]:
        profile = profile_for(model)
        return {
            "model": model,
            "image_mime_types": list(IMAGE_MIMES.values()),
            "pdf_supported": profile.supports_pdf,
            "pdfs_per_conversation": PDF_COUNT if profile.supports_pdf else 0,
            "pdf_bytes_per_file_and_conversation": PDF_BYTES,
            "pdf_pages_per_file": PDF_PAGES,
            "images_per_message": profile.images_per_message,
            "images_per_conversation": profile.images_per_conversation,
            "image_max_dimension": profile.max_dimension,
            "image_message_text_characters": 4500,
            "proxy_max_image_upload_bytes": self.settings.max_image_upload_bytes,
            "proxy_max_image_pixels": self.settings.max_image_pixels,
        }

    def _check_pdf(self, data: bytes):
        if len(data) > PDF_BYTES:
            fail(413, "pdf_too_large", "Anonymous Duck.ai PDFs must fit within 5 MiB.")
        if not data.startswith(b"%PDF-"):
            fail(422, "invalid_pdf", "The uploaded file is not a PDF.")
        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                fail(422, "encrypted_pdf", "Upload an unencrypted PDF.")
            count = len(reader.pages)
        except (PyPdfError, ValueError, KeyError, TypeError, RecursionError):
            fail(422, "invalid_pdf", "The PDF could not be parsed.")
        if not count:
            fail(422, "empty_pdf", "The PDF has no pages.")
        if count > PDF_PAGES:
            fail(422, "pdf_page_limit", "Anonymous Duck.ai supports at most 15 pages per PDF.")

    def _prepare(
        self, data: bytes, filename: str, mime: str, model: str, validate_only: bool = False
    ) -> dict[str, Any]:
        profile = profile_for(model)
        mime = mime.lower().split(";", 1)[0].strip()
        if mime == "image/jpg":
            mime = "image/jpeg"
        generic = mime in {"", "application/octet-stream"}
        if mime == "application/pdf" or (generic and data.startswith(b"%PDF-")):
            if not profile.supports_pdf:
                fail(422, "pdf_model_unsupported", f"{model} does not support PDF attachments.")
            self._check_pdf(data)
            safe_name = filename.replace("\\", "/").rsplit("/", 1)[-1]
            safe_name = "".join(c for c in safe_name if ord(c) >= 32 and ord(c) != 127)
            return {
                "type": "file",
                "content": base64.b64encode(data).decode(),
                "encoding": "base64",
                "mimeType": "application/pdf",
                "filename": safe_name or "document.pdf",
            }
        if not generic and mime not in IMAGE_MIMES.values():
            fail(
                415, "unsupported_file_type", "Duck.ai accepts PDF, PNG, JPEG, WebP, and GIF files."
            )
        if len(data) > self.settings.max_image_upload_bytes:
            fail(413, "image_too_large", "Image exceeds the proxy upload byte limit.")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as original:
                    detected = IMAGE_MIMES.get(original.format)
                    if detected is None:
                        fail(
                            415,
                            "unsupported_file_type",
                            "Only PNG, JPEG, WebP, and GIF images are supported.",
                        )
                    if not generic and mime != detected:
                        fail(
                            415,
                            "file_type_mismatch",
                            "Image contents do not match the declared MIME type.",
                        )
                    if original.width * original.height > self.settings.max_image_pixels:
                        fail(413, "image_pixel_limit", "Image exceeds the proxy pixel limit.")
                    if validate_only:
                        original.load()
                        return {}
                    # Like createImageBitmap, respect EXIF and use the first GIF frame.
                    image = ImageOps.exif_transpose(original).convert("RGBA")
                    scale = min(
                        1, profile.max_dimension / image.width, profile.max_dimension / image.height
                    )
                    size = (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
                    if image.size != size:
                        image = image.resize(size, Image.Resampling.LANCZOS)
                    output = io.BytesIO()
                    image.save(output, format="WEBP", quality=90)
        except (Image.DecompressionBombError, Image.DecompressionBombWarning):
            fail(413, "image_pixel_limit", "Image exceeds the decoder pixel limit.")
        except (UnidentifiedImageError, OSError, ValueError):
            fail(422, "invalid_image", "Image could not be decoded.")
        return {
            "type": "image",
            "mimeType": "image/webp",
            "image": "data:image/webp;base64," + base64.b64encode(output.getvalue()).decode(),
        }

    def prepare(self, data: bytes, filename: str, mime: str, model: str) -> dict[str, Any]:
        if not data:
            fail(422, "empty_attachment", "Files must not be empty.")
        return self._prepare(data, filename, mime, model)

    def validate_payload(self, body: ChatRequest):
        messages = [m for m in body.messages if m.role == "user" and isinstance(m.content, list)]
        if not any(p.get("type") in {"image", "file"} for m in messages for p in m.content):
            return
        profile = profile_for(body.model)
        pdf_count = total_pdf_bytes = total_images = 0
        for message in messages:
            images = 0
            for part in message.content:
                if part.get("type") == "file":
                    if not profile.supports_pdf or part.get("mimeType") != "application/pdf":
                        fail(
                            422,
                            "pdf_model_unsupported",
                            "This model accepts only supported PDF attachments.",
                        )
                    if part.get("encoding") != "base64":
                        fail(422, "invalid_attachment", "PDF encoding must be base64.")
                    encoded = part.get("content")
                    if isinstance(encoded, str) and len(encoded) > ((PDF_BYTES + 2) // 3) * 4:
                        fail(413, "pdf_too_large", "Anonymous Duck.ai PDFs must fit within 5 MiB.")
                    data = decode_base64(encoded)
                    self._check_pdf(data)
                    pdf_count += 1
                    total_pdf_bytes += len(data)
                elif part.get("type") == "image":
                    value, mime = part.get("image"), part.get("mimeType")
                    prefix = f"data:{mime};base64,"
                    if (
                        mime not in IMAGE_MIMES.values()
                        or not isinstance(value, str)
                        or not value.startswith(prefix)
                    ):
                        fail(
                            422,
                            "invalid_attachment",
                            "Images must use Duck.ai image parts with a matching base64 data URL.",
                        )
                    if len(value) > self.settings.max_image_upload_bytes * 4 // 3 + 256:
                        fail(413, "image_too_large", "Image exceeds the proxy upload byte limit.")
                    data = decode_base64(value[len(prefix) :])
                    if not data:
                        fail(422, "invalid_attachment", "Image attachment must not be empty.")
                    # Validate encoded bytes as well as the declared data URL type.
                    self._prepare(data, "", mime, body.model, validate_only=True)
                    images += 1
            total_images += images
            if images > profile.images_per_message:
                fail(
                    422, "image_count_limit", "Duck.ai supports at most 3 images per user message."
                )
            text = "".join(
                p.get("text", "")
                for p in message.content
                if p.get("type") == "text" and isinstance(p.get("text"), str)
            )
            if images and len(text) > 4500:
                fail(
                    422,
                    "image_text_limit",
                    "A message with images supports at most 4500 text characters.",
                )
        if total_images > profile.images_per_conversation:
            fail(
                422,
                "image_conversation_limit",
                "Too many images in this conversation for the selected model.",
            )
        if pdf_count > PDF_COUNT:
            fail(
                422,
                "pdf_count_limit",
                "Anonymous Duck.ai supports at most 3 PDFs per conversation.",
            )
        if total_pdf_bytes > PDF_BYTES:
            fail(
                413,
                "pdf_combined_size",
                "All PDFs in the conversation must fit within 5 MiB combined.",
            )
