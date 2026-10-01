from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable


@dataclass(frozen=True)
class ValidationIssue:
    severity: str
    code: str
    message: str
    entity: str = "dataset"
    entity_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DataQualityReport:
    checked_at: str
    issues: tuple[ValidationIssue, ...]

    @property
    def valid(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    @property
    def errors(self) -> int:
        return sum(issue.severity == "error" for issue in self.issues)

    @property
    def warnings(self) -> int:
        return sum(issue.severity == "warning" for issue in self.issues)

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "checked_at": self.checked_at,
            "errors": self.errors,
            "warnings": self.warnings,
            "issues": [issue.as_dict() for issue in self.issues],
        }


class DataValidationError(RuntimeError):
    def __init__(self, report: DataQualityReport):
        super().__init__(
            f"FPL dataset failed validation with {report.errors} error(s) and "
            f"{report.warnings} warning(s)"
        )
        self.report = report


def _duplicates(values: Iterable[Any]) -> set[Any]:
    seen: set[Any] = set()
    duplicates: set[Any] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


def validate_dataset(bootstrap: Any, fixtures: Any) -> DataQualityReport:
    issues: list[ValidationIssue] = []
    now = datetime.now(timezone.utc).isoformat()
    if not isinstance(bootstrap, dict):
        issues.append(ValidationIssue("error", "bootstrap_type", "Bootstrap payload is not an object."))
        return DataQualityReport(now, tuple(issues))
    if not isinstance(fixtures, list):
        issues.append(ValidationIssue("error", "fixtures_type", "Fixtures payload is not a list."))
        fixtures = []

    required_collections = ("elements", "teams", "events", "element_types")
    for name in required_collections:
        if not isinstance(bootstrap.get(name), list) or not bootstrap.get(name):
            issues.append(
                ValidationIssue("error", "missing_collection", f"Required collection '{name}' is absent or empty.")
            )
    if any(issue.severity == "error" and issue.code == "missing_collection" for issue in issues):
        return DataQualityReport(now, tuple(issues))

    players = bootstrap["elements"]
    teams = bootstrap["teams"]
    events = bootstrap["events"]
    positions = bootstrap["element_types"]
    collections = {
        "player": players,
        "team": teams,
        "event": events,
        "position": positions,
        "fixture": fixtures,
    }
    for entity, rows in collections.items():
        missing_ids = [i for i, row in enumerate(rows) if not isinstance(row, dict) or row.get("id") is None]
        if missing_ids:
            issues.append(
                ValidationIssue("error", "missing_id", f"{len(missing_ids)} {entity} row(s) have no ID.", entity)
            )
        duplicate_ids = _duplicates(row.get("id") for row in rows if isinstance(row, dict) and row.get("id") is not None)
        for duplicate_id in sorted(duplicate_ids, key=str):
            issues.append(
                ValidationIssue("error", "duplicate_id", f"Duplicate {entity} ID {duplicate_id}.", entity, str(duplicate_id))
            )

    team_ids = {row.get("id") for row in teams}
    event_ids = {row.get("id") for row in events}
    position_ids = {row.get("id") for row in positions}
    if len(teams) != 20:
        issues.append(
            ValidationIssue("warning", "unexpected_team_count", f"Expected 20 clubs but received {len(teams)}.", "team")
        )
    if len(events) != 38:
        issues.append(
            ValidationIssue("warning", "unexpected_event_count", f"Expected 38 gameweeks but received {len(events)}.", "event")
        )

    important_stats = ("minutes", "starts", "total_points", "expected_goals", "expected_assists")
    old_news_players: list[str] = []
    invalid_news_players: list[str] = []
    for player in players:
        player_id = str(player.get("id"))
        if player.get("team") not in team_ids:
            issues.append(ValidationIssue("error", "unknown_team", "Player references an unknown club.", "player", player_id))
        if player.get("element_type") not in position_ids:
            issues.append(ValidationIssue("error", "unknown_position", "Player references an unknown position.", "player", player_id))
        price = player.get("now_cost")
        if not isinstance(price, int) or price <= 0 or price > 2000:
            issues.append(ValidationIssue("error", "impossible_price", f"Invalid player price: {price!r}.", "player", player_id))
        if not player.get("web_name"):
            issues.append(ValidationIssue("error", "missing_name", "Player has no display name.", "player", player_id))
        missing = [field for field in important_stats if player.get(field) in (None, "")]
        if missing:
            issues.append(
                ValidationIssue("warning", "missing_statistics", f"Missing fields: {', '.join(missing)}.", "player", player_id)
            )
        if player.get("status") not in (None, "a") and player.get("news") and player.get("news_added"):
            try:
                news_at = datetime.fromisoformat(str(player["news_added"]).replace("Z", "+00:00"))
                if datetime.now(timezone.utc) - news_at > timedelta(days=14):
                    old_news_players.append(player_id)
            except ValueError:
                invalid_news_players.append(player_id)

    if old_news_players:
        sample = ", ".join(old_news_players[:8])
        suffix = "…" if len(old_news_players) > 8 else ""
        issues.append(
            ValidationIssue(
                "warning",
                "old_availability_update",
                f"{len(old_news_players)} unavailable player(s) have news over 14 days old; verify before a minutes model. Player IDs: {sample}{suffix}",
                "player",
            )
        )
    if invalid_news_players:
        issues.append(
            ValidationIssue(
                "warning",
                "invalid_news_timestamp",
                f"{len(invalid_news_players)} availability-news timestamp(s) are invalid.",
                "player",
            )
        )

    for fixture in fixtures:
        fixture_id = str(fixture.get("id"))
        for field in ("team_h", "team_a"):
            if fixture.get(field) not in team_ids:
                issues.append(ValidationIssue("error", "unknown_fixture_team", f"Fixture {field} is unknown.", "fixture", fixture_id))
        event = fixture.get("event")
        if event is not None and event not in event_ids:
            issues.append(ValidationIssue("error", "unknown_fixture_event", f"Fixture gameweek {event} is unknown.", "fixture", fixture_id))
        if fixture.get("team_h") == fixture.get("team_a"):
            issues.append(ValidationIssue("error", "same_team_fixture", "Fixture has the same home and away club.", "fixture", fixture_id))

    current_events = [event for event in events if event.get("is_current")]
    if len(current_events) > 1:
        issues.append(ValidationIssue("error", "multiple_current_events", "More than one gameweek is marked current.", "event"))
    if not current_events and not all(event.get("finished") for event in events):
        issues.append(ValidationIssue("warning", "no_current_event", "No gameweek is marked current.", "event"))

    unavailable_fields = {
        "shots": "Shots are not supplied by the bootstrap endpoint.",
        "big_chances": "Big chances are not supplied by the bootstrap endpoint.",
        "key_passes": "Key passes are not supplied by the bootstrap endpoint.",
        "touches_in_box": "Touches in box are not supplied by the bootstrap endpoint.",
        "external_workload": "European, cup, and international workload is not supplied by official FPL endpoints.",
        "tactical_roles": "Detailed tactical roles and manager-specific rotation context require a separate reliable provider.",
    }
    for code, message in unavailable_fields.items():
        issues.append(ValidationIssue("info", f"provider_required_{code}", message))

    return DataQualityReport(now, tuple(issues))
