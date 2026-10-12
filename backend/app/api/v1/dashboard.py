from fastapi import APIRouter, Depends, HTTPException
from typing import Dict, Any
from app.services.cache import get_revenue_summary
from app.core.auth import authenticate_request as get_current_user

router = APIRouter()

@router.get("/dashboard/summary")
async def get_dashboard_summary(
    property_id: str,
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    
    # Tenant comes only from the verified token; never fall back to a shared tenant
    tenant_id = getattr(current_user, "tenant_id", None)
    if not tenant_id:
        raise HTTPException(status_code=403, detail="No tenant associated with this user")

    try:
        revenue_data = await get_revenue_summary(property_id, tenant_id)
    except Exception:
        raise HTTPException(status_code=503, detail="Revenue data temporarily unavailable")

    if revenue_data is None:
        # Same response as a non-existent property, so other tenants' IDs are not revealed
        raise HTTPException(status_code=404, detail="Property not found")

    # Money is serialised as a decimal string (already rounded to cents), never a float.
    # total_revenue/currency are null when reservations span several currencies.
    return {
        "property_id": revenue_data['property_id'],
        "total_revenue": revenue_data['total'],
        "currency": revenue_data['currency'],
        "totals_by_currency": revenue_data['totals'],
        "reservations_count": revenue_data['count']
    }
