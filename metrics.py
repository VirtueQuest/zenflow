# metrics.py - Prometheus metrics for ZenFlow
"""
Phase 4: Prometheus metrics integration.
Provides monitoring endpoints for Grafana/Prometheus.
"""

import time
import logging
from typing import Callable, Optional
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("zenflow.metrics")

# ─────────────────────────────────────────
#  Constants
# ─────────────────────────────────────────
PROM_OK = 200
ACTIVE_PROFESSIONALS = 0   # Will be updated by metrics
ACTIVE_ADS = 0             # Will be updated by metrics
DB_POOL_SIZE = 10          # Default pool size

# ─────────────────────────────────────────
#  Metrics Collector
# ─────────────────────────────────────────
class MetricsCollector:
    """Simple metrics collector without Prometheus client dependency."""
    
    def __init__(self):
        self._requests = 0
        self._errors = 0
        self._latencies = []
        self._endpoint_counts = {}
        self._start_time = time.time()
        
        # Business metrics
        self._bookings_total = 0
        self._registrations_total = 0
        self._notifications_sent = 0
        self._token_purchases = 0
        self._cache_hits = 0
        self._cache_misses = 0
        self._ad_impressions = 0
        
        # Active counts
        self.active_professionals = 0
        self.active_ads = 0
        self.db_pool_size = 10
    
    def record_request(self, method: str, path: str, status_code: int, duration_ms: float):
        """Record a request."""
        self._requests += 1
        
        endpoint = f"{method} {path}"
        self._endpoint_counts[endpoint] = self._endpoint_counts.get(endpoint, 0) + 1
        
        if status_code >= 400:
            self._errors += 1
        
        # Keep last 1000 latencies
        self._latencies.append(duration_ms)
        if len(self._latencies) > 1000:
            self._latencies = self._latencies[-1000:]
    
    def record_booking(self):
        """Record a new booking."""
        self._bookings_total += 1
    
    def record_registration(self):
        """Record a new user registration."""
        self._registrations_total += 1
    
    def record_notification(self):
        """Record a notification sent."""
        self._notifications_sent += 1
    
    def record_token_purchase(self):
        """Record a token purchase."""
        self._token_purchases += 1
    
    def record_cache_hit(self):
        """Record a cache hit."""
        self._cache_hits += 1
    
    def record_cache_miss(self):
        """Record a cache miss."""
        self._cache_misses += 1
    
    def record_ad_impression(self):
        """Record an ad impression."""
        self._ad_impressions += 1
    
    def get_metrics(self) -> dict:
        """Get current metrics."""
        uptime = time.time() - self._start_time
        
        avg_latency = sum(self._latencies) / len(self._latencies) if self._latencies else 0
        max_latency = max(self._latencies) if self._latencies else 0
        min_latency = min(self._latencies) if self._latencies else 0
        
        return {
            "requests_total": self._requests,
            "errors_total": self._errors,
            "error_rate": round(self._errors / max(self._requests, 1) * 100, 2),
            "avg_latency_ms": round(avg_latency, 2),
            "max_latency_ms": round(max_latency, 2),
            "min_latency_ms": round(min_latency, 2),
            "uptime_seconds": int(uptime),
            "endpoint_counts": self._endpoint_counts,
            # Business metrics
            "bookings_total": self._bookings_total,
            "registrations_total": self._registrations_total,
            "notifications_sent": self._notifications_sent,
            "token_purchases": self._token_purchases,
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "cache_hit_rate": round(self._cache_hits / max(self._cache_hits + self._cache_misses, 1) * 100, 2),
            "ad_impressions": self._ad_impressions,
            "active_professionals": self.active_professionals,
            "active_ads": self.active_ads,
            "db_pool_size": self.db_pool_size,
        }


# Global collector
_collector = MetricsCollector()


def update_active_counts(professionals: int, ads: int, pool_size: int = 10):
    """Update active counts from database."""
    _collector.active_professionals = professionals
    _collector.active_ads = ads
    _collector.db_pool_size = pool_size


# ─────────────────────────────────────────
#  Record Functions
# ─────────────────────────────────────────
def record_booking():
    """Record a new booking metric."""
    _collector.record_booking()


def record_registration():
    """Record a new registration metric."""
    _collector.record_registration()


def record_notification():
    """Record a notification sent metric."""
    _collector.record_notification()


def record_token_purchase():
    """Record a token purchase metric."""
    _collector.record_token_purchase()


def record_cache_hit():
    """Record a cache hit metric."""
    _collector.record_cache_hit()


def record_cache_miss():
    """Record a cache miss metric."""
    _collector.record_cache_miss()


def record_ad_impression():
    """Record an ad impression metric."""
    _collector.record_ad_impression()


# Export constants
ACTIVE_PROFESSIONALS = _collector.active_professionals
ACTIVE_ADS = _collector.active_ads
DB_POOL_SIZE = _collector.db_pool_size
PROM_OK = 200


# ─────────────────────────────────────────
#  Middleware
# ─────────────────────────────────────────
class PrometheusMiddleware(BaseHTTPMiddleware):
    """Middleware to record request metrics."""
    
    async def dispatch(self, request: Request, call_next):
        start_time = time.time()
        
        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception as e:
            status_code = 500
            raise
        
        duration_ms = (time.time() - start_time) * 1000
        
        # Record metrics
        _collector.record_request(
            method=request.method,
            path=request.url.path,
            status_code=status_code,
            duration_ms=duration_ms,
        )
        
        return response


# ─────────────────────────────────────────
#  Metrics Endpoint
# ─────────────────────────────────────────
def metrics_endpoint(request: Request):
    """
    Prometheus-compatible metrics endpoint.
    Scraped by Prometheus at GET /metrics.
    """
    metrics = _collector.get_metrics()
    
    # Format as Prometheus-style text
    lines = [
        f'# HELP zenflow_requests_total Total HTTP requests',
        f'# TYPE zenflow_requests_total counter',
        f'zenflow_requests_total {metrics["requests_total"]}',
        '',
        f'# HELP zenflow_errors_total Total HTTP errors',
        f'# TYPE zenflow_errors_total counter',
        f'zenflow_errors_total {metrics["errors_total"]}',
        '',
        f'# HELP zenflow_error_rate Percentage of requests that resulted in errors',
        f'# TYPE zenflow_error_rate gauge',
        f'zenflow_error_rate {metrics["error_rate"]}',
        '',
        f'# HELP zenflow_avg_latency_ms Average request latency in milliseconds',
        f'# TYPE zenflow_avg_latency_ms gauge',
        f'zenflow_avg_latency_ms {metrics["avg_latency_ms"]}',
        '',
        f'# HELP zenflow_max_latency_ms Maximum request latency in milliseconds',
        f'# TYPE zenflow_max_latency_ms gauge',
        f'zenflow_max_latency_ms {metrics["max_latency_ms"]}',
        '',
        f'# HELP zenflow_min_latency_ms Minimum request latency in milliseconds',
        f'# TYPE zenflow_min_latency_ms gauge',
        f'zenflow_min_latency_ms {metrics["min_latency_ms"]}',
        '',
        f'# HELP zenflow_uptime_seconds Uptime in seconds',
        f'# TYPE zenflow_uptime_seconds gauge',
        f'zenflow_uptime_seconds {metrics["uptime_seconds"]}',
        '',
        # Business metrics
        f'# HELP zenflow_bookings_total Total bookings created',
        f'# TYPE zenflow_bookings_total counter',
        f'zenflow_bookings_total {metrics["bookings_total"]}',
        '',
        f'# HELP zenflow_registrations_total Total user registrations',
        f'# TYPE zenflow_registrations_total counter',
        f'zenflow_registrations_total {metrics["registrations_total"]}',
        '',
        f'# HELP zenflow_notifications_sent Total notifications sent',
        f'# TYPE zenflow_notifications_sent counter',
        f'zenflow_notifications_sent {metrics["notifications_sent"]}',
        '',
        f'# HELP zenflow_token_purchases Total token purchases',
        f'# TYPE zenflow_token_purchases counter',
        f'zenflow_token_purchases {metrics["token_purchases"]}',
        '',
        f'# HELP zenflow_cache_hits Total cache hits',
        f'# TYPE zenflow_cache_hits counter',
        f'zenflow_cache_hits {metrics["cache_hits"]}',
        '',
        f'# HELP zenflow_cache_misses Total cache misses',
        f'# TYPE zenflow_cache_misses counter',
        f'zenflow_cache_misses {metrics["cache_misses"]}',
        '',
        f'# HELP zenflow_cache_hit_rate Cache hit rate percentage',
        f'# TYPE zenflow_cache_hit_rate gauge',
        f'zenflow_cache_hit_rate {metrics["cache_hit_rate"]}',
        '',
        f'# HELP zenflow_ad_impressions Total ad impressions',
        f'# TYPE zenflow_ad_impressions counter',
        f'zenflow_ad_impressions {metrics["ad_impressions"]}',
        '',
        f'# HELP zenflow_active_professionals Currently active professionals',
        f'# TYPE zenflow_active_professionals gauge',
        f'zenflow_active_professionals {metrics["active_professionals"]}',
        '',
        f'# HELP zenflow_active_ads Currently active advertisements',
        f'# TYPE zenflow_active_ads gauge',
        f'zenflow_active_ads {metrics["active_ads"]}',
        '',
        f'# HELP zenflow_db_pool_size Database connection pool size',
        f'# TYPE zenflow_db_pool_size gauge',
        f'zenflow_db_pool_size {metrics["db_pool_size"]}',
        '',
    ]
    
    # Add endpoint-specific metrics
    for endpoint, count in metrics["endpoint_counts"].items():
        # Clean up endpoint name for Prometheus label
        label = endpoint.replace(" ", "_").replace("/", "_").replace("?", "_")
        lines.append(f'zenflow_endpoint_requests{{endpoint="{endpoint}"}} {count}')
    
    return Response("\n".join(lines), media_type="text/plain")


# ─────────────────────────────────────────
#  Grafana Dashboard
# ─────────────────────────────────────────
GRAFANA_DASHBOARD = {
    "title": "ZenFlow API Dashboard",
    "uid": "zenflow",
    "panels": [
        {
            "title": "Requests per Second",
            "targets": [{"expr": "rate(zenflow_requests_total[5m])"}]
        },
        {
            "title": "Error Rate",
            "targets": [{"expr": "zenflow_error_rate"}]
        },
        {
            "title": "Average Latency",
            "targets": [{"expr": "zenflow_avg_latency_ms"}]
        },
        {
            "title": "Active Professionals",
            "targets": [{"expr": "zenflow_active_professionals"}]
        },
        {
            "title": "Active Ads",
            "targets": [{"expr": "zenflow_active_ads"}]
        },
        {
            "title": "Cache Hit Rate",
            "targets": [{"expr": "zenflow_cache_hit_rate"}]
        }
    ]
}