"""Regression tests for Issue A: tenant isolation of revenue data and its cache."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import dashboard
from app.core.auth import authenticate_request
from app.services import cache, reservations

# Both tenants own a property called prop-001; prop-002 belongs to tenant-a only.
REVENUE = {
    ("tenant-a", "prop-001"): {"total": "2250.000", "count": 4},
    ("tenant-b", "prop-001"): {"total": "0", "count": 0},
    ("tenant-a", "prop-002"): {"total": "4975.500", "count": 4},
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeRedis:
    def __init__(self):
        self.store = {}

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value):
        self.store[key] = value


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(cache, "redis_client", fake)
    return fake


@pytest.fixture
def fake_db(monkeypatch):
    async def calculate_total_revenue(property_id, tenant_id):
        row = REVENUE.get((tenant_id, property_id))
        if row is None:
            return None
        return {"property_id": property_id, "tenant_id": tenant_id,
                "total": row["total"], "currency": "USD", "count": row["count"]}

    monkeypatch.setattr(reservations, "calculate_total_revenue", calculate_total_revenue)


def client_for(tenant_id):
    app = FastAPI()
    app.include_router(dashboard.router, prefix="/api/v1")
    app.dependency_overrides[authenticate_request] = lambda: SimpleNamespace(tenant_id=tenant_id)
    return TestClient(app)


# --- cache layer ---

@pytest.mark.anyio
async def test_cache_key_includes_tenant(fake_redis, fake_db):
    await cache.get_revenue_summary("prop-001", "tenant-a")
    assert list(fake_redis.store) == ["revenue:tenant-a:prop-001"]


@pytest.mark.anyio
async def test_same_property_id_is_cached_separately_per_tenant(fake_redis, fake_db):
    a = await cache.get_revenue_summary("prop-001", "tenant-a")
    b = await cache.get_revenue_summary("prop-001", "tenant-b")
    assert a["total"] == "2250.000" and a["tenant_id"] == "tenant-a"
    assert b["total"] == "0" and b["tenant_id"] == "tenant-b"


@pytest.mark.anyio
async def test_foreign_property_is_not_cached(fake_redis, fake_db):
    assert await cache.get_revenue_summary("prop-002", "tenant-b") is None
    assert fake_redis.store == {}


@pytest.mark.anyio
async def test_missing_tenant_is_rejected(fake_redis, fake_db):
    with pytest.raises(ValueError):
        await cache.get_revenue_summary("prop-001", None)


# --- API endpoint ---

def test_tenant_can_read_own_property(fake_redis, fake_db):
    res = client_for("tenant-a").get("/api/v1/dashboard/summary", params={"property_id": "prop-001"})
    assert res.status_code == 200
    assert res.json()["total_revenue"] == 2250.0
    assert res.json()["reservations_count"] == 4


def test_login_as_a_then_b_does_not_reveal_a_data(fake_redis, fake_db):
    client_for("tenant-a").get("/api/v1/dashboard/summary", params={"property_id": "prop-001"})
    res = client_for("tenant-b").get("/api/v1/dashboard/summary", params={"property_id": "prop-001"})
    assert res.status_code == 200
    assert res.json()["total_revenue"] == 0
    assert res.json()["reservations_count"] == 0


def test_tenant_cannot_read_other_tenants_property(fake_redis, fake_db):
    res = client_for("tenant-b").get("/api/v1/dashboard/summary", params={"property_id": "prop-002"})
    assert res.status_code == 404


def test_user_without_tenant_is_forbidden(fake_redis, fake_db):
    res = client_for(None).get("/api/v1/dashboard/summary", params={"property_id": "prop-001"})
    assert res.status_code == 403


def test_database_failure_returns_503_not_fake_data(fake_redis, monkeypatch):
    async def broken(property_id, tenant_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(reservations, "calculate_total_revenue", broken)
    res = client_for("tenant-a").get("/api/v1/dashboard/summary", params={"property_id": "prop-001"})
    assert res.status_code == 503
