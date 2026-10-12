from datetime import datetime
from decimal import Decimal
from typing import Dict, Any, List, Optional

async def calculate_monthly_revenue(property_id: str, tenant_id: str, month: int, year: int, db_session=None) -> Decimal:
    """
    Calculates revenue for a specific month.

    A reservation belongs to the month of its check-in date in the property's
    local timezone (properties.timezone), using a half-open interval:
    local start of month <= check_in_date < local start of next month.
    """

    # Wall-clock month boundaries; they are localised to the property's timezone in SQL
    start_date = datetime(year, month, 1)
    if month < 12:
        end_date = datetime(year, month + 1, 1)
    else:
        end_date = datetime(year + 1, 1, 1)

    from sqlalchemy import text

    # timestamp AT TIME ZONE tz converts local wall-clock time to an absolute instant,
    # so DST shifts are handled per property by Postgres
    query = text("""
        SELECT COALESCE(SUM(r.total_amount), 0) as total
        FROM reservations r
        JOIN properties p
            ON p.id = r.property_id AND p.tenant_id = r.tenant_id
        WHERE r.property_id = :property_id
        AND r.tenant_id = :tenant_id
        AND r.check_in_date >= (CAST(:start_date AS timestamp) AT TIME ZONE p.timezone)
        AND r.check_in_date < (CAST(:end_date AS timestamp) AT TIME ZONE p.timezone)
    """)
    params = {
        "property_id": property_id,
        "tenant_id": tenant_id,
        "start_date": start_date,
        "end_date": end_date,
    }

    if db_session is not None:
        result = await db_session.execute(query, params)
        return Decimal(str(result.scalar_one()))

    from app.core.database_pool import DatabasePool

    db_pool = DatabasePool()
    await db_pool.initialize()
    if not db_pool.session_factory:
        raise Exception("Database pool not available")

    async with db_pool.get_session() as session:
        result = await session.execute(query, params)
        return Decimal(str(result.scalar_one()))

async def calculate_total_revenue(property_id: str, tenant_id: str) -> Optional[Dict[str, Any]]:
    """
    Aggregates revenue from database.
    Returns None when the property does not belong to the tenant.
    """
    try:
        # Import database pool
        from app.core.database_pool import DatabasePool
        
        # Initialize pool if needed
        db_pool = DatabasePool()
        await db_pool.initialize()
        
        if db_pool.session_factory:
            async with db_pool.get_session() as session:
                # Use SQLAlchemy text for raw SQL
                from sqlalchemy import text
                
                # Start from properties so a property owned by another tenant yields no row
                query = text("""
                    SELECT
                        p.id AS property_id,
                        COALESCE(SUM(r.total_amount), 0) as total_revenue,
                        COUNT(r.id) as reservation_count
                    FROM properties p
                    LEFT JOIN reservations r
                        ON r.property_id = p.id AND r.tenant_id = p.tenant_id
                    WHERE p.id = :property_id AND p.tenant_id = :tenant_id
                    GROUP BY p.id
                """)
                
                result = await session.execute(query, {
                    "property_id": property_id, 
                    "tenant_id": tenant_id
                })
                row = result.fetchone()
                
                if row:
                    total_revenue = Decimal(str(row.total_revenue))
                    return {
                        "property_id": property_id,
                        "tenant_id": tenant_id,
                        "total": str(total_revenue),
                        "currency": "USD", 
                        "count": row.reservation_count
                    }
                else:
                    # Property not found for this tenant
                    return None
        else:
            raise Exception("Database pool not available")

    except Exception as e:
        # Never fall back to fabricated figures: they ignore the tenant and can leak across clients
        print(f"Database error for {property_id} (tenant: {tenant_id}): {e}")
        raise
