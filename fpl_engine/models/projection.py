from __future__ import annotations

import json
from collections import defaultdict
from math import exp, sqrt
from statistics import fmean
from typing import Any

from fpl_engine.models import MODEL_NAME, MODEL_VERSION
from fpl_engine.models.fixture import FixtureEnvironment, FixtureModel
from fpl_engine.models.minutes import MinutesModel, MinutesProjection, clamp


HORIZONS = (1, 3, 5, 6, 8)
GOAL_POINTS = {1: 10, 2: 6, 3: 5, 4: 4}
CLEAN_SHEET_POINTS = {1: 4, 2: 4, 3: 1, 4: 0}
RATE_DEFAULTS = {
    "expected_goals": {1: 0.01, 2: 0.08, 3: 0.22, 4: 0.36},
    "expected_assists": {1: 0.01, 2: 0.10, 3: 0.18, 4: 0.14},
    "bonus": {1: 0.20, 2: 0.18, 3: 0.22, 4: 0.20},
    "saves": {1: 3.0, 2: 0.0, 3: 0.0, 4: 0.0},
    "yellow_cards": {1: 0.04, 2: 0.13, 3: 0.14, 4: 0.11},
    "red_cards": {1: 0.003, 2: 0.006, 3: 0.005, 4: 0.004},
    "own_goals": {1: 0.002, 2: 0.008, 3: 0.002, 4: 0.001},
    "penalties_missed": {1: 0.0, 2: 0.001, 3: 0.004, 4: 0.007},
    "penalties_saved": {1: 0.015, 2: 0.0, 3: 0.0, 4: 0.0},
}


def _poisson_conceded_penalty(expected_goals_against: float) -> float:
    """Expected FPL deductions from one point per two goals conceded."""
    lam = max(0.0, expected_goals_against)
    return max(0.0, lam / 2.0 - (1.0 - exp(-2.0 * lam)) / 4.0)


def _round_dict(values: dict[str, float]) -> dict[str, float]:
    return {key: round(value, 4) for key, value in values.items()}


class ProjectionEngine:
    """Explainable expected-points engine; no LLM participates in calculations."""

    def __init__(self) -> None:
        self.minutes_model = MinutesModel()
        self.fixture_model = FixtureModel()

    @staticmethod
    def _position_rates(players: list[dict[str, Any]]) -> dict[int, dict[str, float]]:
        fields = tuple(RATE_DEFAULTS)
        totals: dict[int, dict[str, float]] = {
            position: {field: 0.0 for field in fields} | {"minutes": 0.0}
            for position in (1, 2, 3, 4)
        }
        for player in players:
            position = int(player["position_id"])
            minutes = float(player.get("minutes") or 0)
            totals[position]["minutes"] += minutes
            for field in fields:
                totals[position][field] += float(player.get(field) or 0)
        rates: dict[int, dict[str, float]] = {}
        for position, values in totals.items():
            rates[position] = {}
            for field in fields:
                observed = values[field] * 90.0 / values["minutes"] if values["minutes"] >= 180 else None
                rates[position][field] = observed if observed is not None else RATE_DEFAULTS[field][position]
        return rates

    @staticmethod
    def _shrunk_rate(
        player: dict[str, Any], field: str, prior_rate: float, prior_minutes: float = 720.0
    ) -> float:
        minutes = float(player.get("minutes") or 0)
        observed_total = float(player.get(field) or 0)
        return (observed_total + prior_rate * prior_minutes / 90.0) * 90.0 / (minutes + prior_minutes)

    @staticmethod
    def _dc_priors(
        players: list[dict[str, Any]], histories: list[dict[str, Any]]
    ) -> tuple[dict[int, float], dict[int, tuple[int, int]]]:
        positions = {int(player["id"]): int(player["position_id"]) for player in players}
        cohort_hits = defaultdict(int)
        cohort_games = defaultdict(int)
        player_counts: dict[int, list[int]] = defaultdict(lambda: [0, 0])
        for row in histories:
            player_id = int(row["player_id"])
            position = positions.get(player_id)
            if position is None or int(row.get("minutes") or 0) <= 0 or position == 1:
                continue
            threshold = 10 if position == 2 else 12
            hit = int(row.get("defensive_contribution") or 0) >= threshold
            cohort_games[position] += 1
            cohort_hits[position] += int(hit)
            player_counts[player_id][1] += 1
            player_counts[player_id][0] += int(hit)
        defaults = {1: 0.0, 2: 0.24, 3: 0.10, 4: 0.025}
        priors = {
            position: (
                cohort_hits[position] / cohort_games[position]
                if cohort_games[position] >= 10
                else defaults[position]
            )
            for position in (1, 2, 3, 4)
        }
        return priors, {key: (value[0], value[1]) for key, value in player_counts.items()}

    def project_all(self, inputs: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        players = inputs["players"]
        teams = inputs["teams"]
        fixtures = inputs["fixtures"]
        histories = inputs["histories"]
        calibration = inputs.get("calibration") or {
            "status": "collecting",
            "evaluated_events": 0,
            "sample_size": 0,
            "points_intercept": 0.0,
            "points_slope": 1.0,
        }
        planning_event = int(inputs["planning_event"])
        environments, fixture_metadata = self.fixture_model.build(
            teams, fixtures, planning_event, max_horizon=8
        )
        history_by_player: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in histories:
            history_by_player[int(row["player_id"])].append(row)
        completed_games_by_team: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
        for fixture in fixtures:
            if fixture.get("finished") and fixture.get("event_id") is not None:
                event_id = int(fixture["event_id"])
                completed_games_by_team[int(fixture["team_h_id"])][event_id] += 1
                completed_games_by_team[int(fixture["team_a_id"])][event_id] += 1
        team_names = {int(team["id"]): team["short_name"] for team in teams}
        rates = self._position_rates(players)
        dc_priors, dc_counts = self._dc_priors(players, histories)
        baseline = float(fixture_metadata["team_goal_baseline"])

        player_contexts: dict[int, dict[str, Any]] = {}
        team_minute_totals: dict[int, float] = defaultdict(float)
        for player in players:
            player_id = int(player["id"])
            position = int(player["position_id"])
            minutes = self.minutes_model.project(
                player,
                history_by_player[player_id],
                completed_games_by_team[int(player["team_id"])],
            )
            player_rates = {
                field: self._shrunk_rate(player, field, rates[position][field])
                for field in RATE_DEFAULTS
            }
            role_xg_multiplier = 1.10 if player.get("penalties_order") == 1 else 1.0
            role_xa_multiplier = 1.0
            if player.get("corners_order") == 1:
                role_xa_multiplier += 0.06
            if player.get("direct_freekicks_order") == 1:
                role_xa_multiplier += 0.03
            dc_hits, dc_games = dc_counts.get(player_id, (0, 0))
            dc_probability = (dc_hits + 4.0 * dc_priors[position]) / (dc_games + 4.0)
            player_contexts[player_id] = {
                "minutes": minutes,
                "rates": player_rates,
                "role_xg_multiplier": role_xg_multiplier,
                "role_xa_multiplier": role_xa_multiplier,
                "dc_probability": dc_probability,
            }
            team_minute_totals[int(player["team_id"])] += minutes.expected_minutes

        team_capacity_factors = {
            team_id: min(1.0, 990.0 / total) if total > 0 else 1.0
            for team_id, total in team_minute_totals.items()
        }
        team_attack_pools: dict[int, dict[str, float]] = defaultdict(
            lambda: {"expected_goals": 0.0, "expected_assists": 0.0}
        )
        for player in players:
            player_id = int(player["id"])
            team_id = int(player["team_id"])
            context = player_contexts[player_id]
            context["minutes"] = context["minutes"].constrained(team_capacity_factors[team_id])
            expected_minutes = context["minutes"].expected_minutes
            team_attack_pools[team_id]["expected_goals"] += (
                context["rates"]["expected_goals"]
                * context["role_xg_multiplier"]
                * expected_minutes
                / 90.0
            )
            team_attack_pools[team_id]["expected_assists"] += (
                context["rates"]["expected_assists"]
                * context["role_xa_multiplier"]
                * expected_minutes
                / 90.0
            )

        rows: list[dict[str, Any]] = []
        for player in players:
            context = player_contexts[int(player["id"])]
            player_history = sorted(
                (
                    row
                    for row in history_by_player[int(player["id"])]
                    if int(row.get("event_id") or 0) < planning_event
                ),
                key=lambda row: int(row.get("event_id") or 0),
            )
            recent_history = player_history[-3:]
            baseline_inputs = {
                "recent_points": (
                    fmean(float(row.get("total_points") or 0) for row in recent_history)
                    if recent_history
                    else 0.0
                ),
                "total_points": float(player.get("total_points") or 0),
                "xgi_per_90": float(context["rates"].get("expected_goals") or 0)
                + float(context["rates"].get("expected_assists") or 0),
                "history_games": len(player_history),
            }
            rows.append(
                self._project_player(
                    player,
                    context["minutes"],
                    environments.get(int(player["team_id"]), []),
                    team_names,
                    context["rates"],
                    context["role_xg_multiplier"],
                    context["role_xa_multiplier"],
                    context["dc_probability"],
                    team_attack_pools[int(player["team_id"])],
                    baseline,
                    planning_event,
                    calibration,
                    baseline_inputs,
                )
            )
        metadata = {
            "model_name": MODEL_NAME,
            "model_version": MODEL_VERSION,
            "planning_event": planning_event,
            "player_count": len(rows),
            "history_rows": len(histories),
            "fixture_model": fixture_metadata,
            "team_minute_capacity": 990,
            "team_capacity_factors": {
                team_names.get(team_id, str(team_id)): round(factor, 4)
                for team_id, factor in team_capacity_factors.items()
            },
            "horizons": list(HORIZONS),
            "calibration_status": calibration.get("status", "collecting"),
            "calibration_events": int(calibration.get("evaluated_events") or 0),
            "calibration_sample_size": int(calibration.get("sample_size") or 0),
        }
        return rows, metadata

    def _project_player(
        self,
        player: dict[str, Any],
        minutes: MinutesProjection,
        environments: list[FixtureEnvironment],
        team_names: dict[int, str],
        player_rates: dict[str, float],
        role_xg_multiplier: float,
        role_xa_multiplier: float,
        dc_probability: float,
        team_attack_pool: dict[str, float],
        goal_baseline: float,
        planning_event: int,
        calibration: dict[str, Any],
        baseline_inputs: dict[str, Any],
    ) -> dict[str, Any]:
        position = int(player["position_id"])

        per_event: dict[int, dict[str, Any]] = {}
        for event_id in range(planning_event, planning_event + 8):
            event_fixtures = [env for env in environments if env.event_id == event_id]
            components = defaultdict(float)
            variance = 0.0
            details: list[dict[str, Any]] = []
            for env in event_fixtures:
                attack_scale = clamp(env.expected_goals_for / goal_baseline, 0.5, 1.75)
                raw_team_xg = team_attack_pool["expected_goals"] * attack_scale
                raw_team_xa = team_attack_pool["expected_assists"] * attack_scale
                # Small numerical guard keeps sums inside the declared 95%/85% budgets after JSON rounding.
                xg_allocation = min(1.0, env.expected_goals_for * 0.949 / raw_team_xg) if raw_team_xg > 0 else 1.0
                xa_allocation = min(1.0, env.expected_goals_for * 0.849 / raw_team_xa) if raw_team_xa > 0 else 1.0
                expected_goals = min(
                    player_rates["expected_goals"]
                    * role_xg_multiplier
                    * attack_scale
                    * minutes.expected_minutes
                    / 90.0
                    * xg_allocation,
                    env.expected_goals_for * 0.78,
                )
                expected_assists = min(
                    player_rates["expected_assists"]
                    * role_xa_multiplier
                    * attack_scale
                    * minutes.expected_minutes
                    / 90.0
                    * xa_allocation,
                    env.expected_goals_for * 0.72,
                )
                appearance = 2.0 * minutes.sixty_probability + minutes.under_sixty_probability
                goal_points = expected_goals * GOAL_POINTS[position]
                assist_points = expected_assists * 3.0
                clean_probability = minutes.sixty_probability * env.clean_sheet_probability
                clean_points = clean_probability * CLEAN_SHEET_POINTS[position]
                save_points = 0.0
                penalty_save_points = 0.0
                conceded_points = 0.0
                if position == 1:
                    save_points = player_rates["saves"] * minutes.expected_minutes / 90.0 / 3.0
                    penalty_save_points = player_rates["penalties_saved"] * minutes.expected_minutes / 90.0 * 5.0
                if position in (1, 2):
                    conceded_points = -minutes.sixty_probability * _poisson_conceded_penalty(env.expected_goals_against)
                bonus_points = min(1.6, player_rates["bonus"] * minutes.expected_minutes / 90.0)
                card_points = -(
                    player_rates["yellow_cards"] + 3.0 * player_rates["red_cards"]
                ) * minutes.expected_minutes / 90.0
                other_negative = -(
                    2.0 * player_rates["own_goals"] + 2.0 * player_rates["penalties_missed"]
                ) * minutes.expected_minutes / 90.0
                dc_points = 0.0 if position == 1 else 2.0 * dc_probability * minutes.sixty_probability
                fixture_components = {
                    "appearance": appearance,
                    "goals": goal_points,
                    "assists": assist_points,
                    "clean_sheet": clean_points,
                    "saves": save_points,
                    "penalty_saves": penalty_save_points,
                    "goals_conceded": conceded_points,
                    "bonus": bonus_points,
                    "defensive_contributions": dc_points,
                    "cards": card_points,
                    "other_negatives": other_negative,
                }
                for key, value in fixture_components.items():
                    components[key] += value
                appearance_second_moment = 4.0 * minutes.sixty_probability + minutes.under_sixty_probability
                variance += max(0.0, appearance_second_moment - appearance**2)
                variance += expected_goals * GOAL_POINTS[position] ** 2
                variance += expected_assists * 9.0
                variance += CLEAN_SHEET_POINTS[position] ** 2 * clean_probability * (1.0 - clean_probability)
                variance += bonus_points * 1.25 + dc_points * max(0.0, 2.0 - dc_points)
                details.append(
                    {
                        **env.as_dict(),
                        "opponent": team_names.get(env.opponent_id, str(env.opponent_id)),
                        "expected_goals": round(expected_goals, 4),
                        "expected_assists": round(expected_assists, 4),
                        "goal_probability": round(1.0 - exp(-expected_goals), 4),
                        "assist_probability": round(1.0 - exp(-expected_assists), 4),
                        "team_xg_allocation_factor": round(xg_allocation, 4),
                        "team_xa_allocation_factor": round(xa_allocation, 4),
                        "position_fixture_score": round(
                            (
                                env.defensive_score
                                if position == 1
                                else 0.75 * env.defensive_score + 0.25 * env.attacking_score
                                if position == 2
                                else 0.85 * env.attacking_score + 0.15 * env.defensive_score
                                if position == 3
                                else env.attacking_score
                            ),
                            1,
                        ),
                        "components": _round_dict(fixture_components),
                    }
                )
            mean = sum(components.values())
            # Never let a fitted intercept manufacture points in a blank gameweek.
            if event_fixtures and calibration.get("status") == "calibrated":
                calibrated_mean = max(
                    -2.0,
                    float(calibration.get("points_intercept") or 0.0)
                    + float(calibration.get("points_slope") or 1.0) * mean,
                )
                components["calibration_adjustment"] += calibrated_mean - mean
                mean = calibrated_mean
            per_event[event_id] = {
                "event_id": event_id,
                "fixture_count": len(event_fixtures),
                "expected_points": round(mean, 4),
                "variance": round(max(0.0, variance), 4),
                "components": _round_dict(dict(components)),
                "fixtures": details,
            }

        horizons: dict[int, dict[str, float]] = {}
        for horizon in HORIZONS:
            selected = [per_event[event] for event in range(planning_event, planning_event + horizon)]
            mean = sum(item["expected_points"] for item in selected)
            variance = sum(item["variance"] for item in selected)
            deviation = sqrt(max(variance, 0.0))
            horizons[horizon] = {
                "expected": round(mean, 2),
                "floor": round(max(-2.0, mean - 0.84 * deviation), 2),
                "median": round(max(-1.0, mean - 0.10 * deviation), 2),
                "ceiling": round(mean + 1.64 * deviation, 2),
            }
        default_events = [per_event[event] for event in range(planning_event, planning_event + 6)]
        components_6 = defaultdict(float)
        for item in default_events:
            for key, value in item["components"].items():
                components_6[key] += value
        performance_sample = float(player.get("minutes") or 0) / (float(player.get("minutes") or 0) + 720.0)
        confidence = clamp(0.72 * minutes.confidence + 0.18 * performance_sample + 0.05, 0.2, 0.9)
        confidence *= 0.92 + 0.08 * minutes.team_capacity_factor
        if calibration.get("status") == "calibrated" and calibration.get("reliability_score") is not None:
            confidence = 0.75 * confidence + 0.25 * float(calibration["reliability_score"])
        if not environments:
            confidence *= 0.5
        payload = {
            "model_name": MODEL_NAME,
            "model_version": MODEL_VERSION,
            "player": {
                "id": int(player["id"]),
                "name": player["web_name"],
                "team_id": int(player["team_id"]),
                "position_id": position,
                "price": float(player["now_cost"]) / 10.0,
                "status": player.get("status"),
                "news": player.get("news") or "",
            },
            "planning_event": planning_event,
            "minutes": minutes.as_dict(),
            "rates_per_90": _round_dict(player_rates),
            "role_adjustments": {
                "penalty_taker_xg_multiplier": role_xg_multiplier,
                "set_piece_xa_multiplier": round(role_xa_multiplier, 4),
            },
            "team_constraints": {
                "match_minutes_budget": 990,
                "minutes_capacity_factor": minutes.team_capacity_factor,
                "raw_player_xg_pool": round(team_attack_pool["expected_goals"], 4),
                "raw_player_xa_pool": round(team_attack_pool["expected_assists"], 4),
                "player_xg_budget_fraction": 0.95,
                "player_xa_budget_fraction": 0.85,
            },
            "defensive_contribution_probability": round(dc_probability, 4),
            "horizons": {str(key): value for key, value in horizons.items()},
            "components_6": _round_dict(dict(components_6)),
            "per_gameweek": [per_event[event] for event in range(planning_event, planning_event + 8)],
            "confidence": round(confidence, 4),
            "confidence_label": calibration.get("status", "collecting"),
            "calibration": {
                "status": calibration.get("status", "collecting"),
                "evaluated_events": int(calibration.get("evaluated_events") or 0),
                "sample_size": int(calibration.get("sample_size") or 0),
                "points_intercept": float(calibration.get("points_intercept") or 0.0),
                "points_slope": float(calibration.get("points_slope") or 1.0),
            },
            "baseline_inputs": {
                "recent_points": round(float(baseline_inputs.get("recent_points") or 0), 4),
                "total_points": round(float(baseline_inputs.get("total_points") or 0), 4),
                "xgi_per_90": round(float(baseline_inputs.get("xgi_per_90") or 0), 4),
                "fdr_score": round(
                    fmean(
                        float(fixture.get("position_fixture_score") or 0)
                        for fixture in per_event[planning_event].get("fixtures", [])
                    )
                    if per_event[planning_event].get("fixtures")
                    else 0.0,
                    4,
                ),
                "history_games": int(baseline_inputs.get("history_games") or 0),
                "captured_pre_deadline": True,
            },
        }
        price = max(float(player["now_cost"]) / 10.0, 0.1)
        return {
            "player_id": int(player["id"]),
            "xpts_1": horizons[1]["expected"],
            "xpts_3": horizons[3]["expected"],
            "xpts_5": horizons[5]["expected"],
            "xpts_6": horizons[6]["expected"],
            "xpts_8": horizons[8]["expected"],
            "value_6": round(horizons[6]["expected"] / price, 4),
            "expected_minutes": minutes.expected_minutes,
            "start_probability": minutes.start_probability,
            "sixty_probability": minutes.sixty_probability,
            "no_play_probability": minutes.no_play_probability,
            "confidence": round(confidence, 4),
            "floor_6": horizons[6]["floor"],
            "median_6": horizons[6]["median"],
            "ceiling_6": horizons[6]["ceiling"],
            "payload_json": json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
        }
