# cache_layer.py - Unified cache layer with Redis
"""
Phase 4: Advanced caching with Redis.
Extends the basic cache from database.py with additional features.
"""

import json
import logging
from typing import Optional, Any, List, Dict
from functools import wraps

from database import cache_get, cache_set, cache_delete, cache_delete_pattern
from metrics import record_cache_hit, record_cache_miss

logger = logging.getLogger("zenflow.cache")

# ─────────────────────────────────────────
#  Cache Key Constants
# ─────────────────────────────────────────
class CK:
    """Centralized cache key definitions."""
    PROFESSIONALS    = "professionals:{sort}:{page}:{page_size}:{skill}:{available}:{q}"
    PROFESSIONAL     = "professional:{id}"
    ACTIVE_ADS       = "ads:active"
    SKILLS           = "skills:all"
    PROF_REVIEWS     = "reviews:prof:{id}:{page}"
    AVAILABILITY     = "availability:{prof_id}:{month}"
    BOOKING_STATS    = "booking:stats:{prof_id}"


# ─────────────────────────────────────────
#  Cache Decorators
# ─────────────────────────────────────────
def cached(ttl: int = 300, key_template: Optional[str] = None):
    """
    Decorator for caching function results.
    
    Usage:
        @cached(ttl=60, key_template="user:{user_id}")
        async def get_user(user_id: int):
            return await db.fetchrow("SELECT * FROM users WHERE id = %s", user_id)
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Generate cache key
            if key_template:
                key = key_template
                for i, arg in enumerate(args):
                    key = key.replace(f":{i}", str(arg))
                for k, v in kwargs.items():
                    key = key.replace(f":{k}", str(v))
            else:
                # Default: use function name + args
                key_parts = [func.__name__] + [str(a) for a in args] + [f"{k}={v}" for k, v in sorted(kwargs.items())]
                key = "cache:" + ":".join(key_parts)
            
            # Try cache
            cached_value = await cache_get(key)
            if cached_value is not None:
                record_cache_hit()
                logger.debug(f"Cache hit: {key}")
                return cached_value
            
            record_cache_miss()
            # Call function
            result = await func(*args, **kwargs)
            
            # Cache result
            if result is not None:
                await cache_set(key, result, ttl)
                logger.debug(f"Cache set: {key} (TTL={ttl}s)")
            
            return result
        return wrapper
    return decorator


# ─────────────────────────────────────────
#  Specialized Cache Functions
# ─────────────────────────────────────────
async def cached_professional(prof_id: int, db) -> Optional[Dict]:
    """Get a professional from cache or database."""
    key = CK.PROFESSIONAL.format(id=prof_id)
    
    # Try cache
    cached = await cache_get(key)
    if cached is not None:
        record_cache_hit()
        return cached
    
    record_cache_miss()
    # Fetch from database
    row = await db.fetchrow("SELECT * FROM professionals WHERE id = %s", prof_id)
    if row:
        await cache_set(key, row, 300)  # 5 minutes TTL
    return row


async def cached_professionals_list(
    sort: str, page: int, page_size: int, 
    skill: Optional[str], available: Optional[bool], q: Optional[str],
    db
) -> Optional[List[Dict]]:
    """Get professionals list from cache or database."""
    key = CK.PROFESSIONALS.format(
        sort=sort,
        page=page,
        page_size=page_size,
        skill=skill or "all",
        available=str(available) if available is not None else "all",
        q=q or "none",
    )
    
    cached = await cache_get(key)
    if cached is not None:
        record_cache_hit()
        return cached
    
    record_cache_miss()
    # Fetch from database (actual query would be here)
    rows = await db.fetch("SELECT * FROM professionals LIMIT %s OFFSET %s", page_size, (page - 1) * page_size)
    if rows:
        await cache_set(key, rows, 300)
    return rows


async def cache_professionals_list(data: List[Dict], key: str, ttl: int = 300):
    """Cache a professionals list result."""
    await cache_set(key, data, ttl)


async def cached_active_ads(db) -> Optional[List[Dict]]:
    """Get active ads from cache or database."""
    key = CK.ACTIVE_ADS
    
    cached = await cache_get(key)
    if cached is not None:
        record_cache_hit()
        return cached
    
    record_cache_miss()
    rows = await db.fetch("SELECT * FROM advertisements WHERE status = 'active' AND days_left > 0")
    if rows:
        await cache_set(key, rows, 60)  # 1 minute TTL for ads
    return rows


async def cached_skills(db) -> Optional[List[Dict]]:
    """Get skills from cache or database."""
    key = CK.SKILLS
    
    cached = await cache_get(key)
    if cached is not None:
        record_cache_hit()
        return cached
    
    record_cache_miss()
    rows = await db.fetch("SELECT * FROM skills ORDER BY id")
    if rows:
        await cache_set(key, rows, 3600)  # 1 hour TTL
    return rows


# ─────────────────────────────────────────
#  Cache Invalidation Functions
# ─────────────────────────────────────────
async def invalidate_professional(prof_id: int):
    """Invalidate all cache entries related to a professional."""
    await cache_delete_pattern(f"*professional:{prof_id}*")
    await cache_delete_pattern(f"*availability:*{prof_id}*")
    await cache_delete_pattern(f"*reviews:prof:{prof_id}*")
    await cache_delete_pattern("professionals:*")
    logger.info(f"Invalidated cache for professional {prof_id}")


async def invalidate_ads():
    """Invalidate the active ads cache."""
    await cache_delete(CK.ACTIVE_ADS)
    logger.info("Invalidated active ads cache")


async def invalidate_skills():
    """Invalidate the skills cache."""
    await cache_delete(CK.SKILLS)
    logger.info("Invalidated skills cache")


async def invalidate_reviews(prof_id: int):
    """Invalidate reviews cache for a professional."""
    await cache_delete_pattern(f"reviews:prof:{prof_id}:*")
    logger.info(f"Invalidated reviews cache for professional {prof_id}")


async def invalidate_pattern(pattern: str) -> int:
    """Invalidate all cache keys matching a pattern."""
    if not pattern.startswith("*") and not pattern.endswith("*"):
        pattern = f"*{pattern}*"
    await cache_delete_pattern(pattern)
    logger.info(f"Cache invalidated: {pattern}")
    return 0


async def warm_cache(db):
    """Warm up the cache with common queries."""
    logger.info("Warming cache...")
    
    # Cache skills
    await cached_skills(db)
    
    # Cache active ads
    await cached_active_ads(db)
    
    # Cache first page of professionals
    await cached_professionals_list("featured", 1, 20, None, None, None, db)
    
    logger.info("Cache warming complete")


# ─────────────────────────────────────────
#  Cache Manager
# ─────────────────────────────────────────
class CacheManager:
    """Cache manager with invalidation helpers."""
    
    @staticmethod
    async def invalidate_professional(prof_id: int):
        await invalidate_professional(prof_id)
    
    @staticmethod
    async def invalidate_professional_list():
        await cache_delete_pattern("professionals:*")
        logger.info("Invalidated professional list cache")
    
    @staticmethod
    async def invalidate_ads():
        await invalidate_ads()
    
    @staticmethod
    async def invalidate_skills():
        await invalidate_skills()
    
    @staticmethod
    async def invalidate_reviews(prof_id: int):
        await invalidate_reviews(prof_id)
    
    @staticmethod
    async def warm_cache(db):
        await warm_cache(db)


# ─────────────────────────────────────────
#  Helper Functions
# ─────────────────────────────────────────
def get_cache_key_professionals(sort: str, page: int, page_size: int, 
                                skill: Optional[str], available: Optional[bool], 
                                q: Optional[str]) -> str:
    """Generate cache key for professionals list."""
    return CK.PROFESSIONALS.format(
        sort=sort,
        page=page,
        page_size=page_size,
        skill=skill or "all",
        available=str(available) if available is not None else "all",
        q=q or "none",
    )


def get_cache_key_professional(prof_id: int) -> str:
    """Generate cache key for a single professional."""
    return CK.PROFESSIONAL.format(id=prof_id)


def get_cache_key_reviews(prof_id: int, page: int) -> str:
    """Generate cache key for reviews."""
    return CK.PROF_REVIEWS.format(id=prof_id, page=page)


def get_cache_key_availability(prof_id: int, month: str) -> str:
    """Generate cache key for availability."""
    return CK.AVAILABILITY.format(prof_id=prof_id, month=month)