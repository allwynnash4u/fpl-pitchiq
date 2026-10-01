from __future__ import annotations

from collections import Counter
from itertools import combinations
from typing import Any, Iterable

from fpl_engine.data.repository import Repository
from fpl_engine.decision_safety import data_warnings, selling_price_warnings
from fpl_engine.models.minutes import clamp


POSITION_QUOTAS = {1: 2, 2: 5, 3: 5, 4: 3}
POSITION_NAMES = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
FORMATIONS = (
    (3, 4, 3),
    (3, 5, 2),
    (4, 3, 3),
    (4, 4, 2),
    (4, 5, 1),
    (5, 3, 2),
    (5, 4, 1),
)


class SquadOptimizer:
    """Build a legal 15-player squad and select its best playable XI."""

    def __init__(self, repository: Repository):
        self.repository = repository

    @staticmethod
    def _available(player: dict[str, Any]) -> bool:
        if player.get("status") in {"i", "s", "u", "n"}:
            return False
        chance = player.get("chance_next")
        return chance is None or int(chance) >= 75

    @staticmethod
    def _risk_score(player: dict[str, Any], horizon: int, risk: str) -> float:
        expected = float(player.get("expected_points") or 0)
        scale = horizon / 6.0
        floor = float(player.get("floor_6") or expected / max(scale, 1e-9)) * scale
        ceiling = float(player.get("ceiling_6") or expected / max(scale, 1e-9)) * scale
        confidence = float(player.get("confidence") or 0.5)
        no_play = float(player.get("no_play_probability") or 0)
        if risk == "conservative":
            score = 0.72 * expected + 0.28 * floor
        elif risk == "aggressive":
            score = 0.80 * expected + 0.20 * ceiling
        else:
            score = expected
        return score * (0.94 + 0.06 * confidence) - expected * no_play * 0.08

    @staticmethod
    def _budget(profile: dict[str, Any] | None, current_squad: list[dict[str, Any]]) -> tuple[int, str]:
        if profile and len(current_squad) == 15:
            sale_value = 0.0
            exact = True
            for player in current_squad:
                selling_price = player.get("selling_price")
                if selling_price is None:
                    exact = False
                    sale_value += float(player.get("current_price") or 0)
                else:
                    sale_value += float(selling_price) / 10.0
            total = sale_value + float(profile.get("bank") or 0)
            assumption = (
                "Imported squad selling prices plus bank"
                if exact
                else "Current prices used where public selling prices are unavailable, plus bank"
            )
            return int(round(total * 10)), assumption
        return 1000, "Standard £100.0m initial squad budget"

    def _prepare_players(
        self,
        horizon: int,
        risk: str,
        *,
        start_event: int | None = None,
        current_squad: list[dict[str, Any]] | None = None,
        continuity_bonus: float = 0.0,
    ) -> list[dict[str, Any]]:
        query_horizon = horizon if horizon in {1, 3, 5, 6, 8} and start_event is None else 8
        rows = self.repository.projections(horizon=query_horizon, limit=1000)
        payloads = self.repository.current_projection_payloads() if start_event is not None else {}
        owned = {int(player["id"]): player for player in (current_squad or [])}
        prepared: list[dict[str, Any]] = []
        for row in rows:
            player_id = int(row["id"])
            if not self._available(row) and player_id not in owned:
                continue
            position_id = int(row["position_id"])
            if position_id not in POSITION_QUOTAS:
                continue
            player = dict(row)
            if start_event is not None:
                weeks = [
                    week
                    for week in payloads.get(int(row["id"]), {}).get("per_gameweek", [])
                    if start_event <= int(week.get("event_id") or 0) < start_event + horizon
                ]
                if not weeks:
                    continue
                expected = sum(float(week.get("expected_points") or 0) for week in weeks)
                player["expected_points"] = expected
                horizon_scale = max(horizon / 6.0, 1e-9)
                player["floor_6"] = expected * 0.58 / horizon_scale
                player["ceiling_6"] = expected * 1.62 / horizon_scale
            squad_player = owned.get(player_id)
            player["cost"] = (
                int(squad_player["selling_price"])
                if squad_player and squad_player.get("selling_price") is not None
                else int(round(float((squad_player or {}).get("current_price") or row["price"]) * 10))
            )
            player["selection_score"] = self._risk_score(row, horizon, risk)
            if start_event is not None:
                player["selection_score"] = self._risk_score(player, horizon, risk)
            player["is_owned"] = player_id in owned
            if player["is_owned"]:
                player["selection_score"] += continuity_bonus
            prepared.append(player)
        return prepared

    @staticmethod
    def _shortlist(
        players: Iterable[dict[str, Any]], quota: int, *, compact: bool = False
    ) -> list[dict[str, Any]]:
        pool = list(players)
        score_count, value_count, price_count = (
            (11, 6, max(6, quota + 1)) if compact else (18, 10, max(8, quota + 3))
        )
        by_score = sorted(pool, key=lambda row: (row["selection_score"], row["expected_points"]), reverse=True)[:score_count]
        by_value = sorted(
            pool,
            key=lambda row: (row["selection_score"] / max(row["cost"], 1), row["selection_score"]),
            reverse=True,
        )[:value_count]
        by_price = sorted(pool, key=lambda row: (row["cost"], -row["selection_score"]))[:price_count]
        selected = {int(row["id"]): row for row in (*by_score, *by_value, *by_price)}
        return list(selected.values())

    @staticmethod
    def _position_combinations(
        players: list[dict[str, Any]], quota: int, budget: int, limit: int = 450
    ) -> list[dict[str, Any]]:
        options: list[dict[str, Any]] = []
        for group in combinations(players, quota):
            club_counts = Counter(int(player["team_id"]) for player in group)
            if max(club_counts.values(), default=0) > 3:
                continue
            cost = sum(int(player["cost"]) for player in group)
            if cost > budget:
                continue
            options.append(
                {
                    "players": group,
                    "cost": cost,
                    "score": sum(float(player["selection_score"]) for player in group),
                    "clubs": club_counts,
                }
            )
        options.sort(key=lambda option: (option["score"], -option["cost"]), reverse=True)
        owned_group = tuple(player for player in players if player.get("is_owned"))
        owned_key = tuple(sorted(int(player["id"]) for player in owned_group))
        owned_option = None
        if len(owned_group) == quota:
            owned_clubs = Counter(int(player["team_id"]) for player in owned_group)
            owned_cost = sum(int(player["cost"]) for player in owned_group)
            if max(owned_clubs.values(), default=0) <= 3 and owned_cost <= budget:
                owned_option = {
                    "players": owned_group,
                    "cost": owned_cost,
                    "score": sum(float(player["selection_score"]) for player in owned_group),
                    "clubs": owned_clubs,
                }
        if len(options) <= limit:
            if owned_option and not any(tuple(sorted(int(player["id"]) for player in option["players"])) == owned_key for option in options):
                options.append(owned_option)
            return options

        # Retain strong inexpensive routes so an expensive early position group cannot
        # crowd every feasible complete squad out of the beam.
        retained = options[: int(limit * 0.75)]
        seen = {tuple(int(player["id"]) for player in option["players"]) for option in retained}
        for option in sorted(options, key=lambda value: (value["cost"], -value["score"])):
            key = tuple(int(player["id"]) for player in option["players"])
            if key in seen:
                continue
            retained.append(option)
            seen.add(key)
            if len(retained) >= limit:
                break
        if owned_option and owned_key not in seen:
            retained[-1] = owned_option
        return retained

    @staticmethod
    def _merge_beam(
        beam: list[dict[str, Any]],
        options: list[dict[str, Any]],
        budget: int,
        minimum_remaining: int,
        limit: int = 450,
    ) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        for partial in beam:
            for option in options:
                cost = int(partial["cost"]) + int(option["cost"])
                if cost + minimum_remaining > budget:
                    continue
                club_counts = Counter(partial["clubs"])
                club_counts.update(option["clubs"])
                if max(club_counts.values(), default=0) > 3:
                    continue
                merged.append(
                    {
                        "players": (*partial["players"], *option["players"]),
                        "cost": cost,
                        "score": float(partial["score"]) + float(option["score"]),
                        "clubs": club_counts,
                    }
                )
        merged.sort(key=lambda value: (value["score"], -value["cost"]), reverse=True)
        if len(merged) <= limit:
            return merged
        strong = merged[: int(limit * 0.8)]
        seen = {tuple(sorted(int(player["id"]) for player in state["players"])) for state in strong}
        continuity = sorted(
            merged,
            key=lambda value: (
                sum(bool(player.get("is_owned")) for player in value["players"]),
                value["score"],
            ),
            reverse=True,
        )
        for state in continuity:
            key = tuple(sorted(int(player["id"]) for player in state["players"]))
            if key in seen:
                continue
            strong.append(state)
            seen.add(key)
            if len(strong) >= int(limit * 0.9):
                break
        for state in sorted(merged, key=lambda value: (value["cost"], -value["score"])):
            key = tuple(sorted(int(player["id"]) for player in state["players"]))
            if key in seen:
                continue
            strong.append(state)
            seen.add(key)
            if len(strong) >= limit:
                break
        return strong

    @staticmethod
    def _best_lineup(squad: tuple[dict[str, Any], ...]) -> dict[str, Any]:
        grouped = {
            position_id: sorted(
                (player for player in squad if int(player["position_id"]) == position_id),
                key=lambda player: (player["selection_score"], player["expected_points"]),
                reverse=True,
            )
            for position_id in POSITION_QUOTAS
        }
        best: dict[str, Any] | None = None
        for defenders, midfielders, forwards in FORMATIONS:
            starters = (
                grouped[1][:1]
                + grouped[2][:defenders]
                + grouped[3][:midfielders]
                + grouped[4][:forwards]
            )
            if len(starters) != 11:
                continue
            ordered = sorted(starters, key=lambda player: player["selection_score"], reverse=True)
            captain, vice = ordered[0], ordered[1]
            starter_ids = {int(player["id"]) for player in starters}
            bench_outfield = sorted(
                (
                    player
                    for player in squad
                    if int(player["id"]) not in starter_ids and int(player["position_id"]) != 1
                ),
                key=lambda player: player["selection_score"],
                reverse=True,
            )
            bench_goalkeeper = [
                player
                for player in grouped[1]
                if int(player["id"]) not in starter_ids
            ]
            # FPL presents the substitute goalkeeper first, followed by the
            # three ordered outfield substitutes.
            bench = [*bench_goalkeeper, *bench_outfield]
            objective = (
                sum(float(player["selection_score"]) for player in starters)
                + float(captain["selection_score"])
                + 0.15 * sum(float(player["selection_score"]) for player in bench_outfield)
                + 0.06 * sum(float(player["selection_score"]) for player in bench_goalkeeper)
            )
            candidate = {
                "formation": f"{defenders}-{midfielders}-{forwards}",
                "starters": starters,
                "bench": bench,
                "captain": captain,
                "vice_captain": vice,
                "objective": objective,
            }
            if best is None or candidate["objective"] > best["objective"]:
                best = candidate
        if best is None:
            raise ValueError("No legal starting formation could be selected")
        return best

    @staticmethod
    def _serialize_player(player: dict[str, Any], role: str, order: int) -> dict[str, Any]:
        return {
            "id": int(player["id"]),
            "name": player["web_name"],
            "team_id": int(player["team_id"]),
            "team": player["team"],
            "position_id": int(player["position_id"]),
            "position": POSITION_NAMES[int(player["position_id"])],
            "price": round(float(player["price"]), 1),
            "expected_points": round(float(player["expected_points"]), 2),
            "start_probability": round(float(player.get("start_probability") or 0), 4),
            "confidence": round(float(player.get("confidence") or 0), 4),
            "role": role,
            "order": order,
        }

    def optimize(
        self,
        *,
        horizon_override: int | None = None,
        risk_override: str | None = None,
        start_event_override: int | None = None,
        search_limit_override: int | None = None,
        rebuild_mode: bool = False,
    ) -> dict[str, Any]:
        profile = self.repository.profile() or {}
        current_squad = self.repository.squad()
        horizon = int(horizon_override or profile.get("horizon") or 6)
        if start_event_override is None and horizon not in {1, 3, 5, 6, 8}:
            horizon = 6
        if start_event_override is not None and not 1 <= horizon <= 8:
            raise ValueError("window horizon must be between 1 and 8 gameweeks")
        risk = str(risk_override or profile.get("risk_preference") or "balanced")
        if risk not in {"conservative", "balanced", "aggressive"}:
            risk = "balanced"
        budget, budget_assumption = self._budget(profile, current_squad)
        planning_status = self.repository.status()
        planning = planning_status.get("planning_event")
        planning_event = planning.get("id") if isinstance(planning, dict) else planning
        free_transfers = max(0, min(5, int(profile.get("free_transfers") or 0)))
        chip_history = profile.get("chip_history") or []
        wildcard_last_week = any(
            str(chip.get("name") or "").lower() in {"wildcard", "wc"}
            and int(chip.get("event") or 0) == int(planning_event or 0) - 1
            for chip in chip_history
        )
        continuity_mode = len(current_squad) == 15 and not rebuild_mode
        allowed_changes = min(1 if wildcard_last_week else 2, free_transfers) if continuity_mode else 15
        churn_penalty = (max(1.5, 0.35 * horizon) * (1.6 if wildcard_last_week else 1.0)) if continuity_mode else 0.0
        players = self._prepare_players(
            horizon,
            risk,
            start_event=start_event_override,
            current_squad=current_squad if continuity_mode else None,
            continuity_bonus=churn_penalty,
        )
        search_limit = max(40, min(int(search_limit_override or 450), 450))
        compact_search = search_limit < 200
        if not players:
            return {
                "status": "projections-required",
                "message": "Generate player projections before optimizing a squad.",
                "squad": [],
            }

        position_options: dict[int, list[dict[str, Any]]] = {}
        for position_id, quota in POSITION_QUOTAS.items():
            shortlist = self._shortlist(
                (player for player in players if int(player["position_id"]) == position_id),
                quota,
                compact=compact_search,
            )
            options = self._position_combinations(shortlist, quota, budget, limit=search_limit)
            if not options:
                return {
                    "status": "no-solution",
                    "message": f"No legal {POSITION_NAMES[position_id]} group fits the available budget.",
                    "squad": [],
                }
            position_options[position_id] = options

        order = (1, 4, 2, 3)
        minimum_costs = {
            position_id: min(option["cost"] for option in position_options[position_id])
            for position_id in order
        }
        beam: list[dict[str, Any]] = [{"players": (), "cost": 0, "score": 0.0, "clubs": Counter()}]
        for index, position_id in enumerate(order):
            minimum_remaining = sum(minimum_costs[item] for item in order[index + 1 :])
            beam = self._merge_beam(
                beam,
                position_options[position_id],
                budget,
                minimum_remaining,
                limit=search_limit,
            )
            if not beam:
                return {
                    "status": "no-solution",
                    "message": "No legal 15-player squad fits the available budget and three-per-club rule.",
                    "squad": [],
                }

        current_ids = {int(player["id"]) for player in current_squad}
        if continuity_mode:
            prepared_by_id = {int(player["id"]): player for player in players}
            if current_ids.issubset(prepared_by_id):
                unchanged_players = tuple(prepared_by_id[player_id] for player_id in current_ids)
                unchanged_clubs = Counter(int(player["team_id"]) for player in unchanged_players)
                unchanged_cost = sum(int(player["cost"]) for player in unchanged_players)
                unchanged_positions = Counter(int(player["position_id"]) for player in unchanged_players)
                unchanged_key = tuple(sorted(current_ids))
                if (
                    unchanged_positions == Counter(POSITION_QUOTAS)
                    and max(unchanged_clubs.values(), default=0) <= 3
                    and unchanged_cost <= budget
                    and not any(tuple(sorted(int(player["id"]) for player in state["players"])) == unchanged_key for state in beam)
                ):
                    beam.append({
                        "players": unchanged_players,
                        "cost": unchanged_cost,
                        "score": sum(float(player["selection_score"]) for player in unchanged_players),
                        "clubs": unchanged_clubs,
                    })
        evaluated = []
        for state in beam:
            optimized_ids = {int(player["id"]) for player in state["players"]}
            changes = len(current_ids - optimized_ids) if continuity_mode else 0
            if changes > allowed_changes:
                continue
            lineup = self._best_lineup(state["players"])
            evaluated.append((float(lineup["objective"]), -int(state["cost"]), state, lineup))
        if not evaluated:
            return {
                "status": "no-solution",
                "message": "No legal continuity plan fits the current squad, budget and transfer allowance.",
                "squad": [],
            }
        _, _, winner, lineup = max(evaluated, key=lambda value: (value[0], value[1]))

        captain_id = int(lineup["captain"]["id"])
        vice_id = int(lineup["vice_captain"]["id"])
        serialized_starters = []
        for index, player in enumerate(lineup["starters"], start=1):
            role = "captain" if int(player["id"]) == captain_id else "vice-captain" if int(player["id"]) == vice_id else "starter"
            serialized_starters.append(self._serialize_player(player, role, index))
        serialized_bench = [
            self._serialize_player(player, "bench", index)
            for index, player in enumerate(lineup["bench"], start=1)
        ]
        optimized_ids = {int(player["id"]) for player in winner["players"]}
        transfers_in = sorted(
            (player for player in (*serialized_starters, *serialized_bench) if player["id"] not in current_ids),
            key=lambda player: (player["position_id"], -player["expected_points"]),
        )
        current_names = {int(player["id"]): player.get("web_name", str(player["id"])) for player in current_squad}
        transfers_out = [
            {"id": player_id, "name": current_names[player_id]}
            for player_id in sorted(current_ids - optimized_ids)
        ]
        starter_expected = sum(float(player["expected_points"]) for player in lineup["starters"])
        captain_bonus = float(lineup["captain"]["expected_points"])
        bench_expected = sum(float(player["expected_points"]) for player in lineup["bench"])
        weighted_confidence = sum(
            float(player.get("confidence") or 0) * float(player["expected_points"])
            for player in lineup["starters"]
        ) / max(starter_expected, 1e-9)
        status = planning_status
        blocking_warnings = data_warnings(status)
        price_warnings = selling_price_warnings(current_squad)
        warnings = [*blocking_warnings, *price_warnings]
        return {
            "status": "ready",
            "decision_safety": "refresh_required" if blocking_warnings else "estimate_only" if price_warnings else "ready",
            "data_warnings": warnings,
            "planning_event": planning_event,
            "window_start_event": int(start_event_override or planning_event or 0) or None,
            "horizon": horizon,
            "risk_preference": risk,
            "optimizer_mode": "chip_rebuild" if rebuild_mode else "continuity",
            "actionable": not rebuild_mode and len(current_squad) == 15 and not warnings,
            "available_free_transfers": free_transfers,
            "maximum_recommended_changes": allowed_changes,
            "wildcard_cooldown": wildcard_last_week,
            "continuity_penalty_per_change": round(churn_penalty, 2),
            "budget": round(budget / 10.0, 1),
            "budget_assumption": budget_assumption,
            "squad_cost": round(int(winner["cost"]) / 10.0, 1),
            "bank_remaining": round((budget - int(winner["cost"])) / 10.0, 1),
            "formation": lineup["formation"],
            "starting_expected_points": round(starter_expected, 2),
            "captain_bonus": round(captain_bonus, 2),
            "total_expected_points": round(starter_expected + captain_bonus, 2),
            "bench_expected_points": round(bench_expected, 2),
            "confidence": round(clamp(weighted_confidence, 0.2, 0.95), 4),
            "captain": self._serialize_player(lineup["captain"], "captain", 1),
            "vice_captain": self._serialize_player(lineup["vice_captain"], "vice-captain", 2),
            "starting_xi": serialized_starters,
            "bench": serialized_bench,
            "squad": [*serialized_starters, *serialized_bench],
            "comparison": {
                "has_imported_squad": len(current_squad) == 15,
                "players_kept": len(current_ids & optimized_ids),
                "changes": len(current_ids - optimized_ids),
                "transfers_in": transfers_in,
                "transfers_out": transfers_out,
            },
            "constraints": {
                "players": 15,
                "positions": {POSITION_NAMES[key]: value for key, value in POSITION_QUOTAS.items()},
                "maximum_per_club": 3,
                "budget_enforced": True,
                "availability_filter": "75% chance or better; injured, suspended and unavailable players excluded",
            },
            "method": (
                "Continuity-first constrained search with transfer cap and churn penalty"
                if continuity_mode
                else "Unrestricted chip rebuild search with exact legal-XI evaluation"
            ),
        }
