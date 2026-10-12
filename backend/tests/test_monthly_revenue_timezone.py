"""Regression tests for Issue B: reservations are assigned to a month in the property's local timezone.

Runs against the Postgres from docker-compose; every test is rolled back.
"""
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.services.reservations import calculate_monthly_revenue

# prop-001 / tenant-a is Europe/Paris, prop-004 / tenant-b is America/New_York (see database/seed.sql)
EXTRA_RESERVATIONS = [
    # Paris, DST starts 2024-03-31 02:00 local (UTC+1 -> UTC+2)
    ("t-paris-mar-last", "prop-001", "tenant-a", "2024-03-31 21:59:00+00", "10.000"),  # 23:59 CEST, 31 Mar
    ("t-paris-apr-first", "prop-001", "tenant-a", "2024-03-31 22:00:00+00", "20.000"),  # 00:00 CEST, 1 Apr
    # New York, standard time in Feb/Mar (UTC-5)
    ("t-ny-feb-last", "prop-004", "tenant-b", "2024-03-01 04:59:00+00", "30.000"),  # 23:59 EST, 29 Feb
    ("t-ny-mar-first", "prop-004", "tenant-b", "2024-03-01 05:00:00+00", "40.000"),  # 00:00 EST, 1 Mar
    # New York, DST ends 2024-11-03 (UTC-4 -> UTC-5)
    ("t-ny-oct-last", "prop-004", "tenant-b", "2024-11-01 03:59:00+00", "50.000"),  # 23:59 EDT, 31 Oct
    ("t-ny-nov-first", "prop-004", "tenant-b", "2024-11-01 04:00:00+00", "60.000"),  # 00:00 EDT, 1 Nov
    ("t-ny-nov-last", "prop-004", "tenant-b", "2024-12-01 04:59:00+00", "70.000"),  # 23:59 EST, 30 Nov
    ("t-ny-dec-first", "prop-004", "tenant-b", "2024-12-01 05:00:00+00", "80.000"),  # 00:00 EST, 1 Dec
]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def session():
    engine = create_async_engine(settings.database_url.replace("postgresql://", "postgresql+asyncpg://", 1))
    try:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            db = AsyncSession(bind=conn)
            for res_id, prop, tenant, check_in, amount in EXTRA_RESERVATIONS:
                await db.execute(
                    text("""
                        INSERT INTO reservations (id, property_id, tenant_id, check_in_date, check_out_date, total_amount)
                        VALUES (:id, :prop, :tenant, :check_in, :check_out, :amount)
                    """),
                    {"id": res_id, "prop": prop, "tenant": tenant,
                     "check_in": datetime.fromisoformat(check_in),
                     "check_out": datetime.fromisoformat(check_in) + timedelta(days=2),
                     "amount": Decimal(amount)},
                )
            yield db
            await db.close()
            await transaction.rollback()
    except OSError as e:
        pytest.skip(f"database not reachable: {e}")
    finally:
        await engine.dispose()


async def monthly(session, property_id, tenant_id, month, year):
    return await calculate_monthly_revenue(property_id, tenant_id, month, year, db_session=session)


@pytest.mark.anyio
async def test_utc_feb_29_checkin_counts_in_march_for_paris_property(session):
    # res-tz-1 checks in 2024-02-29 23:30 UTC = 2024-03-01 00:30 Paris time
    assert await monthly(session, "prop-001", "tenant-a", 2, 2024) == {}
    march = await monthly(session, "prop-001", "tenant-a", 3, 2024)
    # 1250.000 (res-tz-1) + 333.333 + 333.333 + 333.334 + 10.000 (last minute of March)
    assert march == {"USD": Decimal("2260.00")}


@pytest.mark.anyio
async def test_paris_month_end_across_dst_start(session):
    # 00:00 CEST on 1 April is the start of April, not part of March (half-open interval)
    assert await monthly(session, "prop-001", "tenant-a", 4, 2024) == {"USD": Decimal("20.00")}


@pytest.mark.anyio
async def test_new_york_month_start_uses_local_midnight(session):
    assert await monthly(session, "prop-004", "tenant-b", 2, 2024) == {"USD": Decimal("30.00")}
    # 40.000 (first minute of March) + seeded March reservations 420.00 + 560.75 + 480.25 + 315.50
    assert await monthly(session, "prop-004", "tenant-b", 3, 2024) == {"USD": Decimal("1816.50")}


@pytest.mark.anyio
async def test_new_york_months_across_dst_end(session):
    assert await monthly(session, "prop-004", "tenant-b", 10, 2024) == {"USD": Decimal("50.00")}
    assert await monthly(session, "prop-004", "tenant-b", 11, 2024) == {"USD": Decimal("130.00")}


@pytest.mark.anyio
async def test_december_rolls_over_to_next_year(session):
    assert await monthly(session, "prop-004", "tenant-b", 12, 2024) == {"USD": Decimal("80.00")}


@pytest.mark.anyio
async def test_monthly_revenue_is_scoped_to_tenant(session):
    # tenant-b also owns a prop-001 but has no reservations on it
    assert await monthly(session, "prop-001", "tenant-b", 3, 2024) == {}
