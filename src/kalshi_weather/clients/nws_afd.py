"""NWS Area Forecast Discussion (AFD) client.

The AFD is a multi-paragraph narrative product issued by each NWS Weather
Forecast Office (WFO) every 6 hours. It contains the lead forecaster's
qualitative reasoning about the synoptic state, model spread, confidence,
and expected weather features — signal that no numerical guidance carries.

Each city in our registry has a wfo_office attribute (e.g. "OKX" for NYC).
This module fetches the latest AFD text product for a given WFO via the
public NWS API. No auth required.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .http import http_get_json, http_request_text


NWS_API_BASE = "https://api.weather.gov"


class NwsAfdClientError(RuntimeError):
    """Raised when AFD products cannot be fetched."""


@dataclass(frozen=True, slots=True)
class AfdProduct:
    """A single AFD product as published by an NWS WFO."""

    wfo: str
    issued_at: datetime | None
    product_id: str
    raw_text: str


class NwsAfdClient:
    """Fetches the latest AFD product for an NWS Weather Forecast Office."""

    @classmethod
    def fetch_latest(cls, wfo: str) -> AfdProduct | None:
        """Return the most recent AFD product for the WFO, or None if absent.

        Never raises — operational guard. Errors map to None and the caller
        gracefully degrades (we already operate without AFD signal today).
        """
        try:
            listing = http_get_json(
                f"{NWS_API_BASE}/products/types/AFD/locations/{wfo}"
            )
        except Exception:
            return None
        products = listing.get("@graph") if isinstance(listing, dict) else None
        if not products:
            return None
        latest = products[0]
        product_id = latest.get("id") or latest.get("@id")
        if not product_id:
            return None
        # The id contains an @id URL; use the published id field for the
        # direct product fetch instead.
        product_iri = latest.get("@id") or latest.get("id")
        try:
            # The product_iri ends with the product UUID; fetch directly.
            text_resp = http_get_json(product_iri)
        except Exception:
            return None
        if not isinstance(text_resp, dict):
            return None
        issued_str = text_resp.get("issuanceTime")
        issued_at: datetime | None = None
        if issued_str:
            try:
                issued_at = datetime.fromisoformat(issued_str.replace("Z", "+00:00"))
            except Exception:
                issued_at = None
        return AfdProduct(
            wfo=wfo,
            issued_at=issued_at,
            product_id=str(text_resp.get("id") or product_id),
            raw_text=str(text_resp.get("productText") or ""),
        )
