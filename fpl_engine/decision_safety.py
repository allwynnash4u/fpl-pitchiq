"""Shared fail-closed checks before presenting FPL forecasts as decisions."""

import hashlib
import json
from datetime import datetime, timezone
from typing import Any


MAX_OFFICIAL_DATA_AGE_HOURS = 12
MAX_MANAGER_CONFIRMATION_AGE_HOURS = 24


def manager_fingerprint(profile: dict[str, Any], squad: list[dict[str, Any]]) -> str:
    """Bind a confirmation to the exact roster, prices and transfer context."""
    payload = {
        "team_id": profile.get("team_id"),
        "bank": profile.get("bank"),
        "free_transfers": profile.get("free_transfers"),
        "squad": sorted(
            (
                int(player["id"]),
                int(player.get("squad_position") or 0),
                player.get("selling_price"),
                round(float(player.get("current_price") or 0) * 10),
            )
            for player in squad
        ),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def manager_context_warnings(
    profile: dict[str, Any], squad: list[dict[str, Any]], planning_event: int | None,
    *, now: datetime | None = None,
) -> list[str]:
    warnings: list[str] = []
    if profile.get("bank") is None:
        warnings.append("Bank balance has not been confirmed")
    if profile.get("free_transfers") is None:
        warnings.append("Available free transfers have not been confirmed")
    warnings.extend(selling_price_warnings(squad))
    confirmation = profile.get("manager_confirmation") or {}
    if not isinstance(confirmation, dict) or not confirmation.get("confirmed_at"):
        warnings.append("Confirm your current squad, bank, free transfers and selling prices in My team")
        return warnings
    try:
        stamp = datetime.fromisoformat(str(confirmation["confirmed_at"]).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("missing timezone")
        age = ((now or datetime.now(timezone.utc)) - stamp.astimezone(timezone.utc)).total_seconds() / 3600
        if age < -0.1 or age > MAX_MANAGER_CONFIRMATION_AGE_HOURS:
            warnings.append("Manager context confirmation has expired; review it in My team")
    except (ValueError, TypeError):
        warnings.append("Manager context confirmation timestamp is invalid")
    if planning_event is None or confirmation.get("planning_event") != planning_event:
        warnings.append("Manager context must be confirmed for the current planning gameweek")
    if confirmation.get("fingerprint") != manager_fingerprint(profile, squad):
        warnings.append("Squad, prices or transfer context changed since confirmation")
    return warnings


def data_warnings(status: dict[str, Any], *, now: datetime | None = None) -> list[str]:
    sync = status.get("last_sync") or {}
    warnings: list[str] = []
    if not status.get("ready", False) or int((status.get("quality") or {}).get("errors") or 0):
        warnings.append("Official FPL data is incomplete or has validation errors")
    if sync.get("used_stale_data"):
        warnings.append("The latest refresh used stale fallback data")
    timestamp = sync.get("completed_at")
    if not timestamp:
        warnings.append("Official FPL data freshness cannot be verified")
    else:
        try:
            updated = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
            if updated.tzinfo is None:
                raise ValueError("missing timezone")
            age_hours = ((now or datetime.now(timezone.utc)) - updated.astimezone(timezone.utc)).total_seconds() / 3600
            if age_hours < -0.1:
                warnings.append("Official FPL data has a future-dated refresh timestamp")
            elif age_hours > MAX_OFFICIAL_DATA_AGE_HOURS:
                warnings.append(f"Official FPL data is over {MAX_OFFICIAL_DATA_AGE_HOURS} hours old")
        except (TypeError, ValueError):
            warnings.append("Official FPL data timestamp is invalid")
    return warnings


def selling_price_warnings(squad: list[dict[str, Any]]) -> list[str]:
    if any(player.get("selling_price") is None for player in squad):
        return ["Exact FPL selling prices are unavailable; budget-dependent moves need confirmation in the official app"]
    return []
