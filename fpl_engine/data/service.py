from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fpl_engine.data.client import FplApiClient, FplApiError
from fpl_engine.data.normalization import normalize_dataset, normalize_event_live
from fpl_engine.data.repository import Repository
from fpl_engine.data.validation import (
    DataQualityReport,
    DataValidationError,
    ValidationIssue,
    validate_dataset,
)


@dataclass(frozen=True)
class SyncSummary:
    players: int
    teams: int
    events: int
    fixtures: int
    warnings: int
    bootstrap_source: str
    fixtures_source: str
    stale: bool
    player_gameweeks: int = 0
    live_events: int = 0
    team_reimported: bool = False
    team_import_warning: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class DataService:
    def __init__(self, client: FplApiClient, repository: Repository):
        self.client = client
        self.repository = repository

    def refresh(self, *, force: bool = False, reimport_team: bool = True) -> SyncSummary:
        previous_profile = self.repository.profile()
        bootstrap_fetch = self.client.bootstrap(force=force)
        fixtures_fetch = self.client.fixtures(force=force)
        report = validate_dataset(bootstrap_fetch.data, fixtures_fetch.data)
        if not report.valid:
            raise DataValidationError(report)
        extra_issues: list[ValidationIssue] = []
        for result in (bootstrap_fetch, fixtures_fetch):
            if result.stale:
                extra_issues.append(
                    ValidationIssue(
                        "warning",
                        "stale_cache_used",
                        result.warning or f"Stale cache used for {result.endpoint}.",
                        "source",
                        result.endpoint,
                    )
                )
        history_rows: list[dict[str, Any]] = []
        live_event_count = 0
        known_player_ids = {int(row["id"]) for row in bootstrap_fetch.data["elements"]}
        for event in bootstrap_fetch.data["events"]:
            if not event.get("finished"):
                continue
            try:
                live_fetch = self.client.event_live(int(event["id"]), force=force)
                event_rows = [
                    row
                    for row in normalize_event_live(int(event["id"]), live_fetch.data)
                    if row["player_id"] in known_player_ids
                ]
                history_rows.extend(event_rows)
                live_event_count += 1
                if live_fetch.stale:
                    extra_issues.append(
                        ValidationIssue(
                            "warning",
                            "stale_live_history",
                            live_fetch.warning or f"Stale history used for GW{event['id']}.",
                            "event",
                            str(event["id"]),
                        )
                    )
            except (FplApiError, ValueError, KeyError, TypeError, AttributeError) as exc:
                extra_issues.append(
                    ValidationIssue(
                        "warning",
                        "missing_live_history",
                        f"Per-gameweek observations could not be loaded: {exc}",
                        "event",
                        str(event["id"]),
                    )
                )
        if extra_issues:
            report = DataQualityReport(report.checked_at, report.issues + tuple(extra_issues))
        dataset = normalize_dataset(bootstrap_fetch.data, fixtures_fetch.data)
        self.repository.replace_dataset(
            dataset,
            report,
            bootstrap_fetch,
            fixtures_fetch,
            player_gameweeks=history_rows,
        )

        team_reimported = False
        team_import_warning = None
        if reimport_team and previous_profile and previous_profile.get("team_id"):
            try:
                imported = self.import_team(int(previous_profile["team_id"]), automatic=True)
                team_reimported = not imported.get("preserved_local_context", False)
            except (FplApiError, ValueError, KeyError) as exc:
                team_import_warning = f"Core data updated, but squad re-import failed: {exc}"

        return SyncSummary(
            players=len(dataset.players),
            teams=len(dataset.teams),
            events=len(dataset.events),
            fixtures=len(dataset.fixtures),
            warnings=report.warnings,
            bootstrap_source=bootstrap_fetch.source,
            fixtures_source=fixtures_fetch.source,
            stale=bootstrap_fetch.stale or fixtures_fetch.stale,
            player_gameweeks=len(history_rows),
            live_events=live_event_count,
            team_reimported=team_reimported,
            team_import_warning=team_import_warning,
        )

    def import_team(self, team_id: int, *, gameweek: int | None = None, force: bool = False,
                    automatic: bool = False) -> dict[str, Any]:
        if team_id <= 0:
            raise ValueError("Team ID must be a positive integer")
        entry_fetch = self.client.entry(team_id, force=force)
        entry = entry_fetch.data
        if not isinstance(entry, dict) or entry.get("id") is None:
            raise ValueError("Official entry response is incomplete")

        profile = self.repository.profile() or {}
        status = self.repository.status()
        planning_gameweek = (
            int(status["planning_event"]["id"])
            if status.get("planning_event")
            else None
        )
        if gameweek is None:
            gameweek = profile.get("current_gameweek")
            if planning_gameweek is not None:
                gameweek = max(int(gameweek or 1), planning_gameweek)
        if gameweek is None:
            gameweek = planning_gameweek
        gameweek = int(gameweek or 1)
        if not 1 <= gameweek <= 38:
            raise ValueError("Gameweek must be between 1 and 38")

        picks_fetch = None
        last_error: Exception | None = None
        imported_gameweek = gameweek
        for candidate in range(gameweek, 0, -1):
            try:
                result = self.client.picks(team_id, candidate, force=force)
                if isinstance(result.data, dict) and isinstance(result.data.get("picks"), list):
                    picks_fetch = result
                    imported_gameweek = candidate
                    break
            except FplApiError as exc:
                last_error = exc
                if exc.status not in (403, 404):
                    raise
        if picks_fetch is None:
            raise FplApiError(
                f"No public squad was available from GW{gameweek} backwards: {last_error or 'no picks returned'}"
            )

        warnings: list[str] = []
        chip_history: list[dict[str, Any]] = []
        try:
            history_fetch = self.client.entry_history(team_id, force=force)
            if isinstance(history_fetch.data, dict):
                chip_history = [
                    dict(item)
                    for item in history_fetch.data.get("chips", [])
                    if isinstance(item, dict)
                ]
            if history_fetch.stale:
                warnings.append("Chip usage came from stale local cache.")
        except (FplApiError, AttributeError):
            warnings.append(
                "Official chip history was unavailable; chip availability is inferred only from imported picks."
            )
        if imported_gameweek != gameweek:
            warnings.append(
                f"GW{gameweek} picks are not public yet; imported the latest available squad from GW{imported_gameweek}."
            )
        picks = picks_fetch.data
        if any(pick.get("selling_price") is None for pick in picks.get("picks", [])):
            warnings.append(
                "Exact selling prices are not exposed by this public endpoint; current prices are shown separately."
            )
        if entry_fetch.stale or picks_fetch.stale:
            warnings.append("At least one team response came from stale local cache.")
        source_warning = " ".join(warnings) or None
        preserved_local_context = (
            automatic
            and int(profile.get("team_id") or 0) == team_id
            and len(self.repository.squad()) == 15
            and int(profile.get("imported_gameweek") or 0) >= imported_gameweek
        )
        if not preserved_local_context:
            self.repository.save_imported_team(
                entry=entry,
                picks=picks,
                requested_gameweek=gameweek,
                imported_gameweek=imported_gameweek,
                source_warning=source_warning,
                chip_history=chip_history,
            )
        return {
            "team_id": team_id,
            "team_name": entry.get("name"),
            "manager_name": f"{entry.get('player_first_name', '')} {entry.get('player_last_name', '')}".strip(),
            "requested_gameweek": gameweek,
            "imported_gameweek": imported_gameweek,
            "pick_count": len(picks.get("picks", [])),
            "chip_history_count": len(chip_history),
            "warning": source_warning,
            "preserved_local_context": preserved_local_context,
        }
