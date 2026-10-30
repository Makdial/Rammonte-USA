"""Cloudflare R2 storage integration.

Videos, thumbnails and the extracted MP3 audio are stored in a Cloudflare R2
bucket. R2 speaks the S3 API, so this module drives it with ``boto3``
(streaming, multipart uploads) against the R2 S3 endpoint.

Configure the following values in ``rammonte_usa/.env``:

    R2_ACCOUNT_ID=<your Cloudflare account id>
    R2_ACCESS_KEY_ID=<R2 access key id>
    R2_SECRET_ACCESS_KEY=<R2 secret access key>
    R2_BUCKET=fazakkir
    R2_PUBLIC_BASE_URL=https://pub-xxxxxxxx.r2.dev   # optional

``R2_PUBLIC_BASE_URL`` is optional. When it is set (and the bucket has public
access enabled) videos are served straight from that public URL. When it is
missing, the app instead generates short-lived *presigned* URLs on demand, so
the bucket never needs to be public.
"""
from __future__ import annotations

import base64
import binascii
import mimetypes
import os
import uuid
from pathlib import Path
from typing import BinaryIO, Iterator
from urllib.parse import quote

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

DEFAULT_BUCKET = "rammonte-shorts"
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".avi", ".mkv", ".m4v", ".ogv"}
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_AUDIO_EXTENSIONS = {".mp3"}

# Object keys created by this app. Used to tell R2 objects apart from legacy
# records (e.g. old ImageKit paths) that should keep their stored URL.
OBJECT_PREFIXES = ("videos/", "thumbnails/", "audio/")

# Anything larger than this is uploaded as a multipart upload, which is what R2
# recommends for files over a few megabytes. 16 MB parts keep memory use low
# while still moving multi-gigabyte videos efficiently.
MULTIPART_THRESHOLD = 16 * 1024 * 1024
MULTIPART_CHUNKSIZE = 16 * 1024 * 1024
MULTIPART_CONCURRENCY = 4

# Lifetime of generated presigned URLs (SigV4 allows at most 7 days).
PRESIGN_EXPIRY = 12 * 60 * 60

_client_cache = None


class StorageError(RuntimeError):
    """Raised when storage is not configured or rejects an operation."""


def _setting(name: str) -> str:
    # name is like "R2_ACCOUNT_ID"; shorts live in their own bucket/credentials
    # but fall back to the main R2 settings when a shorts-specific value is
    # not provided.
    short_name = name[len("R2_"):] if name.startswith("R2_") else name
    value = os.environ.get("R2_SHORTS_" + short_name, "").strip()
    if value:
        return value
    return os.environ.get("R2_" + short_name, "").strip()


def is_configured() -> bool:
    """Return True when the R2 credentials are present."""
    return bool(
        _setting("R2_ACCOUNT_ID")
        and _setting("R2_ACCESS_KEY_ID")
        and _setting("R2_SECRET_ACCESS_KEY")
        and _setting("R2_BUCKET")
    )


def _bucket() -> str:
    return _setting("R2_BUCKET") or DEFAULT_BUCKET


def _client():
    """Return a cached boto3 S3 client for the R2 endpoint."""
    global _client_cache
    if _client_cache is not None:
        return _client_cache

    account_id = _setting("R2_ACCOUNT_ID")
    access_key = _setting("R2_ACCESS_KEY_ID")
    secret_key = _setting("R2_SECRET_ACCESS_KEY")

    if not (account_id and access_key and secret_key):
        raise StorageError(
            "Cloudflare R2 is not configured. Add R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, "
            "R2_SECRET_ACCESS_KEY and R2_BUCKET to your .env file."
        )

    _client_cache = boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            connect_timeout=30,
            read_timeout=300,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )
    return _client_cache


def _transfer_config() -> TransferConfig:
    return TransferConfig(
        multipart_threshold=MULTIPART_THRESHOLD,
        multipart_chunksize=MULTIPART_CHUNKSIZE,
        max_concurrency=MULTIPART_CONCURRENCY,
    )


def _object_path(folder: str, file_name: str, allowed_extensions: set[str] | None = None) -> str:
    """Build a collision-free object key such as ``videos/8f3...c2.mp4``."""
    extension = Path(file_name or "").suffix.lower()
    if allowed_extensions is not None and extension not in allowed_extensions:
        extension = ""
    return f"{folder.strip('/')}/{uuid.uuid4().hex}{extension}"


def is_object_path(path: str) -> bool:
    """True when ``path`` is an object key created by this app."""
    return bool(path) and path.startswith(OBJECT_PREFIXES)


def build_public_url(path: str) -> str:
    """Return the public (unauthenticated) URL for an object."""
    base = _setting("R2_PUBLIC_BASE_URL").rstrip("/")
    if not base:
        raise StorageError("R2_PUBLIC_BASE_URL is not set.")
    return f"{base}/{path}"


def generate_presigned_url(path: str, expires_in: int = PRESIGN_EXPIRY) -> str:
    """Return a short-lived signed URL that lets anyone read ``path``."""
    try:
        return _client().generate_presigned_url(
            "get_object",
            Params={"Bucket": _bucket(), "Key": path},
            ExpiresIn=expires_in,
        )
    except BotoCoreError as exc:
        raise StorageError(f"Could not sign the URL for {path!r}: {exc}") from exc


def stored_url(path: str) -> str:
    """A stable URL persisted in the database.

    Only the public URL is stable enough to store; when the bucket is private
    the playback URL is generated on demand, so this returns "" instead of a
    URL that would expire.
    """
    base = _setting("R2_PUBLIC_BASE_URL")
    return f"{base.rstrip('/')}/{path}" if base else ""


def resolve_url(path: str, stored_url_value: str = "") -> str:
    """Return a browser-loadable URL for ``path``.

    Prefers the public bucket URL when configured, otherwise generates a
    presigned URL. Legacy records (paths not created by this app) keep using
    their previously stored URL.
    """
    if not is_object_path(path):
        return stored_url_value or ""
    try:
        base = _setting("R2_PUBLIC_BASE_URL")
        if base:
            return build_public_url(path)
        return generate_presigned_url(path)
    except StorageError:
        return stored_url_value or ""


def get_object(path: str, range_header: str | None = None) -> dict:
    """Fetch an object (optionally a byte range) straight from R2.

    Used to stream the extracted MP3 through Django so the browser, and the
    offline service worker, only ever talk to this same origin.
    """
    if not path:
        raise StorageError("No object path supplied.")

    params = {"Bucket": _bucket(), "Key": path}
    if range_header:
        params["Range"] = range_header

    try:
        return _client().get_object(**params)
    except ClientError as exc:
        error = exc.response.get("Error", {})
        raise StorageError(
            f"R2 get failed ({error.get('Code')}): {error.get('Message', '')}"
        ) from exc
    except BotoCoreError as exc:
        raise StorageError(f"Could not reach R2: {exc}") from exc


def upload_object(path: str, data: bytes | BinaryIO, content_type: str | None = None, upsert: bool = False) -> str:
    """Upload content to ``path`` and return a displayable URL.

    ``data`` may be raw ``bytes`` or a binary file-like object. File objects are
    streamed (and multipart-uploaded when large) so multi-gigabyte videos never
    sit in memory.
    """
    if content_type is None:
        content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"

    client = _client()
    bucket = _bucket()

    try:
        if isinstance(data, (bytes, bytearray)):
            client.put_object(Bucket=bucket, Key=path, Body=bytes(data), ContentType=content_type)
        else:
            client.upload_fileobj(
                data,
                bucket,
                path,
                ExtraArgs={"ContentType": content_type},
                Config=_transfer_config(),
            )
    except ClientError as exc:
        error = exc.response.get("Error", {})
        code = error.get("Code", "Unknown")
        message = error.get("Message", "")
        raise StorageError(f"R2 upload failed ({code}): {message}") from exc
    except BotoCoreError as exc:
        raise StorageError(f"Could not reach R2: {exc}") from exc

    return resolve_url(path)


def delete_object(path: str) -> None:
    """Remove an object from the bucket. Missing objects are ignored."""
    if not path:
        return

    client = _client()
    try:
        client.delete_object(Bucket=_bucket(), Key=path)
    except ClientError as exc:
        error = exc.response.get("Error", {})
        if error.get("Code") in {"NoSuchKey", "404"}:
            return
        raise StorageError(
            f"R2 delete failed ({error.get('Code')}): {error.get('Message', '')}"
        ) from exc
    except BotoCoreError as exc:
        raise StorageError(f"Could not reach R2: {exc}") from exc


def _decode_image_payload(file_data) -> bytes:
    """Decode a data-URL (or raw bytes) image payload."""
    if isinstance(file_data, (bytes, bytearray)):
        return bytes(file_data)

    text = str(file_data)
    encoded = text.split(",", 1)[1] if text.startswith("data:") and "," in text else text
    try:
        return base64.b64decode(encoded)
    except (binascii.Error, ValueError) as exc:
        raise StorageError("Invalid image data supplied for the thumbnail.") from exc


# --- High level helpers used by the views ---------------------------------

def upload_video(file_data: bytes | BinaryIO, file_name: str, folder: str = "videos") -> dict:
    path = _object_path(folder, file_name, ALLOWED_VIDEO_EXTENSIONS)
    content_type = mimetypes.guess_type(file_name)[0] or "video/mp4"
    upload_object(path, file_data, content_type=content_type)
    return {"path": path, "url": stored_url(path)}


# The picture types that can be recognised from the first few bytes of the
# file, so a PNG is not stored under a ".jpg" name.
_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
)


def _sniff_image(data: bytes) -> tuple[str, str]:
    """Return the ``(content type, extension)`` of an image by its header."""
    for signature, content_type, extension in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return content_type, extension
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    # Anything else is stored as a JPEG, which is what the browser is told.
    return "image/jpeg", ".jpg"


def is_image(data: bytes) -> bool:
    """True when the bytes really start like a picture a browser can show.

    Used to reject a renamed file before it reaches the bucket, where it would
    only become a broken picture on the site.
    """
    if data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")):
        return True
    return data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def upload_thumbnail(file_data, file_name: str, folder: str = "thumbnails") -> dict:
    data = _decode_image_payload(file_data)
    content_type, extension = _sniff_image(data)
    # The picture is stored under the name it really has, so the browser is
    # told the right type instead of being handed a PNG called ".jpg".
    name = Path(file_name or "thumbnail").stem + extension
    path = _object_path(folder, name, ALLOWED_IMAGE_EXTENSIONS)
    upload_object(path, data, content_type=content_type)
    return {"path": path, "url": stored_url(path)}


def upload_audio(file_data: bytes | BinaryIO, file_name: str, folder: str = "audio") -> dict:
    path = _object_path(folder, file_name, ALLOWED_AUDIO_EXTENSIONS)
    content_type = mimetypes.guess_type(path)[0] or "audio/mpeg"
    upload_object(path, file_data, content_type=content_type)
    return {"path": path, "url": stored_url(path)}


def download_url(path: str, file_name: str = "", stored_url_value: str = "") -> str:
    """Return a URL that downloads ``path`` under the given file name.

    Downloads are always served through a presigned URL so the
    ``Content-Disposition`` header can be attached (this works whether or not
    the bucket is public). Legacy records fall back to their stored URL.
    """
    if not is_object_path(path):
        return stored_url_value or ""

    params = {"Bucket": _bucket(), "Key": path}
    if file_name:
        ascii_name = file_name.encode("ascii", "ignore").decode().strip() or "download"
        params["ResponseContentDisposition"] = (
            f"attachment; filename=\"{ascii_name}\"; "
            f"filename*=UTF-8''{quote(file_name)}"
        )
    try:
        return _client().generate_presigned_url(
            "get_object", Params=params, ExpiresIn=PRESIGN_EXPIRY
        )
    except (BotoCoreError, StorageError):
        return stored_url_value or ""


def delete_file(path: str) -> None:
    delete_object(path)


# --- Usage accounting (used by the administrator dashboard) ----------------

def list_objects(prefix: str = "") -> Iterator[dict]:
    """Yield every object under ``prefix`` as ``{"key", "size", ...}``.

    The S3 paginator is used so a bucket with many objects is walked page by
    page without holding the whole listing in memory.
    """
    client = _client()
    paginator = client.get_paginator("list_objects_v2")
    try:
        for page in paginator.paginate(Bucket=_bucket(), Prefix=prefix):
            for item in page.get("Contents", []):
                yield {
                    "key": item["Key"],
                    "size": int(item.get("Size", 0)),
                    "last_modified": item.get("LastModified"),
                }
    except ClientError as exc:
        error = exc.response.get("Error", {})
        raise StorageError(
            f"R2 list failed ({error.get('Code')}): {error.get('Message', '')}"
        ) from exc
    except BotoCoreError as exc:
        raise StorageError(f"Could not reach R2: {exc}") from exc


def bucket_usage(prefix: str = "") -> dict:
    """Return ``{"total_bytes", "object_count", "by_prefix"}`` for the bucket."""
    total = 0
    count = 0
    by_prefix: dict[str, int] = {}
    for obj in list_objects(prefix):
        total += obj["size"]
        count += 1
        top = obj["key"].split("/", 1)[0] + "/"
        by_prefix[top] = by_prefix.get(top, 0) + obj["size"]
    return {"total_bytes": total, "object_count": count, "by_prefix": by_prefix}
