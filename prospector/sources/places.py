"""Google Places API (New) text search."""
import httpx

from .. import db
from ..config import env

URL = "https://places.googleapis.com/v1/places:searchText"
FIELDS = ",".join(
    f"places.{f}"
    for f in [
        "id", "displayName", "formattedAddress", "addressComponents", "nationalPhoneNumber",
        "internationalPhoneNumber", "websiteUri", "googleMapsUri", "businessStatus",
        "primaryTypeDisplayName", "rating", "userRatingCount",
    ]
) + ",nextPageToken"


def _component(place: dict, kind: str) -> str | None:
    for c in place.get("addressComponents", []):
        if kind in c.get("types", []):
            return c.get("longText")
    return None


def search(query: str, max_pages: int = 3) -> list[dict]:
    """All results for one query (up to 60), cached for 30 days."""
    key = f"places {query}"
    cached = db.cache_get(key, 30)
    if cached is not None:
        return cached
    results, token = [], None
    for _ in range(max_pages):
        body = {"textQuery": query, "languageCode": "hr", "regionCode": "HR", "pageSize": 20}
        if token:
            body["pageToken"] = token
        r = httpx.post(
            URL, json=body, timeout=30,
            headers={"X-Goog-Api-Key": env("GOOGLE_PLACES_API_KEY"), "X-Goog-FieldMask": FIELDS},
        )
        r.raise_for_status()
        data = r.json()
        for p in data.get("places", []):
            if p.get("businessStatus") == "CLOSED_PERMANENTLY":
                continue
            results.append({
                "source": "google_places",
                "source_id": p["id"],
                "name": p.get("displayName", {}).get("text", ""),
                "address": p.get("formattedAddress"),
                "town": _component(p, "locality") or _component(p, "postal_town"),
                "county": _component(p, "administrative_area_level_1"),
                "phones": [x for x in [p.get("nationalPhoneNumber")] if x],
                "website": p.get("websiteUri"),
                "maps_url": p.get("googleMapsUri"),
                "google_category": (p.get("primaryTypeDisplayName") or {}).get("text"),
                "rating": p.get("rating"),
                "reviews": p.get("userRatingCount"),
            })
        token = data.get("nextPageToken")
        if not token:
            break
    db.cache_put(key, results)
    return results
