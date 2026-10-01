from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> int:
    return 1 if bool(value) else 0


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class NormalizedDataset:
    events: list[dict[str, Any]]
    teams: list[dict[str, Any]]
    positions: list[dict[str, Any]]
    players: list[dict[str, Any]]
    fixtures: list[dict[str, Any]]


def normalize_dataset(bootstrap: dict[str, Any], fixtures: list[dict[str, Any]]) -> NormalizedDataset:
    events = [
        {
            "id": row["id"],
            "name": row["name"],
            "deadline_time": row.get("deadline_time"),
            "finished": _bool(row.get("finished")),
            "data_checked": _bool(row.get("data_checked")),
            "is_previous": _bool(row.get("is_previous")),
            "is_current": _bool(row.get("is_current")),
            "is_next": _bool(row.get("is_next")),
            "average_entry_score": _int(row.get("average_entry_score")),
            "highest_score": _int(row.get("highest_score")),
            "raw_json": _json(row),
        }
        for row in bootstrap["events"]
    ]
    teams = [
        {
            "id": row["id"],
            "code": row.get("code"),
            "name": row["name"],
            "short_name": row["short_name"],
            "strength": _int(row.get("strength")),
            "strength_overall_home": _int(row.get("strength_overall_home")),
            "strength_overall_away": _int(row.get("strength_overall_away")),
            "strength_attack_home": _int(row.get("strength_attack_home")),
            "strength_attack_away": _int(row.get("strength_attack_away")),
            "strength_defence_home": _int(row.get("strength_defence_home")),
            "strength_defence_away": _int(row.get("strength_defence_away")),
            "raw_json": _json(row),
        }
        for row in bootstrap["teams"]
    ]
    positions = [
        {
            "id": row["id"],
            "singular_name": row["singular_name"],
            "short_name": row["singular_name_short"],
            "squad_select": _int(row.get("squad_select")),
            "squad_min_play": _int(row.get("squad_min_play")),
            "squad_max_play": _int(row.get("squad_max_play")),
            "raw_json": _json(row),
        }
        for row in bootstrap["element_types"]
    ]
    players = []
    for row in bootstrap["elements"]:
        players.append(
            {
                "id": row["id"],
                "code": row.get("code"),
                "first_name": row.get("first_name", ""),
                "second_name": row.get("second_name", ""),
                "web_name": row["web_name"],
                "team_id": row["team"],
                "position_id": row["element_type"],
                "now_cost": row["now_cost"],
                "status": row.get("status"),
                "news": row.get("news") or "",
                "news_added": row.get("news_added"),
                "chance_this": _int(row.get("chance_of_playing_this_round")),
                "chance_next": _int(row.get("chance_of_playing_next_round")),
                "selected_by_percent": _float(row.get("selected_by_percent")),
                "total_points": _int(row.get("total_points")),
                "event_points": _int(row.get("event_points")),
                "points_per_game": _float(row.get("points_per_game")),
                "form": _float(row.get("form")),
                "minutes": _int(row.get("minutes")),
                "starts": _int(row.get("starts")),
                "goals_scored": _int(row.get("goals_scored")),
                "assists": _int(row.get("assists")),
                "clean_sheets": _int(row.get("clean_sheets")),
                "goals_conceded": _int(row.get("goals_conceded")),
                "own_goals": _int(row.get("own_goals")),
                "penalties_saved": _int(row.get("penalties_saved")),
                "penalties_missed": _int(row.get("penalties_missed")),
                "yellow_cards": _int(row.get("yellow_cards")),
                "red_cards": _int(row.get("red_cards")),
                "saves": _int(row.get("saves")),
                "bonus": _int(row.get("bonus")),
                "bps": _int(row.get("bps")),
                "influence": _float(row.get("influence")),
                "creativity": _float(row.get("creativity")),
                "threat": _float(row.get("threat")),
                "ict_index": _float(row.get("ict_index")),
                "expected_goals": _float(row.get("expected_goals")),
                "expected_assists": _float(row.get("expected_assists")),
                "expected_goal_involvements": _float(row.get("expected_goal_involvements")),
                "expected_goals_conceded": _float(row.get("expected_goals_conceded")),
                "defensive_contribution": _int(row.get("defensive_contribution")),
                "corners_order": _int(row.get("corners_and_indirect_freekicks_order")),
                "direct_freekicks_order": _int(row.get("direct_freekicks_order")),
                "penalties_order": _int(row.get("penalties_order")),
                "raw_json": _json(row),
            }
        )
    normalized_fixtures = [
        {
            "id": row["id"],
            "code": row.get("code"),
            "event_id": _int(row.get("event")),
            "kickoff_time": row.get("kickoff_time"),
            "team_h_id": row["team_h"],
            "team_a_id": row["team_a"],
            "team_h_score": _int(row.get("team_h_score")),
            "team_a_score": _int(row.get("team_a_score")),
            "team_h_difficulty": _int(row.get("team_h_difficulty")),
            "team_a_difficulty": _int(row.get("team_a_difficulty")),
            "started": _bool(row.get("started")),
            "finished": _bool(row.get("finished")),
            "finished_provisional": _bool(row.get("finished_provisional")),
            "minutes": _int(row.get("minutes")),
            "stats_json": _json(row.get("stats", [])),
            "raw_json": _json(row),
        }
        for row in fixtures
    ]
    return NormalizedDataset(events, teams, positions, players, normalized_fixtures)


def normalize_event_live(event_id: int, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize official per-player gameweek observations used by the minutes model."""
    rows: list[dict[str, Any]] = []
    for element in payload.get("elements", []):
        stats = element.get("stats") or {}
        rows.append(
            {
                "player_id": int(element["id"]),
                "event_id": int(event_id),
                "minutes": _int(stats.get("minutes")) or 0,
                "starts": _int(stats.get("starts")) or 0,
                "played": _int(stats.get("played")) or 0,
                "total_points": _int(stats.get("total_points")) or 0,
                "goals_scored": _int(stats.get("goals_scored")) or 0,
                "assists": _int(stats.get("assists")) or 0,
                "clean_sheets": _int(stats.get("clean_sheets")) or 0,
                "goals_conceded": _int(stats.get("goals_conceded")) or 0,
                "saves": _int(stats.get("saves")) or 0,
                "bonus": _int(stats.get("bonus")) or 0,
                "bps": _int(stats.get("bps")) or 0,
                "yellow_cards": _int(stats.get("yellow_cards")) or 0,
                "red_cards": _int(stats.get("red_cards")) or 0,
                "own_goals": _int(stats.get("own_goals")) or 0,
                "penalties_missed": _int(stats.get("penalties_missed")) or 0,
                "expected_goals": _float(stats.get("expected_goals")) or 0.0,
                "expected_assists": _float(stats.get("expected_assists")) or 0.0,
                "expected_goals_conceded": _float(stats.get("expected_goals_conceded")) or 0.0,
                "defensive_contribution": _int(stats.get("defensive_contribution")) or 0,
                "raw_json": _json(element),
            }
        )
    return rows
