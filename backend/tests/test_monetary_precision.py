"""Regression tests for Issue C: decimal money handling and per-currency totals."""
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.api.v1 import dashboard
from app.config import settings
from app.core.auth import authenticate_request
from app.services import cache, reservations
from app.services.reservations import calculate_monthly_revenue, calculate_total_revenue, round_money


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --- rounding rule ---

def test_total_is_summed_exactly_then_rounded_once():
    # Rounding each reservation first would give 333.33 * 3 = 999.99
    assert round_money(Decimal("333.333") + Decimal("333.333") + Decimal("333.334")) == Decimal("1000.00")


@pytest.mark.parametrize("raw, expected", [
    ("2.675", "2.68"),  # float(2.675) is 2.67499999..., which rounds down
    ("1.005", "1.01"),  # Math.round(1.005 * 100) / 100 == 1 in the old frontend
    ("0.004", "0.00"),
    ("-1.005", "-1.01"),
])
def test_round_half_up_to_cents(raw, expected):
    assert str(round_money(Decimal(raw))) == expected


# --- database ---

@pytest.fixture
async def session():
    engine = create_async_engine(settings.database_url.replace("postgresql://", "postgresql+asyncpg://", 1))
    try:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            db = AsyncSession(bind=conn)
            yield db
            await db.close()
            await transaction.rollback()
    except OSError as e:
        pytest.skip(f"database not reachable: {e}")
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_sub_cent_amounts_total_exactly_from_database():
    # prop-001 / tenant-a: 1250.000 + 333.333 + 333.333 + 333.334
    result = await calculate_total_revenue("prop-001", "tenant-a")
    assert result["total"] == "2250.00"
    assert result["currency"] == "USD"
    assert result["totals"] == [{"currency": "USD", "total": "2250.00", "count": 4}]


@pytest.mark.anyio
async def test_different_currencies_are_not_added_together(session):
    check_in = datetime.fromisoformat("2024-03-10 12:00:00+00")
    await session.execute(
        text("""
            INSERT INTO reservations (id, property_id, tenant_id, check_in_date, check_out_date, total_amount, currency)
            VALUES ('t-eur-1', 'prop-003', 'tenant-a', :check_in, :check_out, :amount, 'EUR')
        """),
        {"check_in": check_in, "check_out": check_in + timedelta(days=2), "amount": Decimal("99.995")},
    )
    march = await calculate_monthly_revenue("prop-003", "tenant-a", 3, 2024, db_session=session)
    # 2850.00 + 3250.50 USD stay separate from the EUR booking
    assert march == {"USD": Decimal("6100.50"), "EUR": Decimal("100.00")}


# --- API serialisation ---

def client_with(monkeypatch, summary):
    async def fake_summary(property_id, tenant_id):
        return summary

    monkeypatch.setattr(dashboard, "get_revenue_summary", fake_summary)
    app = FastAPI()
    app.include_router(dashboard.router, prefix="/api/v1")
    app.dependency_overrides[authenticate_request] = lambda: SimpleNamespace(tenant_id="tenant-a")
    return TestClient(app)


def test_api_returns_money_as_decimal_string(monkeypatch):
    client = client_with(monkeypatch, {
        "property_id": "prop-001", "tenant_id": "tenant-a", "total": "2250.00", "currency": "USD",
        "totals": [{"currency": "USD", "total": "2250.00", "count": 4}], "count": 4,
    })
    body = client.get("/api/v1/dashboard/summary", params={"property_id": "prop-001"}).json()
    assert body["total_revenue"] == "2250.00"
    assert isinstance(body["total_revenue"], str)


def test_api_mixed_currencies_has_no_single_total(monkeypatch):
    totals = [{"currency": "EUR", "total": "100.00", "count": 1},
              {"currency": "USD", "total": "6100.50", "count": 2}]
    client = client_with(monkeypatch, {
        "property_id": "prop-003", "tenant_id": "tenant-a", "total": None, "currency": None,
        "totals": totals, "count": 3,
    })
    body = client.get("/api/v1/dashboard/summary", params={"property_id": "prop-003"}).json()
    assert body["total_revenue"] is None
    assert body["currency"] is None
    assert body["totals_by_currency"] == totals
