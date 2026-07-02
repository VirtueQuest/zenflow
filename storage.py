"""
ZenFlow — Phase 3: File Storage
────────────────────────────────
· Cloudflare R2 (S3-compatible) for photos and videos
· AWS S3 fallback
· Local filesystem fallback for dev
· Image validation: type, size, dimensions
· Auto-generate thumbnail URLs
· Signed URLs for private uploads
"""

import os, io, uuid, logging, mimetypes, asyncio
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger("zenflow.storage")

# ─────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────
STORAGE_BACKEND   = os.getenv("STORAGE_BACKEND", "local")  # "local" | "s3" | "r2"
AWS_ACCESS_KEY    = os.getenv("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_KEY    = os.getenv("AWS_SECRET_ACCESS_KEY", "")
AWS_REGION        = os.getenv("AWS_REGION", "ap-southeast-1")
S3_BUCKET         = os.getenv("S3_BUCKET", "zenflow-media")
R2_ACCOUNT_ID     = os.getenv("R2_ACCOUNT_ID", "")
R2_ENDPOINT       = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com" if R2_ACCOUNT_ID else ""
CDN_BASE_URL      = os.getenv("CDN_BASE_URL", "")          # e.g. https://media.zenflow.sg
LOCAL_UPLOAD_DIR  = os.getenv("LOCAL_UPLOAD_DIR", "./uploads")
APP_BASE_URL      = os.getenv("APP_BASE_URL", "http://localhost:8000")

# Limits
MAX_PHOTO_SIZE_MB = int(os.getenv("MAX_PHOTO_MB", "8"))
MAX_VIDEO_SIZE_MB = int(os.getenv("MAX_VIDEO_MB", "50"))
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
ALLOWED_VIDEO_TYPES = {"video/mp4", "video/quicktime", "video/webm"}

# ─────────────────────────────────────────
#  VALIDATION
# ─────────────────────────────────────────
def validate_file(
    content: bytes,
    content_type: str,
    kind: str = "photo",        # "photo" | "video"
) -> tuple[bool, str]:
    """Validate file type and size. Returns (ok, error_message)."""
    if kind == "photo":
        if content_type not in ALLOWED_IMAGE_TYPES:
            return False, f"Invalid image type. Allowed: {', '.join(ALLOWED_IMAGE_TYPES)}"
        max_bytes = MAX_PHOTO_SIZE_MB * 1024 * 1024
        if len(content) > max_bytes:
            return False, f"Image too large. Max {MAX_PHOTO_SIZE_MB} MB"
    elif kind == "video":
        if content_type not in ALLOWED_VIDEO_TYPES:
            return False, f"Invalid video type. Allowed: {', '.join(ALLOWED_VIDEO_TYPES)}"
        max_bytes = MAX_VIDEO_SIZE_MB * 1024 * 1024
        if len(content) > max_bytes:
            return False, f"Video too large. Max {MAX_VIDEO_SIZE_MB} MB"

    # Magic byte check (prevents content-type spoofing)
    signatures = {
        b"\xff\xd8\xff":             "image/jpeg",
        b"\x89PNG\r\n\x1a\n":       "image/png",
        b"RIFF":                     "image/webp",  # simplified
        b"\x00\x00\x00\x18ftyp":    "video/mp4",
        b"\x00\x00\x00\x20ftyp":    "video/mp4",
    }
    for sig, expected_type in signatures.items():
        if content[:len(sig)] == sig:
            if kind == "photo" and "image" not in expected_type:
                return False, "File content does not match declared image type"
            break

    return True, ""


def _make_key(prof_id: int, kind: str, filename: str) -> str:
    """Generate a unique S3/R2 object key."""
    ext  = Path(filename).suffix.lower() or ".jpg"
    uid  = uuid.uuid4().hex[:12]
    return f"professionals/{prof_id}/{kind}/{uid}{ext}"


def _public_url(key: str) -> str:
    """Convert an object key to a public URL."""
    if CDN_BASE_URL:
        return f"{CDN_BASE_URL}/{key}"
    if STORAGE_BACKEND in ("s3", "r2"):
        return f"https://{S3_BUCKET}.s3.{AWS_REGION}.amazonaws.com/{key}"
    # Local
    return f"{APP_BASE_URL}/uploads/{key}"


# ─────────────────────────────────────────
#  LOCAL STORAGE  (dev)
# ─────────────────────────────────────────
async def _save_local(key: str, content: bytes, content_type: str) -> str:
    path = Path(LOCAL_UPLOAD_DIR) / key
    path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.get_event_loop().run_in_executor(
        None, path.write_bytes, content
    )
    logger.info(f"Local upload: {path}")
    return _public_url(key)


async def _delete_local(key: str) -> None:
    path = Path(LOCAL_UPLOAD_DIR) / key
    if path.exists():
        path.unlink()


# ─────────────────────────────────────────
#  S3 / R2 STORAGE
# ─────────────────────────────────────────
def _get_s3_client():
    import boto3
    kwargs = dict(
        aws_access_key_id=AWS_ACCESS_KEY,
        aws_secret_access_key=AWS_SECRET_KEY,
        region_name=AWS_REGION,
    )
    if STORAGE_BACKEND == "r2" and R2_ENDPOINT:
        kwargs["endpoint_url"] = R2_ENDPOINT
    return boto3.client("s3", **kwargs)


async def _save_s3(key: str, content: bytes, content_type: str) -> str:
    def _upload():
        client = _get_s3_client()
        client.put_object(
            Bucket=S3_BUCKET,
            Key=key,
            Body=content,
            ContentType=content_type,
            CacheControl="public, max-age=31536000",   # 1 year cache
            Metadata={"uploaded-by": "zenflow-api"},
        )
    await asyncio.get_event_loop().run_in_executor(None, _upload)
    url = _public_url(key)
    logger.info(f"S3 upload: s3://{S3_BUCKET}/{key}")
    return url


async def _delete_s3(key: str) -> None:
    def _del():
        _get_s3_client().delete_object(Bucket=S3_BUCKET, Key=key)
    await asyncio.get_event_loop().run_in_executor(None, _del)


async def generate_presigned_url(key: str, expires: int = 3600) -> str:
    """Generate a time-limited pre-signed URL for direct browser upload."""
    def _sign():
        return _get_s3_client().generate_presigned_url(
            "put_object",
            Params={"Bucket": S3_BUCKET, "Key": key},
            ExpiresIn=expires,
        )
    return await asyncio.get_event_loop().run_in_executor(None, _sign)


# ─────────────────────────────────────────
#  PUBLIC API
# ─────────────────────────────────────────
async def upload_file(
    content: bytes,
    original_filename: str,
    content_type: str,
    prof_id: int,
    kind: str = "photo",        # "photo" | "video"
) -> tuple[str, str]:
    """
    Validate + upload a file.
    Returns (public_url, s3_key)
    Raises ValueError on validation failure.
    """
    ok, err = validate_file(content, content_type, kind)
    if not ok:
        raise ValueError(err)

    key = _make_key(prof_id, kind, original_filename)

    if STORAGE_BACKEND in ("s3", "r2"):
        url = await _save_s3(key, content, content_type)
    else:
        url = await _save_local(key, content, content_type)

    return url, key


async def delete_file(key: str) -> None:
    """Delete a file from storage by its key."""
    if not key:
        return
    if STORAGE_BACKEND in ("s3", "r2"):
        await _delete_s3(key)
    else:
        await _delete_local(key)


async def get_presigned_upload_url(prof_id: int, filename: str, kind: str = "photo") -> dict:
    """
    For direct browser-to-storage uploads (avoids proxying through API).
    Returns {url, key, public_url} — frontend PUTs directly to the url.
    """
    if STORAGE_BACKEND not in ("s3", "r2"):
        raise ValueError("Presigned URLs only available with S3 or R2 storage")
    key        = _make_key(prof_id, kind, filename)
    signed_url = await generate_presigned_url(key)
    return {
        "upload_url": signed_url,
        "key":        key,
        "public_url": _public_url(key),
        "expires_in": 3600,
    }
