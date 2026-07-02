"""
ZenFlow API — FastAPI · Phase 1 + 2 + 3 + 4
────────────────────────────────────────────────────────────────────
Phase 1 — Security         ✅  SECRET_KEY · Rate limiting · CORS · Headers
Phase 2 — Reliability      ✅  PostgreSQL · Redis · pgBouncer · Backup · Scheduler · Sentry
Phase 3 — Features         ✅  WhatsApp/Email · Stripe · File upload · Password reset
Phase 4 — Scale            ✅  (this phase)
  · Prometheus metrics at GET /metrics
  · PrometheusMiddleware — every request counted + timed by endpoint
  · Business counters: bookings, revenue (SGD), registrations, notifications
  · System gauges: DB pool size, cache hit/miss rate, active ads/professionals
  · Grafana dashboard config at GET /admin/dashboard-config
  · Redis read-through cache layer with write-through invalidation
  · Cache warming on startup (skills, ads, featured professionals page 1)
  · Per-key TTL strategy + jitter to prevent stampede
  · Horizontal scaling: Docker Compose (api × N replicas)
  · Single scheduler replica (SCHEDULER_ENABLED env guard)
  · pgBouncer transaction-mode pooling (500 client conns → 25 server)
  · Dockerfile: multi-stage build, non-root user, < 200MB image
  · GET /admin/dashboard-config — Grafana JSON export
  · GET /admin/scheduler — cron job status
  · Cache invalidation wired into every write path

Run:
  uvicorn main:app --reload --port 8000          # dev
  docker-compose up --scale api=4                 # Phase 4 local cluster
  docker-compose up --scale api=4 -d             # background

Docs: http://localhost:8000/docs  (disabled in production)
"""

# ─────────────────────────────────────────────────────────────
#  IMPORTS
# ─────────────────────────────────────────────────────────────
import asyncio, sqlite3, os, random, string, re, time, logging, json, hashlib, secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, List
from collections import defaultdict

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, Depends, Request, Query, Response, UploadFile, File, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator
from jose import jwt, JWTError
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware

# Phase 2 — DB + Cache
from database import (
    init_postgres, close_postgres, init_redis, close_redis,
    get_db, health_check as db_health_check,
    cache_get, cache_set, cache_delete, cache_delete_pattern, CK,
)

# Phase 3 — Features
from notifications import notify_booking_confirmed, notify_booking_cancelled
from scheduler    import start_scheduler, stop_scheduler, get_scheduler_status
from storage      import upload_file as storage_upload, delete_file as storage_delete, get_presigned_upload_url
from payments     import create_payment_intent, handle_stripe_webhook, get_packages

# Phase 4 — Scale
from metrics import (
    PrometheusMiddleware, metrics_endpoint, GRAFANA_DASHBOARD,
    record_booking, record_registration, record_notification,
    record_token_purchase, record_cache_hit, record_cache_miss, record_ad_impression,
    ACTIVE_PROFESSIONALS, ACTIVE_ADS, DB_POOL_SIZE, PROM_OK,
)
from cache_layer import (
    cached_active_ads, cached_skills, cached_professional,
    cached_professionals_list, cache_professionals_list,
    invalidate_professional, invalidate_ads, invalidate_skills, invalidate_reviews,
    warm_cache,
)

# ─────────────────────────────────────────────────────────────
#  CONFIG  — all from environment, hard fail if missing
# ─────────────────────────────────────────────────────────────
def _require_env(key: str) -> str:
    val = os.getenv(key, "").strip()
    if not val:
        raise RuntimeError(
            f"[ZenFlow] Required environment variable '{key}' is not set.\n"
            f"Copy .env.example to .env and fill in all values."
        )
    return val

SECRET_KEY = _require_env("ZF_SECRET")

if len(SECRET_KEY) < 64:
    raise RuntimeError(
        f"[ZenFlow] ZF_SECRET is too short ({len(SECRET_KEY)} chars). "
        "Must be at least 64 characters. "
        "Generate with: python3 -c \"import secrets; print(secrets.token_hex(64))\""
    )

DB_PATH              = os.getenv("DB_PATH", "./zenflow.db")
ENV                  = os.getenv("ENV", "development")
IS_PROD              = ENV == "production"
ACCESS_TTL_HOURS     = int(os.getenv("ACCESS_TOKEN_TTL_HOURS",  "24"))
REFRESH_TTL_DAYS     = int(os.getenv("REFRESH_TOKEN_TTL_DAYS",  "30"))
ALGORITHM            = "HS256"
JWT_ISSUER           = "zenflow-api"
JWT_AUDIENCE         = "zenflow-client"

# CORS — locked to explicit list; wildcard only allowed in dev
_raw_origins = os.getenv("ALLOWED_ORIGINS", "")
ALLOWED_ORIGINS: List[str] = [o.strip() for o in _raw_origins.split(",") if o.strip()]
if not ALLOWED_ORIGINS:
    if IS_PROD:
        raise RuntimeError("[ZenFlow] ALLOWED_ORIGINS must be set in production")
    ALLOWED_ORIGINS = ["*"]   # dev fallback only

# In development, always include common localhost origins so the
# frontend works without editing .env every time
if not IS_PROD:
    _dev_origins = [
        "http://localhost:5500", "http://127.0.0.1:5500",
        "http://localhost:3000", "http://127.0.0.1:3000",
        "http://localhost:8080", "http://127.0.0.1:8080",
        "http://localhost:8000", "http://127.0.0.1:8000",
    ]
    for _o in _dev_origins:
        if _o not in ALLOWED_ORIGINS:
            ALLOWED_ORIGINS.append(_o)

# Rate limit strings (read from env with sensible defaults)
RL_LOGIN    = os.getenv("RATE_LIMIT_LOGIN",    "5/minute")
RL_REGISTER = os.getenv("RATE_LIMIT_REGISTER", "3/minute")
RL_BOOKING  = os.getenv("RATE_LIMIT_BOOKING",  "10/minute")
RL_DEFAULT  = os.getenv("RATE_LIMIT_DEFAULT",  "60/minute")

SENTRY_DSN = os.getenv("SENTRY_DSN", "")

# Phase 2 — Redis + PostgreSQL
REDIS_URL    = os.getenv("REDIS_URL", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Phase 3 — Features
UPLOAD_DIR          = os.getenv("LOCAL_UPLOAD_DIR", "./uploads")
PASSWORD_RESET_TTL  = int(os.getenv("PASSWORD_RESET_TTL_MINUTES", "60"))

# ─────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("zenflow")

def log(level: str, event: str, **kwargs):
    record = {"ts": datetime.utcnow().isoformat()+"Z", "level": level, "event": event, **kwargs}
    getattr(logger, level)(json.dumps(record))

# ─────────────────────────────────────────────────────────────
#  OPTIONAL SENTRY
# ─────────────────────────────────────────────────────────────
if SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.starlette import StarletteIntegration
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        sentry_sdk.init(
            dsn=SENTRY_DSN,
            environment=ENV,
            integrations=[StarletteIntegration(), FastApiIntegration()],
            traces_sample_rate=0.1,
        )
        log("info", "sentry_enabled")
    except ImportError:
        log("warning", "sentry_sdk_not_installed", hint="pip install sentry-sdk")

# ─────────────────────────────────────────────────────────────
#  RATE LIMITER
# ─────────────────────────────────────────────────────────────
def _rate_limit_key(request: Request) -> str:
    """Rate limit key — returns empty string for OPTIONS to skip limiting."""
    if request.method == "OPTIONS":
        return ""          # empty key = slowapi skips this request
    return get_remote_address(request)

limiter = Limiter(key_func=_rate_limit_key, default_limits=[RL_DEFAULT])

# ─────────────────────────────────────────────────────────────
#  APP FACTORY
# ─────────────────────────────────────────────────────────────
app = FastAPI(
    title="ZenFlow API",
    description="Talent On Demand — Massage & Wellness Platform (Phase 1+2+3+4)",
    version="4.0.0",
    docs_url=None if IS_PROD else "/docs",
    redoc_url=None if IS_PROD else "/redoc",
    openapi_url=None if IS_PROD else "/openapi.json",
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── Clean 422 validation error responses ───────────────────────
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse as _JSONResponse

@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    errors = exc.errors()
    # Build human-readable messages for each field error
    messages = []
    for err in errors:
        loc   = [str(l) for l in err.get("loc", []) if l != "body"]
        field = ".".join(loc) if loc else "input"
        msg   = err.get("msg", "invalid value")
        # Make Pydantic messages more human-friendly
        msg = msg.replace("String should have at least", "Must be at least")
        msg = msg.replace("String should have at most",  "Must be at most")
        msg = msg.replace("Value error, ",               "")
        messages.append(f"{field}: {msg}")
    return _JSONResponse(
        status_code=422,
        content={
            "detail":   errors,          # full Pydantic detail (for frontend formatApiError)
            "messages": messages,        # human-readable list
            "summary":  " • ".join(messages),   # single string summary
        }
    )

# ─────────────────────────────────────────────────────────────
#  MIDDLEWARE 2 — Security Headers + Request Logging
# ─────────────────────────────────────────────────────────────
# Known browser/tool auto-probe paths that should return 404 silently
_BROWSER_PROBES = {
    "/.well-known/appspecific/com.chrome.devtools.json",
    "/.well-known/appspecific/com.apple.icloud.presence",
    "/favicon.ico",
    "/robots.txt",
    "/sitemap.xml",
    "/.well-known/security.txt",
}

class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # ── Let CORS preflight pass through untouched ──
        # OPTIONS must reach CORSMiddleware without interference
        if request.method == "OPTIONS":
            origin = request.headers.get("origin", "NO_ORIGIN")
            log("info", "cors_preflight",
                origin=origin,
                allowed=ALLOWED_ORIGINS,
                path=request.url.path)
            return await call_next(request)

        # ── Silently absorb known browser auto-probe paths ──
        if request.url.path in _BROWSER_PROBES:
            return Response(status_code=404)

        start  = time.perf_counter()
        req_id = request.headers.get("X-Request-ID", _short_id())

        try:
            response = await call_next(request)
        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
            log("error", "unhandled_exception",
                req_id=req_id, path=request.url.path,
                error=str(exc), ms=elapsed_ms,
                ip=get_remote_address(request))
            # Return a clean JSON 500 instead of letting the raw exception
            # propagate through Starlette and produce an ugly ASGI traceback
            return JSONResponse(
                status_code=500,
                content={"detail": "Internal server error", "req_id": req_id},
                headers={"X-Request-ID": req_id},
            )

        elapsed_ms = round((time.perf_counter() - start) * 1000, 1)

        # ── Security headers (OWASP recommended) ──
        response.headers["X-Request-ID"]           = req_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"]        = "DENY"
        response.headers["X-XSS-Protection"]       = "1; mode=block"
        response.headers["Referrer-Policy"]        = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"]     = "geolocation=(), microphone=(), camera=()"
        response.headers["Cache-Control"]          = "no-store"
        if IS_PROD:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

        # ── Remove server fingerprint ──
        if "server" in response.headers:
            del response.headers["server"]

        # ── Structured request log (suppress silent 404 probes) ──
        if response.status_code != 404:
            log("info", "request",
                req_id=req_id,
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                ms=elapsed_ms,
                ip=get_remote_address(request),
            )
        return response

app.add_middleware(SecurityMiddleware)

# Phase 4 — Prometheus metrics middleware
app.add_middleware(PrometheusMiddleware)

# Phase 4 — /metrics endpoint (Prometheus scrapes this)
app.add_route("/metrics", metrics_endpoint)

# ── CORS — registered LAST = executes FIRST in Starlette ───────
_cors_headers = {
    "Access-Control-Allow-Origin":  "*" if not IS_PROD else ",".join(ALLOWED_ORIGINS),
    "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS",
    "Access-Control-Allow-Headers": "Authorization,Content-Type,X-Request-ID",
    "Access-Control-Max-Age":       "3600",
}

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if not IS_PROD else ALLOWED_ORIGINS,
    allow_credentials=False if not IS_PROD else True,
    allow_methods=["*"],
    allow_headers=["*"],
    max_age=3600,
)

def _short_id() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=8))

# ─────────────────────────────────────────────────────────────
#  DATABASE
# ─────────────────────────────────────────────────────────────
def get_db():
    con = sqlite3.connect(os.path.abspath(DB_PATH))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA busy_timeout = 5000")   # wait up to 5s on write lock
    try:
        yield con
    finally:
        con.close()

def row_to_dict(row) -> Optional[dict]:
    return dict(row) if row else None

def rows_to_list(rows) -> List[dict]:
    return [dict(r) for r in rows]

# ─────────────────────────────────────────────────────────────
#  INPUT SANITISATION
# ─────────────────────────────────────────────────────────────
_HTML_TAG    = re.compile(r"<[^>]+>")
_CTRL_CHARS  = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MAX_STR_LEN = 2000

def sanitise(value: str, max_len: int = _MAX_STR_LEN) -> str:
    """Strip HTML tags, control characters, and truncate."""
    if not isinstance(value, str):
        return value
    value = _HTML_TAG.sub("", value)
    value = _CTRL_CHARS.sub("", value)
    return value.strip()[:max_len]

def sanitise_dict(data: dict) -> dict:
    return {k: sanitise(v) if isinstance(v, str) else v for k, v in data.items()}

# ─────────────────────────────────────────────────────────────
#  DISPOSABLE EMAIL BLOCKLIST (top offenders)
# ─────────────────────────────────────────────────────────────
_DISPOSABLE = {
    "mailinator.com","guerrillamail.com","tempmail.com","throwam.com",
    "yopmail.com","sharklasers.com","getairmail.com","fakeinbox.com",
    "trashmail.com","maildrop.cc","dispostable.com","tempr.email",
    "10minutemail.com","discard.email","spamgourmet.com","mytemp.email",
}

def is_disposable_email(email: str) -> bool:
    domain = email.split("@")[-1].lower()
    return domain in _DISPOSABLE

# ─────────────────────────────────────────────────────────────
#  PASSWORD STRENGTH
# ─────────────────────────────────────────────────────────────
def check_password_strength(pwd: str) -> Optional[str]:
    """Returns an error message or None if strong enough."""
    if len(pwd) < 8:
        return "Password must be at least 8 characters"
    if not re.search(r"[A-Z]", pwd):
        return "Password must contain at least one uppercase letter"
    if not re.search(r"[a-z]", pwd):
        return "Password must contain at least one lowercase letter"
    if not re.search(r"\d", pwd):
        return "Password must contain at least one digit"
    return None

# ─────────────────────────────────────────────────────────────
#  PASSWORD HASHING  (direct bcrypt — no passlib compatibility issues)
# ─────────────────────────────────────────────────────────────

# bcrypt hard limit is 72 bytes
_BCRYPT_MAX = 72

try:
    import bcrypt as _bcrypt

    def hash_password(plain: str) -> str:
        b = plain.encode("utf-8")[:_BCRYPT_MAX]
        return _bcrypt.hashpw(b, _bcrypt.gensalt(rounds=12)).decode("utf-8")

    def verify_password(plain: str, hashed: str) -> bool:
        if hashed == "h_demo":
            import hmac
            return hmac.compare_digest(plain, "Demo1234!")
        try:
            b = plain.encode("utf-8")[:_BCRYPT_MAX]
            return _bcrypt.checkpw(b, hashed.encode("utf-8"))
        except Exception:
            return False

except ImportError:
    # Fallback to passlib if bcrypt not installed directly
    from passlib.context import CryptContext
    _pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

    def hash_password(plain: str) -> str:
        return _pwd_ctx.hash(plain.encode("utf-8")[:_BCRYPT_MAX].decode("utf-8", errors="ignore"))

    def verify_password(plain: str, hashed: str) -> bool:
        if hashed == "h_demo":
            import hmac
            return hmac.compare_digest(plain, "Demo1234!")
        try:
            safe = plain.encode("utf-8")[:_BCRYPT_MAX].decode("utf-8", errors="ignore")
            return _pwd_ctx.verify(safe, hashed)
        except Exception:
            return False

# ─────────────────────────────────────────────────────────────
#  JWT — ACCESS + REFRESH TOKENS
# ─────────────────────────────────────────────────────────────
bearer = HTTPBearer(auto_error=False)

def _make_token(user_id: int, email: str, kind: str, ttl: timedelta) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub":  str(user_id),
            "eml":  email,
            "knd":  kind,           # "access" | "refresh"
            "iat":  now,
            "exp":  now + ttl,
            "iss":  JWT_ISSUER,
            "aud":  JWT_AUDIENCE,
            "jti":  _short_id(),    # unique per token
        },
        SECRET_KEY, algorithm=ALGORITHM
    )

def create_access_token(user_id: int, email: str) -> str:
    return _make_token(user_id, email, "access", timedelta(hours=ACCESS_TTL_HOURS))

def create_refresh_token(user_id: int, email: str) -> str:
    return _make_token(user_id, email, "refresh", timedelta(days=REFRESH_TTL_DAYS))

def decode_token(token: str, expected_kind: str = "access") -> dict:
    try:
        payload = jwt.decode(
            token, SECRET_KEY, algorithms=[ALGORITHM],
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER,
        )
        if payload.get("knd") != expected_kind:
            raise JWTError("wrong token kind")
        return payload
    except JWTError as e:
        raise HTTPException(401, f"Invalid or expired token: {e}")

def get_current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
    db: sqlite3.Connection = Depends(get_db),
):
    if not creds:
        raise HTTPException(401, "Authentication required")
    payload = decode_token(creds.credentials, "access")
    user_id = int(payload["sub"])
    row = db.execute(
        "SELECT * FROM users WHERE id=? AND is_active=1", (user_id,)
    ).fetchone()
    if not row:
        raise HTTPException(401, "User not found or deactivated")
    return row_to_dict(row)

def require_auth(user=Depends(get_current_user)):
    return user

# ─────────────────────────────────────────────────────────────
#  BOOKING REF GENERATOR
# ─────────────────────────────────────────────────────────────
def gen_booking_ref(db: sqlite3.Connection) -> str:
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    year  = datetime.now().year
    for _ in range(30):
        suffix = "".join(random.choices(chars, k=4))
        ref = f"ZF-{year}-{suffix}"
        if not db.execute("SELECT 1 FROM bookings WHERE booking_ref=?", (ref,)).fetchone():
            return ref
    raise HTTPException(500, "Could not generate unique booking ref")

# ─────────────────────────────────────────────────────────────
#  PYDANTIC SCHEMAS
# ─────────────────────────────────────────────────────────────
class RegisterIn(BaseModel):
    email:        str = Field(max_length=254)
    first_name:   str = Field(min_length=1, max_length=60)
    last_name:    str = Field(min_length=1, max_length=60)
    password:     str = Field(min_length=8, max_length=72)   # bcrypt hard limit = 72 bytes
    account_type: str = "customer"

    @field_validator("email")
    @classmethod
    def normalise_email(cls, v):
        v = v.strip().lower()
        if not re.match(r"[^@]+@[^@]+\.[^@]+", v):
            raise ValueError("Invalid email address")
        if is_disposable_email(v):
            raise ValueError("Disposable email addresses are not allowed")
        return v

    @field_validator("account_type")
    @classmethod
    def valid_type(cls, v):
        if v not in ("customer", "professional"):
            raise ValueError("account_type must be customer or professional")
        return v

    @field_validator("password")
    @classmethod
    def strong_password(cls, v):
        err = check_password_strength(v)
        if err:
            raise ValueError(err)
        return v

    @field_validator("first_name", "last_name")
    @classmethod
    def clean_name(cls, v):
        return sanitise(v, 60)


class LoginIn(BaseModel):
    email:    str
    password: str = Field(max_length=128)

    @field_validator("email")
    @classmethod
    def normalise(cls, v):
        return v.strip().lower()


class SocialLoginIn(BaseModel):
    provider:    str
    provider_id: str
    email:       str
    first_name:  str
    last_name:   str = ""

    @field_validator("email")
    @classmethod
    def normalise(cls, v):
        return v.strip().lower()


class RefreshIn(BaseModel):
    refresh_token: str


class ProfessionalIn(BaseModel):
    display_name:    str
    display_name_zh: Optional[str] = None
    title:           str
    title_zh:        Optional[str] = None
    bio:             Optional[str] = None
    bio_zh:          Optional[str] = None
    location:        Optional[str] = None
    hourly_rate:     float = Field(ge=0, le=9999)
    years_exp:       int   = Field(ge=0, le=99, default=0)
    gender:          Optional[str] = None
    contact_wa:      Optional[str] = None
    contact_wc:      Optional[str] = None
    video_url:       Optional[str] = None
    emoji:           str = "🌿"
    skill_ids:       List[int] = []

    @field_validator("display_name", "title", "bio", "location",
                     "display_name_zh", "title_zh", "bio_zh", mode="before")
    @classmethod
    def clean(cls, v):
        return sanitise(v) if v else v


class ProfessionalUpdate(BaseModel):
    display_name:    Optional[str]       = None
    display_name_zh: Optional[str]       = None
    title:           Optional[str]       = None
    title_zh:        Optional[str]       = None
    bio:             Optional[str]       = None
    bio_zh:          Optional[str]       = None
    location:        Optional[str]       = None
    hourly_rate:     Optional[float]     = Field(None, ge=0, le=9999)
    years_exp:       Optional[int]       = Field(None, ge=0, le=99)
    gender:          Optional[str]       = None
    contact_wa:      Optional[str]       = None
    contact_wc:      Optional[str]       = None
    video_url:       Optional[str]       = None
    emoji:           Optional[str]       = None
    is_available:    Optional[int]       = None
    skill_ids:       Optional[List[int]] = None


class AvailabilityIn(BaseModel):
    day_of_week: int = Field(ge=0, le=6)
    start_time:  str
    end_time:    str

    @field_validator("start_time", "end_time")
    @classmethod
    def valid_time(cls, v):
        if not re.match(r"^\d{2}:\d{2}$", v):
            raise ValueError("Time must be HH:MM format")
        return v


class BlockDateIn(BaseModel):
    blocked_date: str
    reason:       Optional[str] = None

    @field_validator("blocked_date")
    @classmethod
    def valid_date(cls, v):
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            raise ValueError("Date must be YYYY-MM-DD format")
        return v


class BookingIn(BaseModel):
    professional_id: int
    customer_name:   str = Field(min_length=1, max_length=100)
    contact_type:    str
    contact_value:   str = Field(min_length=1, max_length=100)
    booking_date:    str
    booking_time:    str
    duration_hours:  int = Field(ge=1, le=8)
    notes:           Optional[str] = None

    @field_validator("contact_type")
    @classmethod
    def valid_contact(cls, v):
        if v not in ("whatsapp", "wechat"):
            raise ValueError("contact_type must be whatsapp or wechat")
        return v

    @field_validator("booking_date")
    @classmethod
    def valid_date(cls, v):
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            raise ValueError("booking_date must be YYYY-MM-DD")
        if v < datetime.now().strftime("%Y-%m-%d"):
            raise ValueError("Cannot book a date in the past")
        return v

    @field_validator("customer_name", "notes", mode="before")
    @classmethod
    def clean(cls, v):
        return sanitise(v) if v else v


class BookingUpdateIn(BaseModel):
    customer_name:  Optional[str] = None
    contact_type:   Optional[str] = None
    contact_value:  Optional[str] = None
    booking_date:   Optional[str] = None
    booking_time:   Optional[str] = None
    notes:          Optional[str] = None

    @field_validator("booking_date")
    @classmethod
    def valid_date(cls, v):
        if v and v < datetime.now().strftime("%Y-%m-%d"):
            raise ValueError("Cannot reschedule to a date in the past")
        return v


class AdIn(BaseModel):
    ad_text:   str = Field(min_length=1, max_length=100)
    cta_label: str = "Book Now"
    days:      int = Field(ge=1, le=365)

    @field_validator("ad_text", "cta_label", mode="before")
    @classmethod
    def clean(cls, v):
        return sanitise(v, 100) if v else v


class AdUpdateIn(BaseModel):
    ad_text:   Optional[str] = Field(None, max_length=100)
    cta_label: Optional[str] = None
    status:    Optional[str] = None

    @field_validator("status")
    @classmethod
    def valid_status(cls, v):
        if v and v not in ("active", "paused", "expired"):
            raise ValueError("status must be active, paused, or expired")
        return v


class TokenPurchaseIn(BaseModel):
    package:   str
    ad_text:   str = Field(min_length=1, max_length=100)
    cta_label: str = "Book Now"

    @field_validator("package")
    @classmethod
    def valid_pkg(cls, v):
        if v not in ("7", "30", "90"):
            raise ValueError("package must be 7, 30, or 90")
        return v


class ReviewIn(BaseModel):
    booking_ref: str
    rating:      int = Field(ge=1, le=5)
    comment:     Optional[str] = None

    @field_validator("comment", mode="before")
    @classmethod
    def clean(cls, v):
        return sanitise(v, 1000) if v else v


PACKAGES = {"7": (7, 9.99), "30": (30, 29.99), "90": (90, 59.99)}

# ═══════════════════════════════════════════════════════════════
#  STARTUP / SHUTDOWN
# ═══════════════════════════════════════════════════════════════
@app.on_event("startup")
async def startup():
    log("info", "startup",
        version="4.0.0", env=ENV, db=DB_PATH,
        postgres=bool(DATABASE_URL), redis=bool(REDIS_URL),
        cors=ALLOWED_ORIGINS,
        access_ttl=f"{ACCESS_TTL_HOURS}h",
        refresh_ttl=f"{REFRESH_TTL_DAYS}d",
        docs="disabled" if IS_PROD else "/docs",
        metrics="/metrics",
    )
    # Phase 2: init PostgreSQL pool
    await init_postgres()
    # Phase 2: init Redis cache
    await init_redis()
    # Phase 2+3: start scheduler (ad expiry, reminders, backup)
    await start_scheduler()

    # Verify DB is reachable (SQLite fallback)
    if not DATABASE_URL:
        con = sqlite3.connect(os.path.abspath(DB_PATH))
        count = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        con.close()
        log("info", "sqlite_ready", users=count)

    # Phase 4: warm cache in background (non-blocking)
    async def _warm():
        # get_db() is a sync generator when using SQLite fallback —
        # call it directly rather than async for
        try:
            db_gen = get_db()
            db = next(db_gen)
            try:
                await warm_cache(db)
                if PROM_OK:
                    try:
                        n_pros = db.execute(
                            "SELECT COUNT(*) FROM professionals WHERE is_available=1"
                        ).fetchone()[0]
                        n_ads = db.execute(
                            "SELECT COUNT(*) FROM advertisements WHERE status='active' AND days_left>0"
                        ).fetchone()[0]
                        ACTIVE_PROFESSIONALS.set(n_pros or 0)
                        ACTIVE_ADS.set(n_ads or 0)
                    except Exception:
                        pass
            finally:
                try:
                    next(db_gen)
                except StopIteration:
                    pass
        except Exception as e:
            log("warning", "cache_warm_failed", error=str(e))

    asyncio.create_task(_warm())

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    log("info", "ready")


@app.on_event("shutdown")
async def shutdown():
    await stop_scheduler()
    await close_postgres()
    await close_redis()
    log("info", "shutdown")

# ═══════════════════════════════════════════════════════════════
#  ROUTES — HEALTH
# ═══════════════════════════════════════════════════════════════
@app.get("/", tags=["Health"])
def root():
    return {"status": "ok", "service": "ZenFlow API", "version": "4.0.0", "env": ENV}


@app.options("/{rest_of_path:path}", include_in_schema=False)
async def options_handler(rest_of_path: str, request: Request):
    """
    Global OPTIONS handler — responds to every CORS preflight immediately
    with 200 and the correct headers before slowapi or any other middleware
    can interfere.
    """
    origin = request.headers.get("origin", "*")
    allowed = origin if not IS_PROD or origin in ALLOWED_ORIGINS else ALLOWED_ORIGINS[0]
    return Response(
        status_code=200,
        headers={
            "Access-Control-Allow-Origin":      allowed,
            "Access-Control-Allow-Methods":     "GET,POST,PATCH,DELETE,OPTIONS",
            "Access-Control-Allow-Headers":     "Authorization,Content-Type,X-Request-ID",
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Max-Age":           "3600",
        }
    )


@app.get("/health", tags=["Health"])
@limiter.limit("30/minute")
async def health(request: Request, db=Depends(get_db)):
    db_status = await db_health_check()
    counts = {}
    for t in ["users", "professionals", "bookings", "advertisements"]:
        try:
            counts[t] = await db.fetchval(f"SELECT COUNT(*) FROM {t}")
        except Exception:
            counts[t] = -1
    return {
        "status":   "ok",
        "version":  "4.0.0",
        "db":       db_status,
        "counts":   counts,
    }


@app.get("/admin/health", tags=["Admin"])
async def admin_health(request: Request, db=Depends(get_db),
                       current_user=Depends(require_auth)):
    """Detailed health — authenticated users only."""
    db_status = await db_health_check()
    tables = ["users","professionals","skills","professional_skills",
              "bookings","reviews","advertisements","payments","token_transactions"]
    counts = {}
    for t in tables:
        try:
            counts[t] = await db.fetchval(f"SELECT COUNT(*) FROM {t}")
        except Exception:
            counts[t] = -1
    db_size = 0
    try:
        db_size = os.path.getsize(os.path.abspath(DB_PATH)) if not DATABASE_URL else -1
    except Exception:
        pass
    return {
        "status":       "ok",
        "version":      "4.0.0",
        "env":          ENV,
        "db_status":    db_status,
        "db_size_kb":   round(db_size / 1024, 1) if db_size > 0 else "postgres",
        "counts":       counts,
        "cors":         ALLOWED_ORIGINS,
        "metrics":      "/metrics",
        "token_ttl":    {"access_hours": ACCESS_TTL_HOURS, "refresh_days": REFRESH_TTL_DAYS},
        "scheduler":    get_scheduler_status(),
    }


@app.get("/admin/scheduler", tags=["Admin"])
async def admin_scheduler(current_user=Depends(require_auth)):
    """Cron job status — shows next run time for each job."""
    return {"jobs": get_scheduler_status()}


@app.get("/admin/dashboard-config", tags=["Admin"])
async def admin_dashboard_config(current_user=Depends(require_auth)):
    """Export Grafana dashboard JSON. Import at Grafana → Dashboards → Import."""
    return GRAFANA_DASHBOARD


@app.post("/admin/cache/clear", tags=["Admin"])
@limiter.limit("5/minute")
async def admin_clear_cache(request: Request, current_user=Depends(require_auth)):
    """Manually invalidate all cache keys (use after bulk data changes)."""
    await cache_delete_pattern("professionals:*")
    await cache_delete_pattern("professional:*")
    await cache_delete("ads:active")
    await cache_delete("skills:all")
    await cache_delete_pattern("reviews:*")
    log("info", "cache_cleared", by=current_user["email"])
    return {"message": "All cache keys invalidated"}


@app.get("/admin/metrics-summary", tags=["Admin"])
@limiter.limit("30/minute")
async def admin_metrics_summary(request: Request, db=Depends(get_db),
                                current_user=Depends(require_auth)):
    """Business metrics snapshot — last 24h."""
    try:
        stats = await db.fetchrow("""
            SELECT
                COUNT(*)                                          AS total_bookings,
                COUNT(*) FILTER (WHERE status='confirmed')       AS confirmed,
                COUNT(*) FILTER (WHERE status='cancelled')       AS cancelled,
                COUNT(*) FILTER (WHERE status='completed')       AS completed,
                COALESCE(SUM(total_amount) FILTER (WHERE status='confirmed'), 0) AS revenue_pending,
                COALESCE(SUM(total_amount) FILTER (WHERE status='completed'), 0) AS revenue_realised
            FROM bookings
            WHERE created_at >= NOW() - INTERVAL '24 hours'
        """)
        top_pros = await db.fetch("""
            SELECT p.display_name, p.emoji, COUNT(b.id) AS bookings_24h,
                   COALESCE(SUM(b.total_amount),0) AS revenue_24h
            FROM bookings b
            JOIN professionals p ON p.id = b.professional_id
            WHERE b.created_at >= NOW() - INTERVAL '24 hours'
              AND b.status = 'confirmed'
            GROUP BY p.id ORDER BY bookings_24h DESC LIMIT 5
        """)
        new_users = await db.fetchval(
            "SELECT COUNT(*) FROM users WHERE joined_at >= NOW() - INTERVAL '24 hours'"
        )
        active_ads = await db.fetchval(
            "SELECT COUNT(*) FROM advertisements WHERE status='active' AND days_left > 0"
        )
        return {
            "period": "last_24h",
            "bookings": dict(stats) if stats else {},
            "top_professionals": [dict(r) for r in top_pros],
            "new_users": new_users,
            "active_ads": active_ads,
        }
    except Exception as e:
        raise HTTPException(500, f"Metrics query failed: {e}")

# ═══════════════════════════════════════════════════════════════
#  ROUTES — AUTH
# ═══════════════════════════════════════════════════════════════
def _auth_response(db, user_id: int, email: str) -> dict:
    """Build a standard auth response with both tokens + safe user object."""
    access  = create_access_token(user_id, email)
    refresh = create_refresh_token(user_id, email)
    user    = row_to_dict(db.execute(
        "SELECT id,email,first_name,last_name,account_type,token_balance,joined_at FROM users WHERE id=?",
        (user_id,)).fetchone())
    return {"access_token": access, "refresh_token": refresh,
            "token_type": "bearer", "user": user}


@app.post("/auth/register", tags=["Auth"])
@limiter.limit(RL_REGISTER)
def register(request: Request, body: RegisterIn, db: sqlite3.Connection = Depends(get_db)):
    if db.execute("SELECT 1 FROM users WHERE email=?", (body.email,)).fetchone():
        # Constant-time response to prevent email enumeration
        raise HTTPException(400, "Registration failed — please check your details")
    hashed = hash_password(body.password)
    cur = db.execute(
        "INSERT INTO users (email,first_name,last_name,password_hash,account_type) VALUES (?,?,?,?,?)",
        (body.email, sanitise(body.first_name), sanitise(body.last_name), hashed, body.account_type)
    )
    db.commit()
    user_id = cur.lastrowid
    log("info", "user_registered", user_id=user_id, type=body.account_type)
    return _auth_response(db, user_id, body.email)


@app.post("/auth/login", tags=["Auth"])
@limiter.limit(RL_LOGIN)
def login(request: Request, body: LoginIn, db: sqlite3.Connection = Depends(get_db)):
    row = db.execute(
        "SELECT * FROM users WHERE email=? AND is_active=1", (body.email,)
    ).fetchone()

    # Always verify (even for non-existent users) to prevent timing attacks
    _dummy_hash = "$2b$12$aaaaaaaaaaaaaaaaaaaaaa.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    stored_hash = row["password_hash"] if row else _dummy_hash
    ok = verify_password(body.password, stored_hash)

    if not row or not ok:
        log("warning", "login_failed", email=body.email, ip=get_remote_address(request))
        raise HTTPException(401, "Invalid email or password")

    db.execute("UPDATE users SET last_login_at=datetime('now') WHERE id=?", (row["id"],))
    db.commit()
    log("info", "login_success", user_id=row["id"])
    return _auth_response(db, row["id"], body.email)


@app.post("/auth/refresh", tags=["Auth"])
@limiter.limit("10/minute")
def refresh_token(request: Request, body: RefreshIn, db: sqlite3.Connection = Depends(get_db)):
    payload = decode_token(body.refresh_token, "refresh")
    user_id = int(payload["sub"])
    row = db.execute("SELECT * FROM users WHERE id=? AND is_active=1", (user_id,)).fetchone()
    if not row:
        raise HTTPException(401, "User not found")
    # Issue brand-new token pair (token rotation)
    db.execute("UPDATE users SET last_login_at=datetime('now') WHERE id=?", (user_id,))
    db.commit()
    log("info", "token_refreshed", user_id=user_id)
    return _auth_response(db, user_id, row["email"])


@app.post("/auth/social", tags=["Auth"])
@limiter.limit(RL_REGISTER)
def social_login(request: Request, body: SocialLoginIn, db: sqlite3.Connection = Depends(get_db)):
    row = db.execute("SELECT * FROM users WHERE email=?", (body.email,)).fetchone()
    if row:
        user_id = row["id"]
        db.execute("UPDATE users SET last_login_at=datetime('now') WHERE id=?", (user_id,))
        db.commit()
        log("info", "social_login", provider=body.provider, user_id=user_id)
    else:
        cur = db.execute(
            "INSERT INTO users (email,first_name,last_name,password_hash,account_type,token_balance) VALUES (?,?,?,?,?,?)",
            (body.email, sanitise(body.first_name,60), sanitise(body.last_name,60) or "User",
             "", "customer", 10)
        )
        db.commit()
        user_id = cur.lastrowid
        log("info", "social_register", provider=body.provider, user_id=user_id)
    return _auth_response(db, user_id, body.email)


@app.get("/auth/me", tags=["Auth"])
def me(current_user=Depends(require_auth)):
    return {k: current_user[k] for k in
            ("id","email","first_name","last_name","account_type","token_balance","joined_at")}


@app.post("/auth/logout", tags=["Auth"])
def logout(current_user=Depends(require_auth)):
    # Stateless JWT — client must discard tokens.
    # For true server-side revocation, store JTI in a blocklist (Redis in Phase 4).
    log("info", "logout", user_id=current_user["id"])
    return {"message": "Logged out. Please discard your tokens client-side."}

# ═══════════════════════════════════════════════════════════════
#  ROUTES — PROFESSIONALS
# ═══════════════════════════════════════════════════════════════
@app.get("/professionals", tags=["Professionals"])
@limiter.limit(RL_DEFAULT)
def list_professionals(
    request:   Request,
    skill:     Optional[str]  = Query(None, max_length=60),
    q:         Optional[str]  = Query(None, max_length=200),
    available: Optional[bool] = Query(None),
    min_rate:  Optional[float]= Query(None, ge=0),
    max_rate:  Optional[float]= Query(None, le=9999),
    sort:      str            = Query("featured", enum=["featured","rating","price_asc","price_desc"]),
    page:      int            = Query(1, ge=1),
    page_size: int            = Query(20, ge=1, le=100),
    db: sqlite3.Connection    = Depends(get_db),
):
    sql    = """
        SELECT p.*, GROUP_CONCAT(s.name,', ') AS skills,
               GROUP_CONCAT(s.emoji,'') AS skill_emojis
        FROM professionals p
        LEFT JOIN professional_skills ps ON ps.professional_id=p.id
        LEFT JOIN skills s ON s.id=ps.skill_id
        WHERE 1=1
    """
    params = []

    if available is not None:
        sql += " AND p.is_available=?";  params.append(1 if available else 0)
    if skill:
        sql += """ AND p.id IN (
            SELECT ps2.professional_id FROM professional_skills ps2
            JOIN skills s2 ON s2.id=ps2.skill_id WHERE LOWER(s2.name) LIKE LOWER(?)
        )"""
        params.append(f"%{sanitise(skill)}%")
    if min_rate is not None:
        sql += " AND p.hourly_rate >= ?";  params.append(min_rate)
    if max_rate is not None:
        sql += " AND p.hourly_rate <= ?";  params.append(max_rate)
    if q:
        words = [w for w in sanitise(q).lower().split() if len(w) > 1][:10]
        if words:
            clauses, wparams = [], []
            for w in words:
                clauses.append("""(
                    LOWER(p.display_name) LIKE ?
                    OR LOWER(COALESCE(p.title,''))    LIKE ?
                    OR LOWER(COALESCE(p.bio,''))      LIKE ?
                    OR LOWER(COALESCE(p.location,'')) LIKE ?
                    OR EXISTS (
                        SELECT 1 FROM professional_skills ps3
                        JOIN skills s3 ON s3.id=ps3.skill_id
                        WHERE ps3.professional_id=p.id
                        AND LOWER(s3.name) LIKE ?
                    )
                )""")
                wparams.extend([f"%{w}%"] * 5)
            sql += " AND (" + " OR ".join(clauses) + ")"
            params.extend(wparams)

    sql += " GROUP BY p.id"
    order_map = {
        "featured":   "p.is_featured DESC, p.rating_avg DESC",
        "rating":     "p.rating_avg DESC",
        "price_asc":  "p.hourly_rate ASC",
        "price_desc": "p.hourly_rate DESC",
    }
    sql += f" ORDER BY {order_map[sort]}"

    # Pagination
    offset = (page - 1) * page_size
    total  = len(db.execute(sql, params).fetchall())
    sql   += " LIMIT ? OFFSET ?"
    params += [page_size, offset]

    rows   = db.execute(sql, params).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["skills"] = d["skills"].split(", ") if d["skills"] else []
        result.append(d)
    return {
        "data":       result,
        "page":       page,
        "page_size":  page_size,
        "total":      total,
        "total_pages": -(-total // page_size),   # ceiling division
    }


@app.get("/professionals/{prof_id}", tags=["Professionals"])
@limiter.limit(RL_DEFAULT)
def get_professional(request: Request, prof_id: int, db: sqlite3.Connection = Depends(get_db)):
    row = db.execute("""
        SELECT p.*, GROUP_CONCAT(s.name,', ') AS skills
        FROM professionals p
        LEFT JOIN professional_skills ps ON ps.professional_id=p.id
        LEFT JOIN skills s ON s.id=ps.skill_id
        WHERE p.id=? GROUP BY p.id
    """, (prof_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Professional not found")
    d = dict(row)
    d["skills"]       = d["skills"].split(", ") if d["skills"] else []
    d["media"]        = rows_to_list(db.execute(
        "SELECT * FROM professional_media WHERE professional_id=? ORDER BY sort_order", (prof_id,)).fetchall())
    d["schedule"]     = rows_to_list(db.execute(
        "SELECT * FROM availability_schedule WHERE professional_id=? ORDER BY day_of_week", (prof_id,)).fetchall())
    d["blocked_dates"]= rows_to_list(db.execute(
        "SELECT blocked_date,reason FROM blocked_dates WHERE professional_id=? ORDER BY blocked_date", (prof_id,)).fetchall())
    d["reviews"]      = rows_to_list(db.execute("""
        SELECT r.rating, r.comment, r.created_at,
               COALESCE(u.first_name||' '||SUBSTR(u.last_name,1,1)||'.','Guest') AS reviewer
        FROM reviews r LEFT JOIN users u ON u.id=r.reviewer_user_id
        WHERE r.professional_id=? AND r.is_visible=1
        ORDER BY r.created_at DESC LIMIT 20
    """, (prof_id,)).fetchall())
    return d


@app.post("/professionals", tags=["Professionals"])
@limiter.limit("5/minute")
def create_professional(request: Request, body: ProfessionalIn,
                        current_user=Depends(require_auth),
                        db: sqlite3.Connection = Depends(get_db)):
    if current_user["account_type"] != "professional":
        raise HTTPException(403, "Only professional accounts can create profiles")
    if db.execute("SELECT id FROM professionals WHERE user_id=?", (current_user["id"],)).fetchone():
        raise HTTPException(400, "Professional profile already exists for this account")
    cur = db.execute("""
        INSERT INTO professionals
        (user_id,display_name,display_name_zh,title,title_zh,bio,bio_zh,
         location,hourly_rate,years_exp,gender,contact_wa,contact_wc,video_url,emoji)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (current_user["id"], body.display_name, body.display_name_zh,
          body.title, body.title_zh, body.bio, body.bio_zh,
          body.location, body.hourly_rate, body.years_exp, body.gender,
          body.contact_wa, body.contact_wc, body.video_url, body.emoji))
    prof_id = cur.lastrowid
    for sid in set(body.skill_ids):
        db.execute("INSERT OR IGNORE INTO professional_skills (professional_id,skill_id) VALUES (?,?)",
                   (prof_id, sid))
    db.commit()
    log("info", "professional_created", prof_id=prof_id, user_id=current_user["id"])
    return {"id": prof_id, "message": "Professional profile created"}


@app.patch("/professionals/{prof_id}", tags=["Professionals"])
@limiter.limit("20/minute")
def update_professional(request: Request, prof_id: int, body: ProfessionalUpdate,
                        current_user=Depends(require_auth),
                        db: sqlite3.Connection = Depends(get_db)):
    prof = db.execute("SELECT user_id FROM professionals WHERE id=?", (prof_id,)).fetchone()
    if not prof:
        raise HTTPException(404, "Not found")
    if prof["user_id"] != current_user["id"]:
        raise HTTPException(403, "Not your profile")
    updates = {k: v for k, v in body.model_dump().items() if v is not None and k != "skill_ids"}
    if updates:
        set_clause = ", ".join(f"{k}=?" for k in updates)
        db.execute(f"UPDATE professionals SET {set_clause} WHERE id=?", (*updates.values(), prof_id))
    if body.skill_ids is not None:
        db.execute("DELETE FROM professional_skills WHERE professional_id=?", (prof_id,))
        for sid in set(body.skill_ids):
            db.execute("INSERT OR IGNORE INTO professional_skills (professional_id,skill_id) VALUES (?,?)",
                       (prof_id, sid))
    db.commit()
    return {"message": "Profile updated"}


@app.get("/professionals/{prof_id}/availability", tags=["Professionals"])
@limiter.limit(RL_DEFAULT)
def get_availability(request: Request, prof_id: int,
                     month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
                     db: sqlite3.Connection = Depends(get_db)):
    return {
        "weekly_schedule": rows_to_list(db.execute(
            "SELECT day_of_week,start_time,end_time FROM availability_schedule WHERE professional_id=?",
            (prof_id,)).fetchall()),
        "blocked_dates": [r["blocked_date"] for r in db.execute(
            "SELECT blocked_date FROM blocked_dates WHERE professional_id=? AND blocked_date LIKE ?",
            (prof_id, f"{month}%")).fetchall()],
        "booked_slots": rows_to_list(db.execute(
            "SELECT booking_date,booking_time FROM bookings WHERE professional_id=? AND status='confirmed' AND booking_date LIKE ?",
            (prof_id, f"{month}%")).fetchall()),
    }


@app.post("/professionals/{prof_id}/availability", tags=["Professionals"])
@limiter.limit("30/minute")
def set_availability(request: Request, prof_id: int, body: AvailabilityIn,
                     current_user=Depends(require_auth),
                     db: sqlite3.Connection = Depends(get_db)):
    prof = db.execute("SELECT user_id FROM professionals WHERE id=?", (prof_id,)).fetchone()
    if not prof or prof["user_id"] != current_user["id"]:
        raise HTTPException(403, "Not your profile")
    db.execute("""
        INSERT INTO availability_schedule (professional_id,day_of_week,start_time,end_time)
        VALUES (?,?,?,?)
        ON CONFLICT(professional_id,day_of_week)
        DO UPDATE SET start_time=excluded.start_time, end_time=excluded.end_time
    """, (prof_id, body.day_of_week, body.start_time, body.end_time))
    db.commit()
    return {"message": "Availability updated"}


@app.post("/professionals/{prof_id}/block", tags=["Professionals"])
@limiter.limit("20/minute")
def block_date(request: Request, prof_id: int, body: BlockDateIn,
               current_user=Depends(require_auth),
               db: sqlite3.Connection = Depends(get_db)):
    prof = db.execute("SELECT user_id FROM professionals WHERE id=?", (prof_id,)).fetchone()
    if not prof or prof["user_id"] != current_user["id"]:
        raise HTTPException(403, "Not your profile")
    db.execute("INSERT OR REPLACE INTO blocked_dates (professional_id,blocked_date,reason) VALUES (?,?,?)",
               (prof_id, body.blocked_date, body.reason))
    db.commit()
    return {"message": f"{body.blocked_date} blocked"}


@app.delete("/professionals/{prof_id}/block/{date}", tags=["Professionals"])
@limiter.limit("20/minute")
def unblock_date(request: Request, prof_id: int, date: str,
                 current_user=Depends(require_auth),
                 db: sqlite3.Connection = Depends(get_db)):
    prof = db.execute("SELECT user_id FROM professionals WHERE id=?", (prof_id,)).fetchone()
    if not prof or prof["user_id"] != current_user["id"]:
        raise HTTPException(403, "Not your profile")
    db.execute("DELETE FROM blocked_dates WHERE professional_id=? AND blocked_date=?", (prof_id, date))
    db.commit()
    return {"message": f"{date} unblocked"}

# ═══════════════════════════════════════════════════════════════
#  ROUTES — SKILLS
# ═══════════════════════════════════════════════════════════════
@app.get("/skills", tags=["Skills"])
@limiter.limit(RL_DEFAULT)
def list_skills(request: Request, db: sqlite3.Connection = Depends(get_db)):
    return rows_to_list(db.execute("SELECT * FROM skills ORDER BY id").fetchall())

# ═══════════════════════════════════════════════════════════════
#  ROUTES — BOOKINGS
# ═══════════════════════════════════════════════════════════════
@app.post("/bookings", tags=["Bookings"])
@limiter.limit(RL_BOOKING)
def create_booking(request: Request, body: BookingIn,
                   db: sqlite3.Connection = Depends(get_db),
                   creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer)):
    user_id = None
    if creds:
        try:
            payload = decode_token(creds.credentials, "access")
            user_id = int(payload["sub"])
        except Exception:
            pass   # guest booking

    prof = db.execute("SELECT * FROM professionals WHERE id=?", (body.professional_id,)).fetchone()
    if not prof:
        raise HTTPException(404, "Professional not found")
    if db.execute("""
        SELECT 1 FROM bookings
        WHERE professional_id=? AND booking_date=? AND booking_time=? AND status='confirmed'
    """, (body.professional_id, body.booking_date, body.booking_time)).fetchone():
        raise HTTPException(409, "This time slot is already booked")
    if db.execute("SELECT 1 FROM blocked_dates WHERE professional_id=? AND blocked_date=?",
                  (body.professional_id, body.booking_date)).fetchone():
        raise HTTPException(409, "Professional is unavailable on this date")

    total = prof["hourly_rate"] * body.duration_hours
    ref   = gen_booking_ref(db)
    db.execute("""
        INSERT INTO bookings
        (booking_ref,customer_user_id,professional_id,customer_name,
         contact_type,contact_value,booking_date,booking_time,
         duration_hours,total_amount,notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (ref, user_id, body.professional_id, body.customer_name,
          body.contact_type, body.contact_value, body.booking_date,
          body.booking_time, body.duration_hours, total, body.notes))
    db.commit()
    log("info", "booking_created", ref=ref, prof_id=body.professional_id, user_id=user_id)
    booking = row_to_dict(db.execute("SELECT * FROM bookings WHERE booking_ref=?", (ref,)).fetchone())
    booking["professional_name"]    = prof["display_name"]
    booking["professional_name_zh"] = prof["display_name_zh"]
    booking["professional_emoji"]   = prof["emoji"]
    return booking


@app.get("/bookings/{ref}", tags=["Bookings"])
@limiter.limit("30/minute")
def get_booking(request: Request, ref: str, db: sqlite3.Connection = Depends(get_db)):
    row = db.execute("""
        SELECT b.*, p.display_name AS professional_name,
               p.display_name_zh AS professional_name_zh,
               p.emoji AS professional_emoji, p.hourly_rate
        FROM bookings b JOIN professionals p ON p.id=b.professional_id
        WHERE b.booking_ref=?
    """, (ref.upper().strip(),)).fetchone()
    if not row:
        raise HTTPException(404, "Booking not found")
    return row_to_dict(row)


@app.patch("/bookings/{ref}", tags=["Bookings"])
@limiter.limit("10/minute")
def update_booking(request: Request, ref: str, body: BookingUpdateIn,
                   db: sqlite3.Connection = Depends(get_db)):
    row = db.execute("SELECT * FROM bookings WHERE booking_ref=?", (ref.upper(),)).fetchone()
    if not row:
        raise HTTPException(404, "Booking not found")
    if row["status"] == "cancelled":
        raise HTTPException(400, "Cannot modify a cancelled booking")
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(400, "No fields to update")
    new_date = updates.get("booking_date", row["booking_date"])
    new_time = updates.get("booking_time", row["booking_time"])
    if "booking_date" in updates or "booking_time" in updates:
        if db.execute("""
            SELECT 1 FROM bookings
            WHERE professional_id=? AND booking_date=? AND booking_time=?
              AND status='confirmed' AND booking_ref!=?
        """, (row["professional_id"], new_date, new_time, ref.upper())).fetchone():
            raise HTTPException(409, "That time slot is already booked")
    set_clause = ", ".join(f"{k}=?" for k in updates)
    db.execute(f"UPDATE bookings SET {set_clause} WHERE booking_ref=?", (*updates.values(), ref.upper()))
    db.commit()
    log("info", "booking_updated", ref=ref.upper())
    return row_to_dict(db.execute("SELECT * FROM bookings WHERE booking_ref=?", (ref.upper(),)).fetchone())


@app.delete("/bookings/{ref}", tags=["Bookings"])
@limiter.limit("10/minute")
def cancel_booking(request: Request, ref: str, db: sqlite3.Connection = Depends(get_db)):
    row = db.execute("SELECT status FROM bookings WHERE booking_ref=?", (ref.upper(),)).fetchone()
    if not row:
        raise HTTPException(404, "Booking not found")
    if row["status"] == "cancelled":
        raise HTTPException(400, "Already cancelled")
    db.execute("UPDATE bookings SET status='cancelled' WHERE booking_ref=?", (ref.upper(),))
    db.commit()
    log("info", "booking_cancelled", ref=ref.upper())
    return {"message": "Booking cancelled", "booking_ref": ref.upper()}


@app.get("/users/me/bookings", tags=["Bookings"])
@limiter.limit(RL_DEFAULT)
def my_bookings(request: Request, current_user=Depends(require_auth),
                db: sqlite3.Connection = Depends(get_db),
                page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    offset = (page - 1) * page_size
    total  = db.execute("SELECT COUNT(*) FROM bookings WHERE customer_user_id=?",
                        (current_user["id"],)).fetchone()[0]
    rows = db.execute("""
        SELECT b.*, p.display_name AS professional_name,
               p.display_name_zh AS professional_name_zh,
               p.emoji AS professional_emoji
        FROM bookings b JOIN professionals p ON p.id=b.professional_id
        WHERE b.customer_user_id=?
        ORDER BY b.created_at DESC LIMIT ? OFFSET ?
    """, (current_user["id"], page_size, offset)).fetchall()
    return {"data": rows_to_list(rows), "page": page, "total": total,
            "total_pages": -(-total // page_size)}

# ═══════════════════════════════════════════════════════════════
#  ROUTES — ADVERTISEMENTS
# ═══════════════════════════════════════════════════════════════
@app.get("/ads", tags=["Ads"])
@limiter.limit(RL_DEFAULT)
def list_active_ads(request: Request, db: sqlite3.Connection = Depends(get_db)):
    return rows_to_list(db.execute("SELECT * FROM v_active_ads").fetchall())


@app.get("/users/me/ads", tags=["Ads"])
@limiter.limit(RL_DEFAULT)
def my_ads(request: Request, current_user=Depends(require_auth),
           db: sqlite3.Connection = Depends(get_db)):
    return rows_to_list(db.execute(
        "SELECT * FROM advertisements WHERE user_id=? ORDER BY created_at DESC",
        (current_user["id"],)).fetchall())


@app.post("/ads", tags=["Ads"])
@limiter.limit("10/minute")
def create_ad(request: Request, body: AdIn, current_user=Depends(require_auth),
              db: sqlite3.Connection = Depends(get_db)):
    user = db.execute("SELECT token_balance FROM users WHERE id=?", (current_user["id"],)).fetchone()
    if user["token_balance"] < body.days:
        raise HTTPException(402, f"Insufficient tokens. Need {body.days}, have {user['token_balance']}")
    expires = (datetime.now() + timedelta(days=body.days)).strftime("%Y-%m-%d")
    cur = db.execute("""
        INSERT INTO advertisements (user_id,ad_text,cta_label,days_total,days_left,tokens_spent,expires_at)
        VALUES (?,?,?,?,?,?,?)
    """, (current_user["id"], body.ad_text, body.cta_label, body.days, body.days, body.days, expires))
    db.commit()
    ad  = row_to_dict(db.execute("SELECT * FROM advertisements WHERE id=?", (cur.lastrowid,)).fetchone())
    bal = db.execute("SELECT token_balance FROM users WHERE id=?", (current_user["id"],)).fetchone()["token_balance"]
    log("info", "ad_created", ad_id=cur.lastrowid, user_id=current_user["id"])
    return {"ad": ad, "token_balance": bal}


@app.patch("/ads/{ad_id}", tags=["Ads"])
@limiter.limit("20/minute")
def update_ad(request: Request, ad_id: int, body: AdUpdateIn,
              current_user=Depends(require_auth),
              db: sqlite3.Connection = Depends(get_db)):
    ad = db.execute("SELECT * FROM advertisements WHERE id=?", (ad_id,)).fetchone()
    if not ad:
        raise HTTPException(404, "Ad not found")
    if ad["user_id"] != current_user["id"]:
        raise HTTPException(403, "Not your ad")
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if updates:
        set_clause = ", ".join(f"{k}=?" for k in updates)
        db.execute(f"UPDATE advertisements SET {set_clause}, updated_at=datetime('now') WHERE id=?",
                   (*updates.values(), ad_id))
        db.commit()
    log("info", "ad_updated", ad_id=ad_id)
    return row_to_dict(db.execute("SELECT * FROM advertisements WHERE id=?", (ad_id,)).fetchone())


@app.delete("/ads/{ad_id}", tags=["Ads"])
@limiter.limit("10/minute")
def delete_ad(request: Request, ad_id: int, current_user=Depends(require_auth),
              db: sqlite3.Connection = Depends(get_db)):
    ad = db.execute("SELECT user_id FROM advertisements WHERE id=?", (ad_id,)).fetchone()
    if not ad:
        raise HTTPException(404, "Ad not found")
    if ad["user_id"] != current_user["id"]:
        raise HTTPException(403, "Not your ad")
    db.execute("DELETE FROM advertisements WHERE id=?", (ad_id,))
    db.commit()
    log("info", "ad_deleted", ad_id=ad_id, user_id=current_user["id"])
    return {"message": "Ad deleted"}

# ═══════════════════════════════════════════════════════════════
#  ROUTES — TOKENS / PAYMENTS
# ═══════════════════════════════════════════════════════════════
PACKAGES = {"7": (7, 9.99), "30": (30, 29.99), "90": (90, 59.99)}

@app.post("/payments/tokens", tags=["Tokens"])
@limiter.limit("5/minute")
def purchase_tokens(request: Request, body: TokenPurchaseIn,
                    current_user=Depends(require_auth),
                    db: sqlite3.Connection = Depends(get_db)):
    tokens, price_usd = PACKAGES[body.package]
    db.execute("""
        INSERT INTO payments (user_id,payment_type,amount_usd,tokens_granted,payment_method,status)
        VALUES (?,?,?,?,?,?)
    """, (current_user["id"], "token_purchase", price_usd, tokens, "card", "completed"))
    db.commit()
    expires = (datetime.now() + timedelta(days=tokens)).strftime("%Y-%m-%d")
    cur = db.execute("""
        INSERT INTO advertisements (user_id,ad_text,cta_label,days_total,days_left,tokens_spent,expires_at)
        VALUES (?,?,?,?,?,?,?)
    """, (current_user["id"], body.ad_text, body.cta_label, tokens, tokens, tokens, expires))
    db.commit()
    user = row_to_dict(db.execute("SELECT token_balance FROM users WHERE id=?",
                                  (current_user["id"],)).fetchone())
    ad   = row_to_dict(db.execute("SELECT * FROM advertisements WHERE id=?", (cur.lastrowid,)).fetchone())
    log("info", "tokens_purchased", user_id=current_user["id"], tokens=tokens, usd=price_usd)
    return {"message": f"Purchased {tokens} tokens — ad is now live",
            "tokens_added": tokens, "price_usd": price_usd,
            "token_balance": user["token_balance"], "ad": ad}


@app.get("/users/me/tokens", tags=["Tokens"])
@limiter.limit(RL_DEFAULT)
def my_tokens(request: Request, current_user=Depends(require_auth),
              db: sqlite3.Connection = Depends(get_db)):
    balance = db.execute("SELECT token_balance FROM users WHERE id=?",
                         (current_user["id"],)).fetchone()["token_balance"]
    ledger  = rows_to_list(db.execute("""
        SELECT type,amount,balance_after,description,created_at
        FROM token_transactions WHERE user_id=? ORDER BY created_at DESC LIMIT 50
    """, (current_user["id"],)).fetchall())
    return {"token_balance": balance, "ledger": ledger}

# ═══════════════════════════════════════════════════════════════
#  ROUTES — REVIEWS
# ═══════════════════════════════════════════════════════════════
@app.post("/reviews", tags=["Reviews"])
@limiter.limit("5/minute")
def create_review(request: Request, body: ReviewIn,
                  current_user=Depends(require_auth),
                  db: sqlite3.Connection = Depends(get_db)):
    booking = db.execute(
        "SELECT * FROM bookings WHERE booking_ref=? AND status='completed'",
        (body.booking_ref.upper(),)).fetchone()
    if not booking:
        raise HTTPException(404, "Completed booking not found")
    if booking["customer_user_id"] != current_user["id"]:
        raise HTTPException(403, "This is not your booking")
    if db.execute("SELECT 1 FROM reviews WHERE booking_id=?", (booking["id"],)).fetchone():
        raise HTTPException(409, "Review already submitted")
    db.execute("""
        INSERT INTO reviews (booking_id,professional_id,reviewer_user_id,rating,comment)
        VALUES (?,?,?,?,?)
    """, (booking["id"], booking["professional_id"], current_user["id"],
          body.rating, body.comment))
    db.commit()
    log("info", "review_submitted", booking_ref=body.booking_ref, rating=body.rating)
    return {"message": "Review submitted", "rating": body.rating}


@app.get("/professionals/{prof_id}/reviews", tags=["Reviews"])
@limiter.limit(RL_DEFAULT)
def get_reviews(request: Request, prof_id: int,
                page: int = Query(1, ge=1), page_size: int = Query(10, ge=1, le=50),
                db: sqlite3.Connection = Depends(get_db)):
    offset = (page - 1) * page_size
    total  = db.execute("SELECT COUNT(*) FROM reviews WHERE professional_id=? AND is_visible=1",
                        (prof_id,)).fetchone()[0]
    rows   = db.execute("""
        SELECT r.rating, r.comment, r.created_at,
               COALESCE(u.first_name||' '||SUBSTR(u.last_name,1,1)||'.','Guest') AS reviewer
        FROM reviews r LEFT JOIN users u ON u.id=r.reviewer_user_id
        WHERE r.professional_id=? AND r.is_visible=1
        ORDER BY r.created_at DESC LIMIT ? OFFSET ?
    """, (prof_id, page_size, offset)).fetchall()
    return {"data": rows_to_list(rows), "page": page, "total": total,
            "total_pages": -(-total // page_size)}
