from __future__ import annotations

from dataclasses import asdict, dataclass
from math import exp
from statistics import fmean
from typing import Any

from fpl_engine.models.minutes import clamp


@dataclass(frozen=True)
class FixtureEnvironment:
    fixture_id: int
    event_id: int
    team_id: int
    opponent_id: int
    venue: str
    expected_goals_for: float
    expected_goals_against: float
    clean_sheet_probability: float
    attacking_score: float
    defensive_score: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class FixtureModel:
    """Venue-aware, early-season-shrunk team goal environment model."""

    def build(
        self,
        teams: list[dict[str, Any]],
        fixtures: list[dict[str, Any]],
        planning_event: int,
        max_horizon: int = 8,
    ) -> tuple[dict[int, list[FixtureEnvironment]], dict[str, Any]]:
        team_map = {int(team["id"]): team for team in teams}
        completed = [
            fixture
            for fixture in fixtures
            if fixture.get("finished")
            and fixture.get("team_h_score") is not None
            and fixture.get("team_a_score") is not None
            and int(fixture.get("event_id") or 0) < planning_event
        ]
        home_goals = sum(float(row["team_h_score"]) for row in completed)
        away_goals = sum(float(row["team_a_score"]) for row in completed)
        match_count = len(completed)
        # Stable scoring priors are used only when the current season sample is tiny.
        league_home = (home_goals + 30.0 * 1.52) / (match_count + 30.0)
        league_away = (away_goals + 30.0 * 1.22) / (match_count + 30.0)

        strength_fields = (
            "strength_attack_home",
            "strength_attack_away",
            "strength_defence_home",
            "strength_defence_away",
        )
        means = {
            field: fmean(float(team.get(field) or 1000.0) for team in teams)
            for field in strength_fields
        }
        records: dict[int, dict[str, float]] = {
            team_id: {"h_m": 0, "a_m": 0, "h_gf": 0, "a_gf": 0, "h_ga": 0, "a_ga": 0}
            for team_id in team_map
        }
        for row in completed:
            home = records[int(row["team_h_id"])]
            away = records[int(row["team_a_id"])]
            h_score, a_score = float(row["team_h_score"]), float(row["team_a_score"])
            home["h_m"] += 1; home["h_gf"] += h_score; home["h_ga"] += a_score
            away["a_m"] += 1; away["a_gf"] += a_score; away["a_ga"] += h_score

        prior_matches = 6.0
        ratings: dict[int, dict[str, float]] = {}
        for team_id, team in team_map.items():
            record = records[team_id]
            off_h = float(team.get("strength_attack_home") or means["strength_attack_home"]) / means["strength_attack_home"]
            off_a = float(team.get("strength_attack_away") or means["strength_attack_away"]) / means["strength_attack_away"]
            def_h = means["strength_defence_home"] / float(team.get("strength_defence_home") or means["strength_defence_home"])
            def_a = means["strength_defence_away"] / float(team.get("strength_defence_away") or means["strength_defence_away"])
            ratings[team_id] = {
                "attack_home": (record["h_gf"] + prior_matches * league_home * off_h) / ((record["h_m"] + prior_matches) * league_home),
                "attack_away": (record["a_gf"] + prior_matches * league_away * off_a) / ((record["a_m"] + prior_matches) * league_away),
                "weak_home": (record["h_ga"] + prior_matches * league_away * def_h) / ((record["h_m"] + prior_matches) * league_away),
                "weak_away": (record["a_ga"] + prior_matches * league_home * def_a) / ((record["a_m"] + prior_matches) * league_home),
            }

        environments: dict[int, list[FixtureEnvironment]] = {team_id: [] for team_id in team_map}
        future = [
            row
            for row in fixtures
            if row.get("event_id") is not None
            and planning_event <= int(row["event_id"]) < planning_event + max_horizon
        ]
        league_team_goal = (league_home + league_away) / 2.0
        for row in future:
            home_id, away_id = int(row["team_h_id"]), int(row["team_a_id"])
            home_xg = clamp(league_home * ratings[home_id]["attack_home"] * ratings[away_id]["weak_away"], 0.25, 3.6)
            away_xg = clamp(league_away * ratings[away_id]["attack_away"] * ratings[home_id]["weak_home"], 0.20, 3.3)
            for team_id, opponent_id, venue, xgf, xga in (
                (home_id, away_id, "H", home_xg, away_xg),
                (away_id, home_id, "A", away_xg, home_xg),
            ):
                clean = exp(-xga)
                environments[team_id].append(
                    FixtureEnvironment(
                        fixture_id=int(row["id"]),
                        event_id=int(row["event_id"]),
                        team_id=team_id,
                        opponent_id=opponent_id,
                        venue=venue,
                        expected_goals_for=round(xgf, 4),
                        expected_goals_against=round(xga, 4),
                        clean_sheet_probability=round(clean, 4),
                        attacking_score=round(clamp(50 + 28 * (xgf / league_team_goal - 1), 1, 99), 1),
                        defensive_score=round(clamp(50 + 30 * (clean / 0.25 - 1), 1, 99), 1),
                    )
                )
        metadata = {
            "completed_matches": match_count,
            "league_home_goals": round(league_home, 4),
            "league_away_goals": round(league_away, 4),
            "team_goal_baseline": round(league_team_goal, 4),
            "prior_matches_per_team_venue": prior_matches,
        }
        return environments, metadata

