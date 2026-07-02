"""
ZenFlow — Phase 2: Database & Cache Layer
─────────────────────────────────────────
· asyncpg connection pool (PostgreSQL) with SQLite fallback for dev
· Redis cache for hot queries (professionals list, active ads)
· pgBouncer-compatible (statement pooling mode)
· Automatic cache invalidation on writes
· Backup utility (pg_dump → local file)
"""

import os, json, asyncio, logging, subprocess
from datetime import datetime, timedelta
from typing import Optional, Any
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger("zenflow.db")

# ─────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────
DATABASE_URL  = os.getenv("DATABASE_URL", "")          # postgres://user:pass@host:5432/dbname
SQLITE_PATH   = os.getenv("DB_PATH", "./zenflow.db")   # fallback for local dev
REDIS_URL     = os.getenv("REDIS_URL", "")             # redis://localhost:6379/0
CACHE_TTL     = int(os.getenv("CACHE_TTL_SECONDS", "300"))   # 5 min default
USE_POSTGRES  = bool(DATABASE_URL)
USE_REDIS     = bool(REDIS_URL)

# ─────────────────────────────────────────
#  POSTGRES POOL
# ─────────────────────────────────────────
_pg_pool = None

async def init_postgres():
    """
    Connect to PostgreSQL. On failure, logs a clear diagnostic and falls back
    to SQLite so the server stays up. Does NOT raise — a bad password should
    never take down the whole API.
    """
    global _pg_pool, USE_POSTGRES
    if not USE_POSTGRES:
        return
    try:
        try:
            import asyncpg
        except ImportError:
            logger.warning(
                "\n\n  ⚠️  asyncpg is not installed — falling back to SQLite."
                "\n  To install on Windows: pip install asyncpg==0.29.0"
                "\n  If that fails (Python 3.13): pip install asyncpg --pre\n"
            )
            USE_POSTGRES = False
            return
        _pg_pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=2,
            max_size=20,
            max_inactive_connection_lifetime=300,
            command_timeout=30,
            server_settings={
                "application_name": "zenflow_api",
                "jit": "off",
            }
        )
        async with _pg_pool.acquire() as conn:
            ver = await conn.fetchval("SELECT VERSION()")
            logger.info(f"PostgreSQL connected: {ver[:40]}")

    except Exception as e:
        _pg_pool   = None
        USE_POSTGRES = False   # flip flag so get_db() falls back to SQLite

        # ── Human-readable diagnostics ─────────────────────────
        err = str(e)
        tip = ""
        if "password authentication failed" in err:
            import urllib.parse as _up
            try:
                parsed = _up.urlparse(DATABASE_URL)
                tip = (
                    f"\n\n  ┌─ HOW TO FIX ────────────────────────────────────────┐"
                    f"\n  │  The password in DATABASE_URL is wrong for user        │"
                    f"\n  │  '{parsed.username}' on host '{parsed.hostname}'.       │"
                    f"\n  │                                                         │"
                    f"\n  │  Option A — Use the password you set when you created   │"
                    f"\n  │  the PostgreSQL user. Update .env:                      │"
                    f"\n  │    DATABASE_URL=postgresql://zenflow:CORRECT_PW@...     │"
                    f"\n  │                                                         │"
                    f"\n  │  Option B — Reset the password in psql:                 │"
                    f"\n  │    sudo -u postgres psql                                │"
                    f"\n  │    ALTER USER zenflow WITH PASSWORD 'new_password';     │"
                    f"\n  │    \\q                                                   │"
                    f"\n  │  Then update .env to match.                             │"
                    f"\n  │                                                         │"
                    f"\n  │  Option C — Remove DATABASE_URL from .env to run        │"
                    f"\n  │  on SQLite (dev mode, no PostgreSQL needed).            │"
                    f"\n  └─────────────────────────────────────────────────────────┘"
                )
            except Exception:
                pass
        elif "could not connect" in err or "Connection refused" in err:
            tip = (
                f"\n\n  ┌─ HOW TO FIX ────────────────────────────────────────┐"
                f"\n  │  PostgreSQL is not running or the host/port is wrong.  │"
                f"\n  │                                                         │"
                f"\n  │  Start PostgreSQL (Windows):                            │"
                f"\n  │    net start postgresql-x64-16                          │"
                f"\n  │    (or open Services → postgresql → Start)              │"
                f"\n  │                                                         │"
                f"\n  │  Start PostgreSQL (Linux/Mac):                          │"
                f"\n  │    sudo systemctl start postgresql                      │"
                f"\n  │    brew services start postgresql@16                    │"
                f"\n  │                                                         │"
                f"\n  │  Or remove DATABASE_URL from .env to use SQLite.        │"
                f"\n  └─────────────────────────────────────────────────────────┘"
            )
        elif "does not exist" in err:
            tip = (
                f"\n\n  ┌─ HOW TO FIX ────────────────────────────────────────┐"
                f"\n  │  The database or user does not exist yet. Create it:   │"
                f"\n  │    sudo -u postgres psql                                │"
                f"\n  │    CREATE USER zenflow WITH PASSWORD 'your_password';  │"
                f"\n  │    CREATE DATABASE zenflow OWNER zenflow;              │"
                f"\n  │    \\q                                                   │"
                f"\n  │  Then load the schema:                                  │"
                f"\n  │    psql -U zenflow -d zenflow -f 001_postgres_schema.sql│"
                f"\n  └─────────────────────────────────────────────────────────┘"
            )

        logger.error(
            f"\n\n  ⚠️  PostgreSQL connection failed — falling back to SQLite."
            f"\n  Error: {err}{tip}"
            f"\n\n  Server will start normally using SQLite at: {SQLITE_PATH}\n"
        )

async def close_postgres():
    global _pg_pool
    if _pg_pool:
        await _pg_pool.close()
        logger.info("PostgreSQL pool closed")

@asynccontextmanager
async def get_pg_conn():
    """Acquire a connection from the pool."""
    if not _pg_pool:
        raise RuntimeError("PostgreSQL pool not initialised")
    async with _pg_pool.acquire() as conn:
        yield conn

# ─────────────────────────────────────────
#  SQLITE FALLBACK (dev / testing)
# ─────────────────────────────────────────
def get_sqlite():
    import sqlite3
    con = sqlite3.connect(os.path.abspath(SQLITE_PATH))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA busy_timeout = 5000")
    return con

# ─────────────────────────────────────────
#  UNIFIED DB DEPENDENCY  (FastAPI Depends)
# ─────────────────────────────────────────
class DBConn:
    """
    Unified wrapper — callers use the same API regardless of backend.
    In production: PostgreSQL via asyncpg.
    In dev:        SQLite via sqlite3 (synchronous).
    """
    def __init__(self, conn, is_pg: bool):
        self._conn  = conn
        self._is_pg = is_pg

    async def fetch(self, sql: str, *args) -> list[dict]:
        if self._is_pg:
            rows = await self._conn.fetch(sql, *args)
            return [dict(r) for r in rows]
        else:
            cur = self._conn.execute(sql, args)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    async def fetchrow(self, sql: str, *args) -> Optional[dict]:
        rows = await self.fetch(sql, *args)
        return rows[0] if rows else None

    async def fetchval(self, sql: str, *args) -> Any:
        row = await self.fetchrow(sql, *args)
        if row:
            return list(row.values())[0]
        return None

    async def execute(self, sql: str, *args) -> str:
        if self._is_pg:
            return await self._conn.execute(sql, *args)
        else:
            self._conn.execute(sql, args)
            self._conn.commit()
            return "OK"

    async def executemany(self, sql: str, args_list: list) -> None:
        if self._is_pg:
            await self._conn.executemany(sql, args_list)
        else:
            self._conn.executemany(sql, args_list)
            self._conn.commit()

    async def transaction(self):
        """Context manager for explicit transactions."""
        if self._is_pg:
            return self._conn.transaction()
        else:
            return _SQLiteTransaction(self._conn)

    def commit(self):
        if not self._is_pg:
            self._conn.commit()

    def close(self):
        if not self._is_pg:
            self._conn.close()


class _SQLiteTransaction:
    """Dummy context manager to make SQLite look like asyncpg transactions."""
    def __init__(self, conn):
        self._conn = conn
    async def __aenter__(self):
        self._conn.execute("BEGIN")
        return self
    async def __aexit__(self, exc_type, exc, tb):
        if exc_type:
            self._conn.execute("ROLLBACK")
        else:
            self._conn.commit()


async def get_db():
    """FastAPI dependency — yields a DBConn."""
    if USE_POSTGRES:
        async with get_pg_conn() as pg_conn:
            yield DBConn(pg_conn, is_pg=True)
    else:
        sqlite_conn = get_sqlite()
        try:
            yield DBConn(sqlite_conn, is_pg=False)
        finally:
            sqlite_conn.close()

# ─────────────────────────────────────────
#  REDIS CACHE
# ─────────────────────────────────────────
_redis = None

async def init_redis():
    global _redis
    if not USE_REDIS:
        return
    try:
        import redis.asyncio as aioredis
        _redis = aioredis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
        await _redis.ping()
        logger.info("Redis connected")
    except Exception as e:
        _redis = None
        logger.warning(
            f"\n\n  ⚠️  Redis unavailable — running without cache (this is fine for dev)."
            f"\n  Error: {e}"
            f"\n  To fix: ensure Redis is running and REDIS_URL in .env is correct."
            f"\n  To disable: remove REDIS_URL from .env entirely.\n"
        )

async def close_redis():
    global _redis
    if _redis:
        await _redis.close()

async def cache_get(key: str) -> Optional[Any]:
    """Get from Redis; returns None on miss or error."""
    if not _redis:
        return None
    try:
        raw = await _redis.get(key)
        return json.loads(raw) if raw else None
    except Exception:
        return None

async def cache_set(key: str, value: Any, ttl: int = CACHE_TTL) -> None:
    """Write to Redis; silently fails if unavailable."""
    if not _redis:
        return
    try:
        await _redis.setex(key, ttl, json.dumps(value, default=str))
    except Exception:
        pass

async def cache_delete(*keys: str) -> None:
    """Invalidate one or more cache keys."""
    if not _redis or not keys:
        return
    try:
        await _redis.delete(*keys)
    except Exception:
        pass

async def cache_delete_pattern(pattern: str) -> None:
    """Delete all keys matching a pattern (e.g. 'professionals:*')."""
    if not _redis:
        return
    try:
        keys = await _redis.keys(pattern)
        if keys:
            await _redis.delete(*keys)
    except Exception:
        pass

# Cache key constants
class CK:
    PROFESSIONALS    = "professionals:{sort}:{page}:{page_size}:{skill}:{available}:{q}"
    PROFESSIONAL     = "professional:{id}"
    ACTIVE_ADS       = "ads:active"
    SKILLS           = "skills:all"
    PROF_REVIEWS     = "reviews:prof:{id}:{page}"

# ─────────────────────────────────────────
#  BACKUP UTILITY
# ─────────────────────────────────────────
BACKUP_DIR = os.getenv("BACKUP_DIR", "./backups")

async def run_backup() -> str:
    """
    PostgreSQL: run pg_dump → timestamped .sql.gz file.
    SQLite:     copy .db file.
    Returns path of created backup file.
    """
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts   = datetime.utcnow().strftime("%Y%m%d_%H%M%S")

    if USE_POSTGRES:
        out_path = os.path.join(BACKUP_DIR, f"zenflow_{ts}.sql.gz")
        # Parse DATABASE_URL for pg_dump env vars
        import urllib.parse as urlparse
        parsed = urlparse.urlparse(DATABASE_URL)
        env = {
            **os.environ,
            "PGPASSWORD": parsed.password or "",
        }
        cmd = [
            "pg_dump",
            f"--host={parsed.hostname}",
            f"--port={parsed.port or 5432}",
            f"--username={parsed.username}",
            f"--dbname={parsed.path.lstrip('/')}",
            "--format=custom",      # compressed binary format
            "--no-owner",
            f"--file={out_path}",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"pg_dump failed: {stderr.decode()}")
        logger.info(f"Backup created: {out_path}")
        return out_path

    else:
        import shutil
        out_path = os.path.join(BACKUP_DIR, f"zenflow_{ts}.db")
        shutil.copy2(SQLITE_PATH, out_path)
        logger.info(f"SQLite backup: {out_path}")
        return out_path


async def cleanup_old_backups(keep_days: int = 30) -> int:
    """Delete backups older than keep_days. Returns count deleted."""
    if not os.path.exists(BACKUP_DIR):
        return 0
    cutoff = datetime.utcnow() - timedelta(days=keep_days)
    deleted = 0
    for fname in os.listdir(BACKUP_DIR):
        fpath = os.path.join(BACKUP_DIR, fname)
        if os.path.getmtime(fpath) < cutoff.timestamp():
            os.remove(fpath)
            deleted += 1
    return deleted


# ─────────────────────────────────────────
#  CONNECTION HEALTH CHECK
# ─────────────────────────────────────────
async def health_check() -> dict:
    result = {"postgres": False, "redis": False, "sqlite": False}
    if USE_POSTGRES and _pg_pool:
        try:
            async with get_pg_conn() as conn:
                await conn.fetchval("SELECT 1")
            result["postgres"] = True
        except Exception:
            pass
    if USE_REDIS and _redis:
        try:
            await _redis.ping()
            result["redis"] = True
        except Exception:
            pass
    if not USE_POSTGRES:
        try:
            import sqlite3
            sqlite3.connect(SQLITE_PATH).execute("SELECT 1").fetchone()
            result["sqlite"] = True
        except Exception:
            pass
    return result
