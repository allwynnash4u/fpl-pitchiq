from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from itertools import combinations
from math import exp
from typing import Any

from fpl_engine.data.repository import Repository
from fpl_engine.decision_safety import data_warnings as shared_data_warnings, selling_price_warnings
from fpl_engine.models import MODEL_NAME
from fpl_engine.models.minutes import clamp


class TransferOptimizer:
    """Rank legal transfers against keeping the squad, including bounded two-move routes."""

    MAX_OFFICIAL_DATA_AGE_HOURS = 12

    def __init__(self, repository: Repository):
        self.repository = repository

    @staticmethod
    def _fixture_score(payload: dict[str, Any], horizon: int) -> float:
        weeks = payload.get("per_gameweek", [])[:horizon]
        scores = [
            float(fixture.get("position_fixture_score") or 0)
            for week in weeks
            for fixture in week.get("fixtures", [])
        ]
        return sum(scores) / len(scores) if scores else 0.0

    @staticmethod
    def _official_fdr(
        fixtures: list[dict[str, Any]], squad: list[dict[str, Any]],
        planning_event: int | None, horizon: int,
    ) -> tuple[dict[str, Any], dict[int, float]]:
        """Summarise source FDR separately from the position-aware projection model."""
        if planning_event is None or not fixtures:
            return {"status": "unavailable", "horizon": horizon}, {}
        by_team_event: dict[tuple[int, int], list[int]] = {}
        by_team: dict[int, list[int]] = {}
        for fixture in fixtures:
            event = fixture.get("event_id")
            if event is None or not planning_event <= int(event) < planning_event + horizon:
                continue
            for team_key, difficulty_key in (("team_h_id", "team_h_difficulty"),
                                             ("team_a_id", "team_a_difficulty")):
                team_id = fixture.get(team_key)
                difficulty = fixture.get(difficulty_key)
                if team_id is None or difficulty is None or not 1 <= int(difficulty) <= 5:
                    continue
                by_team_event.setdefault((int(team_id), int(event)), []).append(int(difficulty))
                by_team.setdefault(int(team_id), []).append(int(difficulty))
        team_averages = {
            team_id: round(sum(values) / len(values), 1)
            for team_id, values in by_team.items() if values
        }
        starters = [player for player in squad if 1 <= int(player.get("squad_position") or 0) <= 11]
        if len(starters) != 11 or any(player.get("team_id") is None for player in starters):
            return {"status": "unavailable", "horizon": horizon}, team_averages
        scores: list[int] = []
        blank_player_weeks = 0
        for player in starters:
            team_id = int(player["team_id"])
            for event in range(planning_event, planning_event + horizon):
                values = by_team_event.get((team_id, event), [])
                if values:
                    scores.extend(values)
                else:
                    blank_player_weeks += 1
        if not scores:
            return {"status": "unavailable", "horizon": horizon}, team_averages
        return {
            "status": "ready",
            "horizon": horizon,
            "average": round(sum(scores) / len(scores), 1),
            "fixture_count": len(scores),
            "favourable_count": sum(value <= 2 for value in scores),
            "difficult_count": sum(value >= 4 for value in scores),
            "blank_player_weeks": blank_player_weeks,
            "definition": "Official FPL FDR, 1 easiest to 5 hardest. Starting-XI player-fixtures; doubles count twice and blanks are excluded from the average. Not the model's position-aware fixture score.",
        }, team_averages

    @classmethod
    def _data_warnings(cls, status: dict[str, Any]) -> list[str]:
        return shared_data_warnings(status)

    @staticmethod
    def _why(
        seller: dict[str, Any],
        buyer: dict[str, Any],
        seller_payload: dict[str, Any],
        buyer_payload: dict[str, Any],
        fixture_delta: float,
        value_delta: float,
    ) -> str:
        reasons: list[str] = []
        minutes_delta = float(buyer["expected_minutes"]) - float(seller["expected_minutes"])
        attack_delta = sum(float(buyer_payload.get("rates_per_90", {}).get(key) or 0) for key in ("expected_goals", "expected_assists")) - sum(
            float(seller_payload.get("rates_per_90", {}).get(key) or 0) for key in ("expected_goals", "expected_assists")
        )
        no_play_delta = float(seller["no_play_probability"]) - float(buyer["no_play_probability"])
        if fixture_delta >= 4:
            reasons.append("better fixture run")
        if attack_delta >= 0.06:
            reasons.append("stronger underlying attack")
        if minutes_delta >= 6:
            reasons.append("higher expected minutes")
        if no_play_delta >= 0.07:
            reasons.append("lower no-play risk")
        if value_delta >= 0.25:
            reasons.append("better points per £1m")
        if seller.get("status") != "a":
            reasons.append("removes an availability concern")
        if not reasons:
            reasons.append("higher multi-gameweek expected points")
        return " + ".join(reason.capitalize() if index == 0 else reason for index, reason in enumerate(reasons[:3])) + "."

    @staticmethod
    def _two_transfer_plan(
        squad: list[dict[str, Any]],
        pool: dict[int, dict[str, Any]],
        *,
        bank: float,
        free_transfers: int,
        horizon: int,
    ) -> dict[str, Any] | None:
        """Search legal simultaneous pairs; price and club rules apply to the final squad."""
        owned = {int(player["id"]) for player in squad}
        if not owned.issubset(pool):
            return None
        club_counts = Counter(int(pool[player_id]["team_id"]) for player_id in owned)
        options: dict[int, list[dict[str, Any]]] = {}
        for seller in squad:
            seller_id = int(seller["id"])
            old = pool[seller_id]
            eligible = [
                player for player_id, player in pool.items()
                if player_id not in owned
                and int(player["position_id"]) == int(old["position_id"])
                and player.get("status") not in {"i", "s", "u", "n"}
                and (player.get("chance_next") is None or int(player["chance_next"]) >= 75)
            ]
            # Keep strong upgrades and low-cost enablers; the search is bounded,
            # so its result is the best route found, not a global optimum.
            by_gain = sorted(eligible, key=lambda item: float(item["expected_points"]) - float(old["expected_points"]), reverse=True)[:18]
            by_price = sorted(eligible, key=lambda item: (float(item["price"]), -float(item["expected_points"])))[:6]
            options[seller_id] = list({int(item["id"]): item for item in [*by_gain, *by_price]}.values())

        best: dict[str, Any] | None = None
        hit = 4 * max(0, 2 - free_transfers)
        for first, second in combinations(squad, 2):
            first_id, second_id = int(first["id"]), int(second["id"])
            first_price = first.get("selling_price")
            second_price = second.get("selling_price")
            if first_price is None or second_price is None:
                continue
            budget = bank + (int(first_price) + int(second_price)) / 10.0
            for buy_first in options[first_id]:
                for buy_second in options[second_id]:
                    if int(buy_first["id"]) == int(buy_second["id"]):
                        continue
                    purchase = float(buy_first["price"]) + float(buy_second["price"])
                    if purchase > budget + 1e-9:
                        continue
                    next_clubs = club_counts.copy()
                    for player_id in (first_id, second_id):
                        next_clubs[int(pool[player_id]["team_id"])] -= 1
                    for player in (buy_first, buy_second):
                        next_clubs[int(player["team_id"])] += 1
                    if any(count > 3 for count in next_clubs.values()):
                        continue
                    gross = sum(
                        float(buyer["expected_points"]) - float(pool[seller_id]["expected_points"])
                        for seller_id, buyer in ((first_id, buy_first), (second_id, buy_second))
                    )
                    net = gross - hit
                    if best is None or net > best["net_expected_gain"]:
                        best = {
                            "action": "two_transfers",
                            "moves": [
                                {"sell": {"id": seller_id, "name": pool[seller_id]["web_name"]},
                                 "buy": {"id": int(buyer["id"]), "name": buyer["web_name"]}}
                                for seller_id, buyer in ((first_id, buy_first), (second_id, buy_second))
                            ],
                            "gross_expected_gain": round(gross, 2),
                            "hit_cost": hit,
                            "net_expected_gain": round(net, 2),
                            "bank_after": round(budget - purchase, 1),
                            "horizon": horizon,
                            "search_scope": "bounded shortlist of strong upgrades and low-cost enablers",
                        }
        return best

    def suggestions(
        self,
        *,
        horizon_override: int | None = None,
        risk_override: str | None = None,
    ) -> dict[str, Any]:
        profile = self.repository.profile()
        squad = self.repository.squad()
        calibration = self.repository.calibration_status() or {
            "status": "collecting",
            "evaluated_events": 0,
            "sample_size": 0,
        }
        status = self.repository.status()
        blocking_warnings = self._data_warnings(status)
        if not profile or not profile.get("team_id") or len(squad) != 15:
            return {
                "status": "team-required",
                "message": "Connect a complete 15-player FPL squad to generate team-specific transfers.",
                "suggestions": [],
                "calibration": calibration,
            }
        planning = status.get("planning_event")
        planning_event = planning.get("id") if isinstance(planning, dict) else planning
        price_warnings = selling_price_warnings(squad)
        data_warnings = [*blocking_warnings, *price_warnings]

        horizon = int(horizon_override or profile.get("horizon") or 6)
        if horizon not in {1, 3, 5, 6, 8}:
            horizon = 6
        fixture_reader = getattr(self.repository, "fixtures", None)
        raw_fixtures = fixture_reader(start_event=planning_event, horizon=horizon) if fixture_reader and planning_event else []
        squad_fdr, team_fdr = self._official_fdr(raw_fixtures, squad, planning_event, horizon)
        pools = {
            period: {int(player["id"]): player for player in self.repository.projections(horizon=period, limit=1000)}
            for period in {1, 3, 6, horizon}
        }
        selected_pool = pools[horizon]
        payloads = self.repository.current_projection_payloads()
        owned_ids = {int(player["id"]) for player in squad}
        club_counts = Counter(int(selected_pool[player_id]["team_id"]) for player_id in owned_ids if player_id in selected_pool)
        bank = float(profile.get("bank") or 0.0)
        free_transfers = int(profile.get("free_transfers") or 0)
        hit_cost = 0.0 if free_transfers >= 1 else 4.0
        opportunity_cost = 0.2 if free_transfers >= 2 else 0.7 if free_transfers == 1 else 0.0
        risk = str(risk_override or profile.get("risk_preference") or "balanced")
        if risk not in {"conservative", "balanced", "aggressive"}:
            risk = "balanced"
        candidates: list[dict[str, Any]] = []

        for squad_player in squad:
            seller_id = int(squad_player["id"])
            seller = selected_pool.get(seller_id)
            seller_payload = payloads.get(seller_id)
            if seller is None or seller_payload is None:
                continue
            raw_selling_price = squad_player.get("selling_price")
            selling_price = (
                float(raw_selling_price) / 10.0
                if raw_selling_price is not None
                else float(squad_player["current_price"])
            )
            available_budget = bank + selling_price
            for buyer_id, buyer in selected_pool.items():
                if buyer_id in owned_ids or int(buyer["position_id"]) != int(seller["position_id"]):
                    continue
                if float(buyer["price"]) > available_budget + 1e-9:
                    continue
                if buyer.get("status") in {"i", "s", "u", "n"}:
                    continue
                if buyer.get("chance_next") is not None and int(buyer["chance_next"]) < 75:
                    continue
                next_counts = club_counts.copy()
                next_counts[int(seller["team_id"])] -= 1
                next_counts[int(buyer["team_id"])] += 1
                if any(count > 3 for count in next_counts.values()):
                    continue
                buyer_payload = payloads.get(buyer_id)
                if buyer_payload is None:
                    continue
                gains = {
                    period: float(pools[period][buyer_id]["expected_points"])
                    - float(pools[period][seller_id]["expected_points"])
                    - hit_cost
                    - opportunity_cost
                    for period in (1, 3, 6)
                    if buyer_id in pools[period] and seller_id in pools[period]
                }
                selected_gain = (
                    float(buyer["expected_points"])
                    - float(seller["expected_points"])
                    - hit_cost
                    - opportunity_cost
                )
                seller_horizon = seller_payload["horizons"][str(horizon)]
                buyer_horizon = buyer_payload["horizons"][str(horizon)]
                floor_gain = float(buyer_horizon["floor"]) - float(seller_horizon["floor"])
                ceiling_gain = float(buyer_horizon["ceiling"]) - float(seller_horizon["ceiling"])
                fixture_delta = self._fixture_score(buyer_payload, horizon) - self._fixture_score(seller_payload, horizon)
                value_delta = float(buyer["value"]) - float(seller["value"])
                flexibility_penalty = 0.25 if available_budget - float(buyer["price"]) < 0.3 else 0.0
                risk_adjustment = (
                    0.18 * floor_gain
                    if risk == "conservative"
                    else 0.12 * ceiling_gain
                    if risk == "aggressive"
                    else 0.06 * floor_gain
                )
                ranking_score = selected_gain + risk_adjustment + 0.025 * fixture_delta - flexibility_penalty
                projection_confidence = (
                    float(buyer["confidence"]) + float(seller["confidence"])
                ) / 2.0
                gain_signal = 1.0 / (1.0 + exp(-selected_gain / 3.0))
                empirical_reliability = (
                    float(calibration.get("reliability_score"))
                    if calibration.get("reliability_score") is not None
                    else 0.5
                )
                confidence = 0.55 * projection_confidence + 0.25 * gain_signal + 0.20 * empirical_reliability
                if calibration.get("status") != "calibrated":
                    confidence = min(confidence, 0.65)
                candidates.append(
                    {
                        "action": "transfer",
                        "sell": {
                            "id": seller_id,
                            "name": seller["web_name"],
                            "team": seller["team"],
                            "price": selling_price,
                            "expected_points": round(float(seller["expected_points"]), 2),
                            "average_fdr": team_fdr.get(int(seller["team_id"])),
                        },
                        "buy": {
                            "id": buyer_id,
                            "name": buyer["web_name"],
                            "team": buyer["team"],
                            "price": float(buyer["price"]),
                            "expected_points": round(float(buyer["expected_points"]), 2),
                            "average_fdr": team_fdr.get(int(buyer["team_id"])),
                        },
                        "position": buyer["position"],
                        "net_expected_gain": round(selected_gain, 2),
                        "gain_1": round(gains.get(1, 0.0), 2),
                        "gain_3": round(gains.get(3, 0.0), 2),
                        "gain_6": round(gains.get(6, 0.0), 2),
                        "confidence": round(clamp(confidence, 0.2, 0.9), 4),
                        "ranking_score": round(ranking_score, 4),
                        "fixture_swing": round(fixture_delta, 1),
                        "bank_after": round(available_budget - float(buyer["price"]), 1),
                        "hit_cost": int(hit_cost),
                        "opportunity_cost": opportunity_cost,
                        "expected_minutes_delta": round(
                            float(buyer["expected_minutes"]) - float(seller["expected_minutes"]), 1
                        ),
                        "start_probability_delta": round(
                            float(buyer.get("start_probability") or 0)
                            - float(seller.get("start_probability") or 0), 4
                        ),
                        "no_play_risk_delta": round(
                            float(buyer["no_play_probability"]) - float(seller["no_play_probability"]), 4
                        ),
                        "why": self._why(
                            seller,
                            buyer,
                            seller_payload,
                            buyer_payload,
                            fixture_delta,
                            value_delta,
                        ),
                        "budget_assumption": (
                            "Official selling price"
                            if raw_selling_price is not None
                            else "Current price used because the public endpoint does not expose exact selling price"
                        ),
                        "risks": [
                            *(
                                [f"Costs {int(hit_cost)} points this gameweek"]
                                if hit_cost
                                else []
                            ),
                            *(
                                [f"{float(buyer['no_play_probability']) * 100:.0f}% projected no-play risk"]
                                if float(buyer["no_play_probability"]) >= 0.15
                                else []
                            ),
                            *(
                                ["Selling price is estimated from current price"]
                                if raw_selling_price is None
                                else []
                            ),
                        ],
                    }
                )

        candidates.sort(key=lambda item: (item["ranking_score"], item["net_expected_gain"]), reverse=True)
        two_transfer_plan = self._two_transfer_plan(
            squad, selected_pool, bank=bank, free_transfers=free_transfers, horizon=horizon
        ) if not data_warnings else None
        selected: list[dict[str, Any]] = []
        seller_counts: Counter[int] = Counter()
        buyer_counts: Counter[int] = Counter()
        for candidate in candidates:
            # A negative expected-value route is not an opportunity. The hold
            # baseline communicates that decision without manufacturing choice.
            if candidate["net_expected_gain"] <= 0:
                continue
            sell_id, buy_id = candidate["sell"]["id"], candidate["buy"]["id"]
            # Prefer distinct actionable routes instead of repeating one seller or target.
            if seller_counts[sell_id] >= 1 or buyer_counts[buy_id] >= 1:
                continue
            selected.append(candidate)
            seller_counts[sell_id] += 1
            buyer_counts[buy_id] += 1
            if len(selected) == 4:
                break

        base_threshold = max(0.8, 0.3 * horizon)
        # A hit needs materially more forecast edge because the four-point cost
        # is certain while the projected upside is uncertain. Collecting-state
        # models also need a small evidence margin before telling a manager to act.
        threshold = base_threshold + (3.0 if hit_cost else 0.0) + (
            1.0 if calibration.get("status") != "calibrated" else 0.0
        )
        recommend_hold = bool(blocking_warnings) or not selected or selected[0]["net_expected_gain"] < threshold
        best_gain = selected[0]["net_expected_gain"] if selected else 0.0
        hold = {
            "action": "do_nothing",
            "net_expected_gain": 0.0,
            "gain_1": 0.0,
            "gain_3": 0.0,
            "gain_6": 0.0,
            "confidence": round(
                0.72 if not selected else clamp(0.55 + abs(threshold - best_gain) / 10.0, 0.5, 0.82),
                4,
            ),
            "why": (
                f"Transfer assessment unavailable until official data is refreshed: {'; '.join(blocking_warnings)}."
                if blocking_warnings else
                f"No legal single transfer clears the {threshold:.1f}-point {horizon}-GW action threshold after transfer and opportunity costs."
                if recommend_hold
                else f"Baseline option: keep the squad and preserve flexibility. The best transfer clears the {threshold:.1f}-point action threshold by {best_gain - threshold:.1f} points."
            ),
            "recommended": recommend_hold,
            "risks": [],
        }
        if blocking_warnings:
            selected = [hold]
        elif recommend_hold:
            selected = [hold, *selected[:4]]
        else:
            for index, candidate in enumerate(selected):
                candidate["recommended"] = index == 0
            selected = [*selected, hold]

        projection_status_reader = getattr(self.repository, "projection_status", None)
        projection_status = projection_status_reader() if projection_status_reader else {}
        last_sync = status.get("last_sync") or {}

        return {
            "status": "ready",
            "decision_safety": "refresh_required" if blocking_warnings else "estimate_only" if price_warnings else "ready",
            "data_warnings": data_warnings,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "planning_event": (
                status["planning_event"].get("id")
                if isinstance(status.get("planning_event"), dict)
                else status.get("planning_event")
            ),
            "horizon": horizon,
            "bank": bank,
            "free_transfers": free_transfers,
            "suggestion_scope": "mutually_exclusive_single_transfers",
            "additional_transfer_hit_points": 4,
            "combined_moves_evaluated": not bool(data_warnings),
            "risk_preference": risk,
            "squad_fdr": squad_fdr,
            "transfer_cost": int(hit_cost),
            "opportunity_cost": opportunity_cost,
            "decision_threshold": round(threshold, 2),
            "threshold_components": {
                "base": round(base_threshold, 2),
                "hit_uncertainty_margin": 3.0 if hit_cost else 0.0,
                "uncalibrated_margin": 1.0 if calibration.get("status") != "calibrated" else 0.0,
            },
            "confidence_definition": (
                "The displayed evidence-strength score is a heuristic blend of player projection certainty, "
                "expected-gain strength, and an internal backtest score. It is not a calibrated probability "
                "of a successful transfer and not a guarantee of more points."
            ),
            "model_version": projection_status.get("model_version") or "fpl-xpts-v1",
            "model_name": MODEL_NAME,
            "projection_updated_at": projection_status.get("created_at"),
            "official_data_updated_at": last_sync.get("completed_at"),
            "used_stale_data": bool(last_sync.get("used_stale_data")),
            "calibration": calibration,
            "suggestions": selected,
            "two_transfer_plan": None if data_warnings else two_transfer_plan,
            "route_comparison": {
                "roll": {"transfers": 0, "net_expected_gain": 0.0, "next_week_free_transfers": min(5, free_transfers + 1)},
                "single": {"transfers": 1, "net_expected_gain": None if blocking_warnings else round(max((item["net_expected_gain"] for item in candidates), default=0.0), 2),
                           "next_week_free_transfers": min(5, max(0, free_transfers - 1) + 1)},
                "double": {"transfers": 2, "net_expected_gain": two_transfer_plan["net_expected_gain"] if two_transfer_plan and not data_warnings else None,
                           "hit_cost": 4 * max(0, 2 - free_transfers),
                           "next_week_free_transfers": min(5, max(0, free_transfers - 2) + 1)},
                "note": "Projected squad-player deltas only; this is not a legal-XI, captaincy, or full multiweek optimum.",
            },
        }
