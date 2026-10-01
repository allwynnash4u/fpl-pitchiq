from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from fpl_engine.data.repository import Repository
from fpl_engine.decision_safety import data_warnings, selling_price_warnings
from fpl_engine.models.minutes import clamp
from fpl_engine.optimizer.squad import POSITION_NAMES, SquadOptimizer


CHIPS = (
    ("bench_boost", "Bench Boost"),
    ("triple_captain", "Triple Captain"),
    ("wildcard", "Wildcard"),
    ("free_hit", "Free Hit"),
)
CHIP_ALIASES = {
    "bboost": "bench_boost",
    "benchboost": "bench_boost",
    "bench_boost": "bench_boost",
    "3xc": "triple_captain",
    "triplecaptain": "triple_captain",
    "triple_captain": "triple_captain",
    "wildcard": "wildcard",
    "freehit": "free_hit",
    "free_hit": "free_hit",
}


class ChipPlannerService:
    """Build explainable, team-specific chip plans from the installed forecast window."""

    def __init__(self, repository: Repository, squad_optimizer: SquadOptimizer) -> None:
        self.repository = repository
        self.squad_optimizer = squad_optimizer
        self._cache_key: tuple[Any, ...] | None = None
        self._cache_value: dict[str, Any] | None = None

    @staticmethod
    def _event_id(value: Any) -> int | None:
        if isinstance(value, dict):
            value = value.get("id")
        return int(value) if value is not None else None

    @staticmethod
    def _week(payload: dict[str, Any], event_id: int) -> dict[str, Any]:
        return next(
            (
                week
                for week in payload.get("per_gameweek", [])
                if int(week.get("event_id") or 0) == event_id
            ),
            {},
        )

    @staticmethod
    def _confidence(values: list[float], calibrated: bool) -> float:
        raw = sum(values) / max(len(values), 1)
        return round(clamp(raw, 0.35, 0.9 if calibrated else 0.65), 4)

    @staticmethod
    def _status(gain: float, threshold: float) -> str:
        if gain >= threshold:
            return "strong"
        if gain >= threshold * 0.6:
            return "consider"
        return "save"

    def _availability(self, profile: dict[str, Any], squad: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        used: dict[str, list[int]] = {key: [] for key, _ in CHIPS}
        for item in profile.get("chip_history") or []:
            raw_name = str(item.get("name") or item.get("chip") or "").lower().replace("-", "_")
            key = CHIP_ALIASES.get(raw_name.replace(" ", ""), CHIP_ALIASES.get(raw_name))
            if key:
                event = item.get("event") or item.get("gameweek")
                if event is not None:
                    used[key].append(int(event))
        # A multiplier of three is direct evidence that the imported event used Triple Captain.
        imported_event = profile.get("imported_gameweek")
        if imported_event and any(int(player.get("multiplier") or 0) >= 3 for player in squad):
            if int(imported_event) not in used["triple_captain"]:
                used["triple_captain"].append(int(imported_event))
        return {
            key: {
                "available": not bool(used[key]),
                "used_events": sorted(used[key]),
                "evidence": "Official entry history or imported picks" if used[key] else "No recorded use in imported chip history",
            }
            for key, _ in CHIPS
        }

    def _player_pool(self) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
        rows = {
            int(row["id"]): dict(row)
            for row in self.repository.projections(horizon=8, limit=1000)
        }
        return rows, self.repository.current_projection_payloads()

    def _event_player(
        self,
        row: dict[str, Any],
        payload: dict[str, Any],
        event_id: int,
    ) -> dict[str, Any]:
        week = self._week(payload, event_id)
        player = dict(row)
        expected = float(week.get("expected_points") or 0)
        player["expected_points"] = expected
        player["selection_score"] = expected
        player["fixture_count"] = int(week.get("fixture_count") or 0)
        player["week"] = week
        return player

    def _current_lineup(
        self,
        squad: list[dict[str, Any]],
        rows: dict[int, dict[str, Any]],
        payloads: dict[int, dict[str, Any]],
        event_id: int,
    ) -> dict[str, Any] | None:
        players = []
        for pick in squad:
            player_id = int(pick["id"])
            if player_id not in rows:
                continue
            players.append(self._event_player(rows[player_id], payloads.get(player_id, {}), event_id))
        if len(players) != 15:
            return None
        lineup = SquadOptimizer._best_lineup(tuple(players))
        starter_points = sum(float(player["expected_points"]) for player in lineup["starters"])
        captain = lineup["captain"]
        return {
            "formation": lineup["formation"],
            "starters": lineup["starters"],
            "bench": lineup["bench"],
            "captain": captain,
            "total": starter_points + float(captain["expected_points"]),
            "bench_points": sum(float(player["expected_points"]) for player in lineup["bench"]),
        }

    @staticmethod
    def _fixture_label(player: dict[str, Any]) -> str:
        fixtures = player.get("week", {}).get("fixtures", [])
        if not fixtures:
            return "Blank"
        return " + ".join(
            f"{fixture.get('opponent') or '—'} ({fixture.get('venue') or '—'})"
            for fixture in fixtures
        )

    def _triple_captain(
        self,
        events: list[int],
        squad: list[dict[str, Any]],
        rows: dict[int, dict[str, Any]],
        payloads: dict[int, dict[str, Any]],
    ) -> dict[str, Any]:
        opportunities = []
        for event in events:
            candidates = []
            for pick in squad:
                player_id = int(pick["id"])
                if player_id not in rows:
                    continue
                player = self._event_player(rows[player_id], payloads.get(player_id, {}), event)
                week = player["week"]
                fixtures = week.get("fixtures", [])
                fixture_score = max(
                    (float(item.get("position_fixture_score") or 0) for item in fixtures),
                    default=0,
                )
                goal_probability = max(
                    (float(item.get("goal_probability") or 0) for item in fixtures),
                    default=0,
                )
                assist_probability = max(
                    (float(item.get("assist_probability") or 0) for item in fixtures),
                    default=0,
                )
                minutes = payloads.get(player_id, {}).get("minutes", {})
                minutes_score = clamp(float(minutes.get("expected_minutes") or 0) / 90 * 100, 0, 100)
                rotation_risk = clamp(float(minutes.get("rotation_risk") or 0) * 100, 0, 100)
                penalty = float(payloads.get(player_id, {}).get("role_adjustments", {}).get("penalty_taker_xg_multiplier") or 1) > 1
                score = clamp(
                    0.34 * min(float(player["expected_points"]) / 10 * 100, 100)
                    + 0.18 * fixture_score
                    + 0.18 * minutes_score
                    + 0.14 * min(goal_probability * 180, 100)
                    + 0.08 * min(assist_probability * 250, 100)
                    + (8 if penalty else 0)
                    - 0.08 * rotation_risk,
                    0,
                    100,
                )
                candidates.append(
                    {
                        "id": player_id,
                        "name": player["web_name"],
                        "team": player["team"],
                        "event": event,
                        "fixture": self._fixture_label(player),
                        "expected_points": round(float(player["expected_points"]), 2),
                        "chip_gain": round(float(player["expected_points"]), 2),
                        "confidence": round(float(player.get("confidence") or 0.5), 4),
                        "scores": {
                            "fixture": round(fixture_score),
                            "minutes": round(minutes_score),
                            "goal_threat": round(min(goal_probability * 180, 100)),
                            "assist_threat": round(min(assist_probability * 250, 100)),
                            "rotation_risk": round(rotation_risk),
                            "overall": round(score),
                        },
                        "penalty_taker": penalty,
                        "double_gameweek": int(player.get("fixture_count") or 0) > 1,
                    }
                )
            candidates.sort(key=lambda item: (item["chip_gain"], item["scores"]["overall"]), reverse=True)
            if candidates:
                opportunities.append({"event": event, "gain": candidates[0]["chip_gain"], "candidate": candidates[0], "rankings": candidates[:5]})
        opportunities.sort(key=lambda item: item["gain"], reverse=True)
        best = opportunities[0] if opportunities else None
        return {"best_event": best["event"] if best else None, "best_gain": best["gain"] if best else 0, "opportunities": opportunities}

    def _bench_boost(
        self,
        events: list[int],
        squad: list[dict[str, Any]],
        rows: dict[int, dict[str, Any]],
        payloads: dict[int, dict[str, Any]],
        profile: dict[str, Any],
    ) -> dict[str, Any]:
        opportunities = []
        for event in events:
            lineup = self._current_lineup(squad, rows, payloads, event)
            if not lineup:
                continue
            bench = []
            start_values = []
            fixture_values = []
            double_count = 0
            for player in lineup["bench"]:
                payload = payloads.get(int(player["id"]), {})
                minutes = payload.get("minutes", {})
                fixtures = player.get("week", {}).get("fixtures", [])
                fixture_score = max((float(item.get("position_fixture_score") or 0) for item in fixtures), default=0)
                start_probability = float(minutes.get("start_probability") or 0)
                start_values.append(start_probability)
                fixture_values.append(fixture_score)
                double_count += int(len(fixtures) > 1)
                bench.append(
                    {
                        "id": int(player["id"]),
                        "name": player["web_name"],
                        "team": player["team"],
                        "position": POSITION_NAMES[int(player["position_id"])],
                        "fixture": self._fixture_label(player),
                        "expected_points": round(float(player["expected_points"]), 2),
                        "expected_minutes": round(float(minutes.get("expected_minutes") or 0)),
                        "start_probability": round(start_probability, 4),
                        "risk": "High" if float(minutes.get("no_play_probability") or 0) >= 0.25 else "Low",
                    }
                )
            points = float(lineup["bench_points"])
            readiness = clamp(
                40 * min(points / 20, 1)
                + 30 * (sum(start_values) / max(len(start_values), 1))
                + 20 * (sum(fixture_values) / max(len(fixture_values), 1) / 100)
                + 10 * min(double_count / 2, 1),
                0,
                100,
            )
            opportunities.append(
                {
                    "event": event,
                    "gain": round(points, 2),
                    "readiness": round(readiness),
                    "bench": bench,
                    "double_players": double_count,
                }
            )
        opportunities.sort(key=lambda item: (item["gain"], item["readiness"]), reverse=True)
        best = opportunities[0] if opportunities else None
        preparation = self._bench_preparation(best, squad, rows, payloads, profile) if best else None
        return {
            "best_event": best["event"] if best else None,
            "best_gain": best["gain"] if best else 0,
            "opportunities": opportunities,
            "preparation": preparation,
        }

    def _bench_preparation(
        self,
        best: dict[str, Any],
        squad: list[dict[str, Any]],
        rows: dict[int, dict[str, Any]],
        payloads: dict[int, dict[str, Any]],
        profile: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not best.get("bench"):
            return None
        seller = min(best["bench"], key=lambda item: item["expected_points"])
        seller_row = rows.get(int(seller["id"]), {})
        owned = {int(item["id"]) for item in squad}
        club_counts = Counter(int(rows[int(item["id"])]["team_id"]) for item in squad if int(item["id"]) in rows)
        selling_price = next(
            (
                float(item["selling_price"]) / 10
                if item.get("selling_price") is not None
                else float(item.get("current_price") or 0)
                for item in squad
                if int(item["id"]) == int(seller["id"])
            ),
            float(seller_row.get("price") or 0),
        )
        budget = selling_price + float(profile.get("bank") or 0)
        candidates = []
        for player_id, row in rows.items():
            if player_id in owned or int(row["position_id"]) != int(seller_row.get("position_id") or 0):
                continue
            if float(row["price"]) > budget + 1e-9 or row.get("status") in {"i", "s", "u", "n"}:
                continue
            if club_counts[int(row["team_id"])] >= 3 and int(row["team_id"]) != int(seller_row.get("team_id") or -1):
                continue
            event_player = self._event_player(row, payloads.get(player_id, {}), int(best["event"]))
            gain = float(event_player["expected_points"]) - float(seller["expected_points"])
            candidates.append((gain, event_player))
        if not candidates:
            return None
        gain, buyer = max(candidates, key=lambda item: item[0])
        if gain <= 0.25:
            return None
        return {
            "sell": seller,
            "buy": {
                "id": int(buyer["id"]),
                "name": buyer["web_name"],
                "team": buyer["team"],
                "price": round(float(buyer["price"]), 1),
                "expected_points": round(float(buyer["expected_points"]), 2),
            },
            "gain": round(gain, 2),
            "prepared_value": round(float(best["gain"]) + gain, 2),
        }

    def _optimized_windows(
        self,
        events: list[int],
        *,
        horizon: int,
        risk: str,
    ) -> list[dict[str, Any]]:
        last_event = max(events)
        results = []
        for event in events:
            actual_horizon = min(horizon, last_event - event + 1)
            if actual_horizon < 1:
                continue
            optimized = self.squad_optimizer.optimize(
                horizon_override=actual_horizon,
                risk_override=risk,
                start_event_override=event,
                search_limit_override=90,
                rebuild_mode=True,
            )
            if optimized.get("status") == "ready":
                results.append({"event": event, "horizon": actual_horizon, "optimized": optimized})
        return results

    def _wildcard(
        self,
        events: list[int],
        squad: list[dict[str, Any]],
        rows: dict[int, dict[str, Any]],
        payloads: dict[int, dict[str, Any]],
        risk: str,
    ) -> dict[str, Any]:
        opportunities = []
        complete_window_count = max(1, len(events) - 4)
        windows = self._optimized_windows(events[:complete_window_count], horizon=5, risk=risk)
        for item in windows:
            current_total = 0.0
            for event in range(item["event"], item["event"] + item["horizon"]):
                lineup = self._current_lineup(squad, rows, payloads, event)
                current_total += float(lineup["total"] if lineup else 0)
            optimized = item["optimized"]
            gain = float(optimized.get("total_expected_points") or 0) - current_total
            opportunities.append(
                {
                    "event": item["event"],
                    "horizon": item["horizon"],
                    "gain": round(gain, 2),
                    "current_points": round(current_total, 2),
                    "optimized_points": round(float(optimized.get("total_expected_points") or 0), 2),
                    "changes": int((optimized.get("comparison") or {}).get("changes") or 0),
                    "optimized": optimized,
                }
            )
        opportunities.sort(key=lambda item: item["gain"], reverse=True)
        best = opportunities[0] if opportunities else None
        current_event = events[0]
        horizon_comparison = []
        for horizon in (3, 5, 8):
            optimized = self.squad_optimizer.optimize(
                horizon_override=horizon,
                risk_override=risk,
                start_event_override=current_event,
                search_limit_override=90,
                rebuild_mode=True,
            )
            current_total = 0.0
            for event in events[:horizon]:
                lineup = self._current_lineup(squad, rows, payloads, event)
                current_total += float(lineup["total"] if lineup else 0)
            horizon_comparison.append(
                {
                    "horizon": horizon,
                    "current_points": round(current_total, 2),
                    "optimized_points": round(float(optimized.get("total_expected_points") or 0), 2),
                    "gain": round(float(optimized.get("total_expected_points") or 0) - current_total, 2),
                }
            )
        return {
            "best_event": best["event"] if best else None,
            "best_gain": best["gain"] if best else 0,
            "opportunities": opportunities,
            "horizon_comparison": horizon_comparison,
            "recommended_squad": best["optimized"] if best else None,
        }

    def _free_hit(
        self,
        events: list[int],
        squad: list[dict[str, Any]],
        rows: dict[int, dict[str, Any]],
        payloads: dict[int, dict[str, Any]],
        risk: str,
    ) -> dict[str, Any]:
        opportunities = []
        for item in self._optimized_windows(events, horizon=1, risk=risk):
            current = self._current_lineup(squad, rows, payloads, item["event"])
            optimized = item["optimized"]
            current_points = float(current["total"] if current else 0)
            optimized_points = float(optimized.get("total_expected_points") or 0)
            opportunities.append(
                {
                    "event": item["event"],
                    "gain": round(optimized_points - current_points, 2),
                    "current_points": round(current_points, 2),
                    "optimized_points": round(optimized_points, 2),
                    "blank_players": sum(
                        1
                        for player in (current or {}).get("starters", [])
                        if int(player.get("fixture_count") or 0) == 0
                    ),
                    "optimized": optimized,
                }
            )
        opportunities.sort(key=lambda item: item["gain"], reverse=True)
        best = opportunities[0] if opportunities else None
        return {
            "best_event": best["event"] if best else None,
            "best_gain": best["gain"] if best else 0,
            "opportunities": opportunities,
            "recommended_squad": best["optimized"] if best else None,
        }

    @staticmethod
    def _card(
        key: str,
        name: str,
        detail: dict[str, Any],
        availability: dict[str, Any],
        current_event: int,
        threshold: float,
        calibrated: bool,
    ) -> dict[str, Any]:
        opportunities = detail.get("opportunities") or []
        current = next((item for item in opportunities if int(item["event"]) == current_event), {})
        best_event = detail.get("best_event")
        best_gain = float(detail.get("best_gain") or 0)
        current_gain = float(current.get("gain") or 0)
        if not availability["available"]:
            recommendation = "USED"
            reason = f"Recorded as used in GW{', GW'.join(map(str, availability['used_events']))}."
        elif current_gain >= threshold and current_gain >= best_gain * 0.85:
            recommendation = "USE"
            reason = f"GW{current_event} clears the {threshold:.0f}-point opportunity threshold and is close to the best visible option."
        elif best_gain >= threshold:
            recommendation = "PREPARE"
            reason = f"Saving now preserves the stronger projected GW{best_event} opportunity."
        else:
            recommendation = "SAVE"
            reason = f"No gameweek in the visible forecast window clears the {threshold:.0f}-point action threshold."
        confidence = ChipPlannerService._confidence(
            [0.5 + min(best_gain / max(threshold, 1), 1) * 0.25], calibrated
        )
        alternative = (
            f"Use in GW{current_event} for an estimated +{current_gain:.1f} points."
            if best_event != current_event
            else "Save it and reassess after the next fixture or availability update."
        )
        return {
            "key": key,
            "name": name,
            "available": availability["available"],
            "used_events": availability["used_events"],
            "recommendation": recommendation,
            "best_event": best_event,
            "current_gain": round(current_gain, 2),
            "expected_gain": round(best_gain, 2),
            "confidence": confidence,
            "status": ChipPlannerService._status(best_gain, threshold) if availability["available"] else "used",
            "reason": reason,
            "alternative": alternative,
            "what_could_change": [
                "Injury or suspension news",
                "A blank or double Gameweek announcement",
                "Fixture postponements or rescheduling",
                "Price changes, rotation news or a changed squad",
            ],
        }

    @staticmethod
    def _strategy(
        events: list[int],
        details: dict[str, dict[str, Any]],
        availability: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        options = {
            key: [item for item in detail.get("opportunities", []) if float(item.get("gain") or 0) > 0]
            for key, detail in details.items()
        }

        def build(name: str, preference: str) -> dict[str, Any]:
            used_events: set[int] = set()
            sequence = []
            order = ["wildcard", "bench_boost", "triple_captain", "free_hit"]
            for key in order:
                if not availability[key]["available"]:
                    continue
                candidates = options.get(key, [])
                if preference == "late":
                    candidates = list(reversed(sorted(candidates, key=lambda item: int(item["event"]))))
                elif preference == "wc_bb" and key == "bench_boost" and sequence:
                    wildcard = next((item for item in sequence if item["chip"] == "wildcard"), None)
                    after = [item for item in candidates if wildcard and int(item["event"]) > int(wildcard["event"])]
                    candidates = sorted(after or candidates, key=lambda item: float(item.get("gain") or 0), reverse=True)
                for candidate in candidates:
                    event = int(candidate["event"])
                    if event in used_events:
                        continue
                    used_events.add(event)
                    sequence.append({"chip": key, "event": event, "gain": round(float(candidate.get("gain") or 0), 2)})
                    break
            gain = sum(float(item["gain"]) for item in sequence)
            return {"name": name, "sequence": sorted(sequence, key=lambda item: item["event"]), "projected_advantage": round(gain, 2)}

        strategies = [
            build("Best independent opportunities", "best"),
            build("Wildcard then Bench Boost", "wc_bb"),
            build("Save for later in the window", "late"),
        ]
        strategies.sort(key=lambda item: item["projected_advantage"], reverse=True)
        advantage = strategies[0]["projected_advantage"] - (strategies[1]["projected_advantage"] if len(strategies) > 1 else 0)
        return {
            "recommended": strategies[0]["name"],
            "projected_advantage": round(advantage, 2),
            "strategies": strategies,
            "window": {"start": events[0], "end": events[-1]},
            "method": "Sum of chip-specific incremental values with no two chips assigned to the same Gameweek.",
            "limitation": "This is an eight-Gameweek comparison score, not a full-season points forecast; future transfers and unannounced schedule changes are not fabricated.",
        }

    def plan(self, *, forced_chip: str | None = None, forced_event: int | None = None) -> dict[str, Any]:
        profile = self.repository.profile() or {}
        squad = self.repository.squad()
        status = self.repository.status()
        safety_warnings = data_warnings(status)
        budget_warnings = selling_price_warnings(squad)
        projection_status = self.repository.projection_status() or {}
        planning_event = self._event_id(status.get("planning_event"))
        if planning_event is None or not projection_status:
            return {"status": "projections-required", "message": "Generate projections before planning chips."}
        if len(squad) != 15:
            return {"status": "team-required", "message": "Import a complete 15-player squad before planning chips.", "planning_event": planning_event}
        cache_key = (projection_status.get("id"), profile.get("updated_at"), (status.get("last_sync") or {}).get("completed_at"), tuple((item.get("id"), item.get("squad_position"), item.get("selling_price")) for item in squad))
        if forced_chip is None and forced_event is None and cache_key == self._cache_key and self._cache_value is not None:
            return self._cache_value

        rows, payloads = self._player_pool()
        available_events = sorted(
            {
                int(week["event_id"])
                for payload in payloads.values()
                for week in payload.get("per_gameweek", [])
                if int(week.get("event_id") or 0) >= planning_event
            }
        )[:8]
        if not available_events:
            return {"status": "projections-required", "message": "The current projection has no future Gameweeks."}
        calibrated = (self.repository.calibration_status() or {}).get("status") == "calibrated"
        availability = self._availability(profile, squad)
        risk = str(profile.get("risk_preference") or "balanced")
        details = {
            "triple_captain": self._triple_captain(available_events, squad, rows, payloads),
            "bench_boost": self._bench_boost(available_events, squad, rows, payloads, profile),
            "wildcard": self._wildcard(available_events, squad, rows, payloads, risk),
            "free_hit": self._free_hit(available_events, squad, rows, payloads, risk),
        }
        thresholds = {"triple_captain": 8.0, "bench_boost": 14.0, "wildcard": 12.0, "free_hit": 12.0}
        cards = [
            self._card(key, name, details[key], availability[key], planning_event, thresholds[key], calibrated)
            for key, name in CHIPS
        ]
        if safety_warnings:
            for card in cards:
                if card.get("recommendation") == "USE":
                    card["recommendation"] = "WAIT"
        if budget_warnings:
            for card in cards:
                if card.get("key") in {"wildcard", "free_hit"} and card.get("recommendation") == "USE":
                    card["recommendation"] = "WAIT"
        actionable = [card for card in cards if card["available"]]
        use_now = [card for card in actionable if card["recommendation"] == "USE"]
        if safety_warnings:
            strongest = None
            headline = "Verify data and manager inputs before acting on a chip plan."
            copy = "; ".join(safety_warnings)
        elif use_now:
            strongest = max(use_now, key=lambda item: item["current_gain"])
            headline = f"Use {strongest['name']} in GW{planning_event}."
            copy = f"It adds an estimated {strongest['current_gain']:+.1f} points and is competitive with the best visible future opportunity."
        elif actionable:
            strongest = max(actionable, key=lambda item: item["expected_gain"])
            headline = "Save your chips this week."
            copy = f"Your strongest visible opportunity is {strongest['name']} in GW{strongest['best_event']}, worth an estimated {strongest['expected_gain']:+.1f} points."
        else:
            strongest = None
            headline = "No tracked chips remain available."
            copy = "The imported chip history records all four planner chips as used."

        scenario = None
        normalized_forced = CHIP_ALIASES.get(str(forced_chip or "").lower().replace(" ", ""))
        if normalized_forced and forced_event:
            detail = details.get(normalized_forced, {})
            opportunity = next((item for item in detail.get("opportunities", []) if int(item["event"]) == int(forced_event)), None)
            scenario = {
                "chip": normalized_forced,
                "event": int(forced_event),
                "available": availability[normalized_forced]["available"],
                "gain": round(float((opportunity or {}).get("gain") or 0), 2),
                "verdict": (
                    "This is the strongest visible timing."
                    if opportunity and int(detail.get("best_event") or 0) == int(forced_event)
                    else f"The model currently prefers GW{detail.get('best_event')}."
                ),
            }

        report = {
            "status": "ready",
            "decision_safety": "refresh_required" if safety_warnings else "budget_confirmation_required" if budget_warnings else "ready",
            "data_warnings": [*safety_warnings, *budget_warnings],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "planning_event": planning_event,
            "forecast_window": {"start": available_events[0], "end": available_events[-1], "events": available_events},
            "summary": {
                "headline": headline,
                "explanation": copy,
                "strongest_chip": strongest["key"] if strongest else None,
                "strongest_name": strongest["name"] if strongest else None,
                "best_event": strongest["best_event"] if strongest else None,
                "expected_gain": strongest["expected_gain"] if strongest else 0,
                "confidence": strongest["confidence"] if strongest else 0,
            },
            "availability": availability,
            "cards": cards,
            "triple_captain": details["triple_captain"],
            "bench_boost": details["bench_boost"],
            "wildcard": details["wildcard"],
            "free_hit": details["free_hit"],
            "season_strategy": self._strategy(available_events, details, availability),
            "scenario": scenario,
            "data_boundaries": [
                "Only the next eight projected Gameweeks are ranked.",
                "Double and blank Gameweeks are detected from installed official fixtures; unannounced changes are not assumed.",
                "European congestion is not scored because the installed official FPL feed does not provide that schedule.",
                "Confidence remains capped until rolling backtesting reaches calibration thresholds.",
            ],
        }
        if forced_chip is None and forced_event is None:
            self._cache_key, self._cache_value = cache_key, report
        return report
