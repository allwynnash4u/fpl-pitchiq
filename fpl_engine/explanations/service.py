from __future__ import annotations

import re
import unicodedata
from typing import Any

from fpl_engine.dashboard.service import ALLOWED_HORIZONS, ALLOWED_RISKS, DashboardService
from fpl_engine.data.repository import Repository
from fpl_engine.chips.service import CHIP_ALIASES, ChipPlannerService


class ExplanationService:
    """Turn calculated model outputs into bounded, source-aware explanations."""

    STARTERS = [
        "What transfer should I make and why?",
        "Should I roll, make one move, or take a hit?",
        "Why not another player?",
        "Who should I captain?",
        "What are my biggest squad risks?",
        "How reliable are these projections?",
        "What does the wildcard rebuild suggest?",
        "Should I use a chip this Gameweek?",
        "Plan all my chips for the next eight Gameweeks.",
    ]

    def __init__(
        self,
        repository: Repository,
        dashboard_service: DashboardService,
        chip_planner: ChipPlannerService | None = None,
    ) -> None:
        self.repository = repository
        self.dashboard_service = dashboard_service
        self.chip_planner = chip_planner

    @staticmethod
    def _normalise(value: str) -> str:
        folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
        return re.sub(r"[^a-z0-9]+", " ", folded.lower()).strip()

    @staticmethod
    def _evidence(label: str, value: str, source: str) -> dict[str, str]:
        return {"label": label, "value": value, "source": source}

    @staticmethod
    def _event_id(value: Any) -> int | None:
        if isinstance(value, dict):
            return value.get("id")
        return value

    @staticmethod
    def _fixture_score(payload: dict[str, Any], horizon: int) -> float:
        values = [
            float(fixture.get("position_fixture_score") or 0)
            for week in payload.get("per_gameweek", [])[:horizon]
            for fixture in week.get("fixtures", [])
        ]
        return sum(values) / len(values) if values else 0.0

    def suggestions(self) -> dict[str, Any]:
        return {
            "mode": "deterministic-grounded",
            "questions": self.STARTERS,
            "guardrail": "Answers use installed FPL data and calculated engine outputs only.",
        }

    def answer(
        self,
        question: str,
        *,
        horizon: int | None = None,
        risk: str | None = None,
        scenario: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        question = str(question or "").strip()
        if not question:
            raise ValueError("question is required")
        if len(question) > 500:
            raise ValueError("question must be 500 characters or fewer")
        if horizon is not None and int(horizon) not in ALLOWED_HORIZONS:
            raise ValueError("horizon must be one of 1, 3, 5, 6, or 8")
        if risk is not None and str(risk).lower() not in ALLOWED_RISKS:
            raise ValueError("risk must be conservative, balanced, or aggressive")

        dashboard = self.dashboard_service.build(horizon=horizon, risk=risk)
        filters = dashboard["filters"]
        normalized = self._normalise(question)
        chip_terms = ("chip", "wildcard", "free hit", "bench boost", "triple captain")
        decision_terms = ("roll", "transfer", "sell", "buy", "move", "hold", "captain", "rebuild", "optimal squad", "best squad", "lineup", *chip_terms)
        transfers = dashboard.get("transfers") or {}
        if transfers.get("decision_safety") == "refresh_required" and (scenario is not None or any(term in normalized for term in decision_terms)):
            result = {
                "intent": "verification_required",
                "headline": "Verify your team before deciding",
                "answer": "I can explain projections, but I cannot recommend a move or chip until your current squad, bank, free transfers and selling prices are confirmed and official data is fresh. Open My team to verify them.",
                "evidence": [],
                "sources": ["Manager context", "Official FPL data"],
                "caveats": transfers.get("data_warnings") or [],
                "follow_up_questions": ["How reliable are these projections?", "What are my biggest squad risks?"],
            }
        elif scenario is not None:
            result = self._scenario(scenario, dashboard)
        elif "roll" in normalized and any(term in normalized for term in ("transfer", "move", "hit", "wait")):
            result = self._transfer_routes(dashboard)
        elif self.chip_planner is not None and any(term in normalized for term in chip_terms):
            result = self._chips(normalized)
        elif normalized.startswith("compare ") and re.search(r"\s+(?:vs|versus|or|and)\s+", normalized):
            left, right = re.split(r"\s+(?:vs\.?|versus|or|and)\s+", question[8:], maxsplit=1, flags=re.IGNORECASE)
            left_player, right_player = self._match_player(left), self._match_player(right)
            result = self._compare_players(left_player, right_player, dashboard) if left_player and right_player else self._unavailable(
                "comparison", "I could not uniquely identify both players. Use the two player selectors below for an exact comparison."
            )
        elif "what if" in normalized and "captain" in normalized:
            player = self._match_player(question)
            result = self._captain_scenario(player, dashboard) if player else self._unavailable(
                "captain_scenario", "Choose a current starting player in the scenario tool to test a captain change."
            )
        elif "why not" in normalized:
            result = self._why_not(question, dashboard)
        elif any(term in normalized for term in ("transfer", "sell", "buy", "move", "do nothing", "hold")):
            result = self._transfer(dashboard)
        elif any(term in normalized for term in ("captain", "armband", "vice captain")):
            result = self._captain(dashboard)
        elif any(term in normalized for term in ("rebuild", "optimal squad", "best squad", "formation", "lineup")):
            result = self._squad(dashboard)
        elif any(term in normalized for term in ("reliable", "reliability", "confidence", "calibration", "backtest", "trust")):
            result = self._reliability(dashboard)
        elif any(term in normalized for term in ("risk", "injury", "injured", "rotation", "doubt", "alert")):
            result = self._risks(dashboard)
        elif any(term in normalized for term in ("fixture", "schedule", "opponent", "run")):
            result = self._fixtures(dashboard)
        elif any(term in normalized for term in ("budget", "bank", "free transfer", "hit", "money")):
            result = self._budget(dashboard)
        else:
            player = self._match_player(question)
            result = self._player(player, dashboard) if player else self._unsupported()

        freshness = dashboard.get("freshness") or {}
        caveats = list(result.get("caveats") or [])
        if freshness.get("used_stale_data"):
            caveats.append("The latest refresh used cached source data; refresh official data before acting.")
        for warning in (dashboard.get("transfers") or {}).get("data_warnings") or []:
            if warning.startswith("Official FPL data is over") and warning not in caveats:
                caveats.append(warning)
        result["caveats"] = caveats
        result.update(
            {
                "status": "ready",
                "question": question,
                "mode": "deterministic-grounded",
                "context": {
                    "planning_event": self._event_id(dashboard.get("planning_event")),
                    "horizon": int(filters["horizon"]),
                    "risk": filters["risk"],
                    "generated_at": dashboard.get("generated_at"),
                    "decision_threshold": (dashboard.get("transfers") or {}).get("decision_threshold"),
                },
                "follow_up_questions": result.get("follow_up_questions") or self.STARTERS[:3],
            }
        )
        return result

    def _scenario(self, scenario: dict[str, Any], dashboard: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(scenario, dict):
            raise ValueError("scenario must be an object")
        kind = scenario.get("type")
        if kind == "compare_players":
            left = self._projection(self._scenario_id(scenario, "left_id"), 6)
            right = self._projection(self._scenario_id(scenario, "right_id"), 6)
            return self._compare_players(left, right, dashboard)
        if kind == "transfer_swap":
            return self._transfer_swap(self._scenario_id(scenario, "sell_id"), self._scenario_id(scenario, "buy_id"), dashboard)
        if kind == "transfer_routes":
            return self._transfer_routes(dashboard)
        if kind == "captain_swap":
            player = self._projection(self._scenario_id(scenario, "player_id"), 1)
            return self._captain_scenario(player, dashboard)
        raise ValueError("scenario type must be compare_players, transfer_swap, transfer_routes, or captain_swap")

    @staticmethod
    def _scenario_id(scenario: dict[str, Any], key: str) -> int:
        try:
            player_id = int(scenario[key])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be a player ID") from exc
        if player_id < 1:
            raise ValueError(f"{key} must be a positive player ID")
        return player_id

    def _compare_players(self, left: dict[str, Any], right: dict[str, Any], dashboard: dict[str, Any]) -> dict[str, Any]:
        if int(left["id"]) == int(right["id"]):
            raise ValueError("choose two different players")
        horizon = int(dashboard["filters"]["horizon"])
        first, second = self._projection(int(left["id"]), horizon), self._projection(int(right["id"]), horizon)
        payloads = self.repository.current_projection_payloads()
        first_points, second_points = float(first["expected_points"]), float(second["expected_points"])
        leader = first["web_name"] if first_points > second_points else second["web_name"] if second_points > first_points else "Neither player"
        return {
            "intent": "comparison",
            "headline": f"{first['web_name']} vs {second['web_name']}",
            "answer": f"{leader} has the higher {horizon}-GW projection by {abs(first_points - second_points):.1f} points. This compares individual forecasts; a transfer also needs squad, price and FPL rule checks.",
            "table": {
                "headers": ["Measure", first["web_name"], second["web_name"]],
                "rows": [
                    [f"{horizon}-GW xPts", f"{first_points:.1f}", f"{second_points:.1f}"],
                    ["Expected minutes / fixture", f"{float(first.get('expected_minutes') or 0):.0f}", f"{float(second.get('expected_minutes') or 0):.0f}"],
                    ["No-play risk", f"{float(first.get('no_play_probability') or 0) * 100:.0f}%", f"{float(second.get('no_play_probability') or 0) * 100:.0f}%"],
                    ["Fixture score", f"{self._fixture_score(payloads.get(int(first['id']), {}), horizon):.1f}", f"{self._fixture_score(payloads.get(int(second['id']), {}), horizon):.1f}"],
                    ["Price", f"£{float(first['price']):.1f}m", f"£{float(second['price']):.1f}m"],
                ],
            },
            "evidence": [self._evidence("Projection gap", f"{abs(first_points - second_points):.1f} pts", "Projection model")],
            "sources": ["Projection model", "Minutes model", "Official FPL prices and fixtures"],
            "caveats": ["This head-to-head forecast is not a team-specific transfer recommendation."],
            "follow_up_questions": ["Should I roll, make one move, or take a hit?", "What transfer should I make and why?"],
        }

    def _transfer_routes(self, dashboard: dict[str, Any]) -> dict[str, Any]:
        transfers = dashboard.get("transfers") or {}
        routes = transfers.get("route_comparison") or {}
        if transfers.get("status") != "ready" or not routes:
            return self._unavailable("transfer_routes", "Connect a complete squad and generate projections to compare transfer routes.")
        roll, single, double = (routes.get(name) or {} for name in ("roll", "single", "double"))
        best_single = next((item for item in transfers.get("suggestions", []) if item.get("action") == "transfer"), None)
        single_label = f"{best_single['sell']['name']} → {best_single['buy']['name']}" if best_single else "No positive single move"
        pair = transfers.get("two_transfer_plan") or {}
        pair_label = " + ".join(f"{move['sell']['name']} → {move['buy']['name']}" for move in pair.get("moves", [])) or "No verified pair"
        double_gain = double.get("net_expected_gain")
        has_single = best_single is not None
        has_double = double_gain is not None and bool(pair.get("moves"))
        warnings = transfers.get("data_warnings") or []
        return {
            "intent": "transfer_routes",
            "headline": "Roll, single move, or two moves",
            "answer": f"The current decision is {dashboard['decision'].get('title', 'unavailable')}. Single and pair values are estimated squad-player gains over {dashboard['filters']['horizon']} GWs after hits. Each row is an alternative plan.",
            "table": {
                "headers": ["Route", "Move", "Net xPts", "Hit", "FT next GW"],
                "rows": [
                    ["Roll", "Keep squad", "0.0", "0", str(roll.get("next_week_free_transfers", "—"))],
                    ["One move", single_label, f"{float(single.get('net_expected_gain') or 0):+.1f}" if has_single else "Unavailable", str(transfers.get("transfer_cost", 0)) if has_single else "—", str(single.get("next_week_free_transfers", "—")) if has_single else "—"],
                    ["Two moves", pair_label, f"{float(double_gain):+.1f}" if has_double else "Unavailable", str(double.get("hit_cost", "—")) if has_double else "—", str(double.get("next_week_free_transfers", "—")) if has_double else "—"],
                ],
            },
            "evidence": [
                self._evidence("Free transfers now", str(transfers.get("free_transfers", "—")), "Imported manager context"),
                self._evidence("Action threshold", f"{float(transfers.get('decision_threshold') or 0):.1f} pts", "Transfer optimizer"),
            ],
            "sources": ["Transfer optimizer", "FPL transfer rules", "Imported squad"],
            "caveats": [*warnings, routes.get("note") or "", "The best pair is found from a bounded shortlist, not an exhaustive season plan."],
            "follow_up_questions": ["What transfer should I make and why?", "How reliable are these projections?"],
        }

    def _transfer_swap(self, sell_id: int, buy_id: int, dashboard: dict[str, Any]) -> dict[str, Any]:
        transfers = dashboard.get("transfers") or {}
        squad = self.repository.squad()
        if transfers.get("status") != "ready" or len(squad) != 15:
            return self._unavailable("transfer_scenario", "Connect your complete 15-player squad before testing a transfer.")
        owned = {int(player["id"]): player for player in squad}
        if sell_id not in owned or buy_id in owned:
            raise ValueError("select one player in your squad to sell and one player outside it to buy")
        horizon = int(dashboard["filters"]["horizon"])
        pool = {int(player["id"]): player for player in self.repository.projections(horizon=horizon, limit=1000)}
        if sell_id not in pool or buy_id not in pool:
            return self._unavailable("transfer_scenario", "A current projection is missing for one of the selected players.")
        if not set(owned).issubset(pool):
            return self._unavailable("transfer_scenario", "One or more current squad players lack a projection, so the club-limit check is unavailable.")
        sell, buy = pool[sell_id], pool[buy_id]
        if int(sell["position_id"]) != int(buy["position_id"]):
            raise ValueError("FPL transfers must replace a player in the same position")
        club_count = sum(int(pool[int(player_id)]["team_id"]) == int(buy["team_id"]) for player_id in owned if player_id != sell_id)
        if club_count >= 3:
            raise ValueError("that transfer would exceed the three-players-per-club limit")
        if buy.get("status") in {"i", "s", "u", "n"} or (buy.get("chance_next") is not None and int(buy["chance_next"]) < 75):
            raise ValueError("the selected buyer is currently unavailable or has a major availability flag")
        raw_price = owned[sell_id].get("selling_price")
        sell_price = float(raw_price) / 10.0 if raw_price is not None else float(owned[sell_id].get("current_price") or sell["price"])
        available = sell_price + float(dashboard.get("manager", {}).get("bank") or 0)
        if float(buy["price"]) > available + 1e-9:
            raise ValueError(f"{buy['web_name']} costs £{float(buy['price']):.1f}m; this move has only £{available:.1f}m available")
        hit = float(transfers.get("transfer_cost") or 0)
        opportunity = float(transfers.get("opportunity_cost") or 0)
        windows = sorted({1, 3, 6, horizon})
        rows = []
        selected_net = 0.0
        for window in windows:
            player_pool = {int(player["id"]): player for player in self.repository.projections(horizon=window, limit=1000)}
            if sell_id not in player_pool or buy_id not in player_pool:
                continue
            gross = float(player_pool[buy_id]["expected_points"]) - float(player_pool[sell_id]["expected_points"])
            net = gross - hit - opportunity
            rows.append([f"{window} GW", f"{gross:+.1f}", f"-{hit:.0f}" if hit else "0", f"-{opportunity:.1f}" if opportunity else "0.0", f"{net:+.1f}"])
            if window == horizon:
                selected_net = net
        threshold = float(transfers.get("decision_threshold") or 0)
        warnings = transfers.get("data_warnings") or []
        action = "clears" if selected_net >= threshold and selected_net > 0 else "does not clear"
        answer = f"{sell['web_name']} → {buy['web_name']} projects {selected_net:+.1f} net points over {horizon} GWs after the {hit:.0f}-point hit and {opportunity:.1f}-point transfer opportunity cost. It {action} the current {threshold:.1f}-point action threshold."
        if warnings:
            answer += " This is a preview only: the official data or exact selling price needs confirmation before acting."
        return {
            "intent": "transfer_scenario",
            "headline": f"{sell['web_name']} → {buy['web_name']}",
            "answer": answer,
            "table": {"headers": ["Window", "Gross xPts", "Hit", "FT cost", "Net xPts"], "rows": rows},
            "evidence": [
                self._evidence("Bank after", f"£{available - float(buy['price']):.1f}m", "Imported bank and selling price"),
                self._evidence("Action threshold", f"{threshold:.1f} pts", "Transfer optimizer"),
                self._evidence("Free transfers", str(transfers.get("free_transfers", "—")), "Imported manager context"),
            ],
            "sources": ["Projection model", "Transfer optimizer costs", "Imported squad", "Official FPL prices"],
            "caveats": [*warnings, *(["Selling price is estimated from current price; confirm affordability in the official app."] if raw_price is None else []), "This single swap preview does not recompute your best XI, captain, or later transfers."],
            "follow_up_questions": ["Should I roll, make one move, or take a hit?", "What transfer should I make and why?"],
        }

    def _captain_scenario(self, player: dict[str, Any] | None, dashboard: dict[str, Any]) -> dict[str, Any]:
        starters = [item for item in dashboard.get("current_squad", {}).get("ranking", []) if item.get("starter")]
        if player is None or len(starters) < 11:
            return self._unavailable("captain_scenario", "Connect a complete starting XI and choose one of its players to compare captaincy.")
        starter_ids = {int(item["id"]) for item in starters}
        if int(player["id"]) not in starter_ids:
            raise ValueError("captain must be in your current starting XI")
        rows = [self._projection(player_id, 1) for player_id in starter_ids]
        best = max(rows, key=lambda row: float(row.get("expected_points") or 0))
        current_name = dashboard.get("current_squad", {}).get("captain")
        captain_pick = next((pick for pick in self.repository.squad() if pick.get("is_captain")), None)
        current = next((row for row in rows if int(row["id"]) == int(captain_pick["id"])), None) if captain_pick else None
        current = current or next((row for row in rows if row["web_name"] == current_name), best)
        target = next(row for row in rows if int(row["id"]) == int(player["id"]))
        gap = float(target.get("expected_points") or 0) - float(current.get("expected_points") or 0)
        comparison = f"{abs(gap):.1f} {'fewer' if gap < 0 else 'more'} than" if gap else "the same as"
        return {
            "intent": "captain_scenario",
            "headline": f"Captain {target['web_name']}?",
            "answer": f"Captain {target['web_name']} projects an extra {float(target['expected_points']):.1f} xPts from the captaincy multiplier in the next GW. That is {comparison} your current captain, {current['web_name']}. The model's top starter is {best['web_name']}. Captaincy doubles that player's points; this shows only the extra captain copy, not FPL bonus points.",
            "evidence": [
                self._evidence("Target xPts · 1 GW", f"{float(target['expected_points']):.1f}", "Projection model"),
                self._evidence("Current captain xPts", f"{float(current['expected_points']):.1f}", "Imported starting XI"),
                self._evidence("Top starter xPts", f"{float(best['expected_points']):.1f}", "Current starting XI"),
                self._evidence("Captain bonus gap", f"{gap:+.1f} pts", "Projection model"),
            ],
            "sources": ["Current starting XI", "One-GW projection model", "FPL captaincy rules"],
            "caveats": ["Expected points are uncertain; vice-captain substitution and lineup changes are not simulated here."],
            "follow_up_questions": ["Who should I captain?", "What are my biggest squad risks?"],
        }

    def _chips(self, normalized_question: str) -> dict[str, Any]:
        event_match = re.search(r"\bgw\s*(\d{1,2})\b|\bgameweek\s*(\d{1,2})\b", normalized_question)
        event = int(next((value for value in event_match.groups() if value), 0)) if event_match else None
        aliases = {
            "bench boost": "bench_boost",
            "triple captain": "triple_captain",
            "free hit": "free_hit",
            "wildcard": "wildcard",
        }
        chip = next((value for label, value in aliases.items() if label in normalized_question), None)
        report = self.chip_planner.plan(forced_chip=chip, forced_event=event)
        if report.get("status") != "ready":
            return self._unavailable("chips", report.get("message") or "The chip plan is unavailable.")
        if chip:
            card = next(item for item in report["cards"] if item["key"] == chip)
            detail = report[chip]
            opportunity = next(
                (item for item in detail.get("opportunities", []) if event and int(item["event"]) == event),
                (detail.get("opportunities") or [{}])[0],
            )
            target_event = int(opportunity.get("event") or card.get("best_event") or report["planning_event"])
            gain = float(opportunity.get("gain") or 0)
            answer = (
                f"{card['recommendation']} {card['name']}. GW{target_event} projects {gain:+.1f} incremental points. "
                f"{card['reason']} Alternative: {card['alternative']}"
            )
            evidence = [
                self._evidence("Recommendation", card["recommendation"], "Chip planner"),
                self._evidence("Best Gameweek", f"GW{card['best_event']}", "Eight-GW simulation"),
                self._evidence("Expected gain", f"{card['expected_gain']:+.1f} pts", "Chip-specific optimizer"),
                self._evidence("Model signal", f"{card['confidence'] * 100:.0f}/100", "Heuristic, not success probability"),
            ]
            if chip == "triple_captain" and opportunity.get("candidate"):
                evidence.append(self._evidence("Candidate", opportunity["candidate"]["name"], "Current squad captain ranking"))
            return {
                "intent": "chips",
                "headline": f"{card['name']} · {card['recommendation']}",
                "answer": answer,
                "evidence": evidence,
                "sources": ["AI Chip Planner", "Projection model", "Imported squad", "Official fixtures"],
                "caveats": [*(report.get("data_warnings") or []), *report["data_boundaries"][:2]],
                "follow_up_questions": ["Should I use a chip this Gameweek?", "What is my best chip sequence?"],
            }
        summary = report["summary"]
        strategy = report["season_strategy"]
        return {
            "intent": "chips",
            "headline": summary["headline"],
            "answer": f"{summary['explanation']} The preferred eight-Gameweek sequence is {strategy['recommended']}.",
            "evidence": [
                self._evidence("Strongest chip", summary.get("strongest_name") or "None", "Chip planner"),
                self._evidence("Best Gameweek", f"GW{summary['best_event']}" if summary.get("best_event") else "—", "Eight-GW simulation"),
                self._evidence("Expected gain", f"{float(summary.get('expected_gain') or 0):+.1f} pts", "Chip-specific optimizer"),
                self._evidence("Strategy edge", f"{float(strategy.get('projected_advantage') or 0):+.1f} pts", "Sequence comparison"),
            ],
            "sources": ["AI Chip Planner", "Projection model", "Imported squad", "Official fixtures"],
            "caveats": report["data_boundaries"],
            "follow_up_questions": ["When should I Bench Boost?", "Should I Free Hit this week?", "Compare Wildcard gameweeks."],
        }

    def _transfer(self, dashboard: dict[str, Any]) -> dict[str, Any]:
        decision = dashboard["decision"]
        transfers = dashboard.get("transfers") or {}
        suggestions = transfers.get("suggestions") or []
        best = suggestions[0] if suggestions else {}
        horizon = int(dashboard["filters"]["horizon"])
        if decision.get("action") == "team_required":
            return {
                "intent": "transfer",
                "headline": "Connect your 15-player squad first",
                "answer": decision["summary"],
                "evidence": [],
                "sources": ["Imported FPL squad"],
                "caveats": ["A team-specific recommendation cannot be calculated without all 15 players."],
            }
        if decision.get("action") == "hold":
            threshold = transfers.get("decision_threshold")
            threshold_label = f"{float(threshold):.1f} pts" if threshold is not None else "Unavailable"
            threshold_components = transfers.get("threshold_components") or {}
            margin_evidence = [
                self._evidence("Base edge", f"{float(threshold_components.get('base') or 0):.1f} pts", "Transfer optimizer"),
                self._evidence("Hit risk margin", f"+{float(threshold_components.get('hit_uncertainty_margin') or 0):.1f} pts", "Transfer optimizer"),
                self._evidence("Calibration margin", f"+{float(threshold_components.get('uncalibrated_margin') or 0):.1f} pts", "Rolling calibration"),
            ] if threshold_components else []
            return {
                "intent": "transfer",
                "headline": "Do Nothing is the best action",
                "answer": decision["summary"],
                "evidence": [
                    self._evidence("Action threshold", threshold_label, "Transfer optimizer"),
                    *margin_evidence,
                    self._evidence("Free transfers", str(transfers.get("free_transfers", "—")), "Imported manager context"),
                    self._evidence("Transfer cost", f"-{float(transfers.get('transfer_cost') or 0):.0f} pts", "FPL transfer rules"),
                ],
                "sources": ["Transfer optimizer", "Imported manager context", "Rolling calibration"],
                "caveats": ["Expected points are forecasts, not guaranteed returns."],
                "follow_up_questions": ["What are my biggest squad risks?", "How reliable are these projections?"],
            }

        sell, buy = best["sell"], best["buy"]
        gain = float(best.get("net_expected_gain") or 0)
        answer = (
            f"Sell {sell['name']} and buy {buy['name']}. The optimizer projects "
            f"{gain:+.1f} net points over {horizon} gameweeks after a "
            f"{float(best.get('hit_cost') or 0):.0f}-point hit and "
            f"{float(best.get('opportunity_cost') or 0):.1f}-point opportunity cost. "
            f"{best.get('why', '')}"
        )
        return {
            "intent": "transfer",
            "headline": f"{sell['name']} → {buy['name']}",
            "answer": answer,
            "evidence": [
                self._evidence(f"Net gain · {horizon} GW", f"{gain:+.1f} pts", "Transfer optimizer"),
                self._evidence("3-GW gain", f"{float(best.get('gain_3') or 0):+.1f} pts", "Projection model"),
                self._evidence("6-GW gain", f"{float(best.get('gain_6') or 0):+.1f} pts", "Projection model"),
                self._evidence("Fixture swing", f"{float(best.get('fixture_swing') or 0):+.1f}", "Position-aware fixture model"),
                self._evidence("Bank after", f"£{float(best.get('bank_after') or 0):.1f}m", "Budget constraint"),
                self._evidence("Model signal", f"{float(best.get('confidence') or 0) * 100:.0f}/100", "Heuristic, not success probability"),
            ],
            "sources": ["Transfer optimizer", "Projection model", "Official FPL squad and prices", "Rolling calibration"],
            "caveats": ["Expected points are forecasts, not guaranteed returns."],
            "follow_up_questions": [f"Why not another {best.get('position', 'player')}?", "How reliable are these projections?", "What are my biggest squad risks?"],
        }

    def _why_not(self, question: str, dashboard: dict[str, Any]) -> dict[str, Any]:
        remainder = re.split(r"why\s+not", question, flags=re.IGNORECASE, maxsplit=1)[-1]
        challenger = self._match_player(remainder)
        if challenger is None:
            return {
                "intent": "why_not",
                "headline": "Name the player you want compared",
                "answer": "I can compare the recommended target with a specific player, but I could not match one in the installed player database. Try “Why not Palmer?”.",
                "evidence": [],
                "sources": ["Installed FPL player database"],
                "caveats": [],
                "follow_up_questions": ["What transfer should I make and why?"],
            }
        suggestions = dashboard.get("transfers", {}).get("suggestions") or []
        best = next((item for item in suggestions if item.get("action") == "transfer"), None)
        if best is None:
            return {
                "intent": "why_not",
                "headline": f"No active transfer target to compare with {challenger['web_name']}",
                "answer": "The optimizer currently prefers holding, so there is no recommended buyer for a direct target comparison.",
                "evidence": [],
                "sources": ["Transfer optimizer"],
                "caveats": ["Ask about the hold decision to see its threshold and costs."],
            }
        horizon = int(dashboard["filters"]["horizon"])
        recommended = self._projection(int(best["buy"]["id"]), horizon)
        comparison = self._comparison(recommended, challenger, best, dashboard, horizon)
        return comparison

    def _comparison(
        self,
        recommended: dict[str, Any],
        challenger: dict[str, Any],
        best: dict[str, Any],
        dashboard: dict[str, Any],
        horizon: int,
    ) -> dict[str, Any]:
        challenger = self._projection(int(challenger["id"]), horizon)
        recommended_long = self._projection(int(recommended["id"]), 6)
        challenger_long = self._projection(int(challenger["id"]), 6)
        payloads = self.repository.current_projection_payloads()
        recommended_payload = payloads.get(int(recommended["id"]), {})
        challenger_payload = payloads.get(int(challenger["id"]), {})
        difference = float(recommended.get("expected_points") or 0) - float(challenger.get("expected_points") or 0)
        available = float(best["sell"]["price"]) + float(dashboard["manager"].get("bank") or 0)
        same_position = int(recommended["position_id"]) == int(challenger["position_id"])
        affordable = float(challenger["price"]) <= available + 1e-9
        legal_note = "a legal direct alternative"
        if not same_position:
            legal_note = f"not a direct alternative because {challenger['web_name']} is {challenger['position']} and the recommended slot is {recommended['position']}"
        elif not affordable:
            legal_note = f"not affordable for the £{available:.1f}m available in this move"
        verdict = "projects higher" if difference >= 0 else "projects lower"
        answer = (
            f"{recommended['web_name']} {verdict} than {challenger['web_name']} by {abs(difference):.1f} points over "
            f"{horizon} gameweeks. {challenger['web_name']} is {legal_note}. The comparison uses the same projection "
            "window and risk context; the optimizer also applies squad legality, transfer cost and opportunity cost."
        )
        return {
            "intent": "why_not",
            "headline": f"{recommended['web_name']} vs {challenger['web_name']}",
            "answer": answer,
            "evidence": [
                self._evidence(f"{horizon}-GW xPts", f"{float(recommended.get('expected_points') or 0):.1f} vs {float(challenger.get('expected_points') or 0):.1f}", "Projection model"),
                self._evidence("Expected minutes / fixture", f"{float(recommended.get('expected_minutes') or 0):.0f} vs {float(challenger.get('expected_minutes') or 0):.0f}", "Minutes model"),
                self._evidence("Fixture score", f"{self._fixture_score(recommended_payload, horizon):.1f} vs {self._fixture_score(challenger_payload, horizon):.1f}", "Position-aware fixture model"),
                self._evidence("Price", f"£{float(recommended['price']):.1f}m vs £{float(challenger['price']):.1f}m", "Official FPL prices"),
                self._evidence("Value", f"{float(recommended.get('value') or 0):.2f} vs {float(challenger.get('value') or 0):.2f}", "Projected points per £1m"),
                self._evidence("No-play risk", f"{float(recommended.get('no_play_probability') or 0) * 100:.0f}% vs {float(challenger.get('no_play_probability') or 0) * 100:.0f}%", "Minutes model"),
                self._evidence("6-GW ceiling", f"{float(recommended.get('ceiling_6') or 0):.1f} vs {float(challenger.get('ceiling_6') or 0):.1f}", "Projection intervals"),
                self._evidence("Long-term xPts · 6 GW", f"{float(recommended_long.get('expected_points') or 0):.1f} vs {float(challenger_long.get('expected_points') or 0):.1f}", "Projection model"),
            ],
            "sources": ["Transfer optimizer", "Projection model", "Minutes model", "Official FPL prices"],
            "caveats": ["A head-to-head projection does not override FPL position, budget or club-limit constraints."],
            "follow_up_questions": ["What transfer should I make and why?", "Who should I captain?"],
        }

    def _captain(self, dashboard: dict[str, Any]) -> dict[str, Any]:
        starters = [item for item in dashboard.get("current_squad", {}).get("ranking", []) if item.get("starter")]
        if len(starters) < 2:
            return self._unavailable("captaincy", "Captaincy is unavailable until a complete current starting XI and projections are ready.")
        one_week = sorted(
            (self._projection(int(player["id"]), 1) for player in starters),
            key=lambda player: (float(player.get("expected_points") or 0), float(player.get("confidence") or 0)),
            reverse=True,
        )
        captain_row, vice_row = one_week[:2]
        horizon = int(dashboard["filters"]["horizon"])
        captain_long = self._projection(int(captain_row["id"]), horizon)
        vice_long = self._projection(int(vice_row["id"]), horizon)
        return {
            "intent": "captain",
            "headline": f"Captain {captain_row['web_name']}",
            "answer": f"{captain_row['web_name']} has the highest 1-gameweek expected points among your current starters, with {vice_row['web_name']} second. This captaincy answer uses your imported XI—not the wildcard rebuild.",
            "evidence": [
                self._evidence("Captain · 1-GW xPts", f"{float(captain_row.get('expected_points') or 0):.1f}", "Projection model"),
                self._evidence("Vice · 1-GW xPts", f"{float(vice_row.get('expected_points') or 0):.1f}", "Projection model"),
                self._evidence(f"Captain · {horizon}-GW xPts", f"{float(captain_long.get('expected_points') or 0):.1f}", "Projection model"),
                self._evidence(f"Vice · {horizon}-GW xPts", f"{float(vice_long.get('expected_points') or 0):.1f}", "Projection model"),
            ],
            "sources": ["Imported current XI", "Projection model", "Minutes model"],
            "caveats": ["Captaincy is ranked on expected points; last-minute availability news can still change the decision."],
        }

    def _squad(self, dashboard: dict[str, Any]) -> dict[str, Any]:
        optimized = dashboard.get("optimized_squad") or {}
        if optimized.get("status") != "ready":
            return self._unavailable("squad", "The optimized rebuild is unavailable until projections are ready.")
        comparison = optimized.get("comparison") or {}
        changes = comparison.get("changes")
        answer = f"The constrained optimizer selects a {optimized.get('formation')} formation with {float(optimized.get('total_expected_points') or 0):.1f} captain-adjusted expected points over {dashboard['filters']['horizon']} gameweeks."
        if comparison.get("has_imported_squad"):
            answer += f" It keeps {comparison.get('players_kept')} of your current players and changes {changes}."
        return {
            "intent": "squad",
            "headline": f"Optimized {optimized.get('formation')} rebuild",
            "answer": answer,
            "evidence": [
                self._evidence("Captain-adjusted xPts", f"{float(optimized.get('total_expected_points') or 0):.1f}", "Squad optimizer"),
                self._evidence("Squad cost", f"£{float(optimized.get('squad_cost') or 0):.1f}m", "Budget constraint"),
                self._evidence("Bank remaining", f"£{float(optimized.get('bank_remaining') or 0):.1f}m", "Budget constraint"),
                self._evidence("Captain", str((optimized.get('captain') or {}).get('name') or '—'), "Squad optimizer"),
            ],
            "sources": ["Squad optimizer", "Projection model", "Official FPL squad constraints"],
            "caveats": ["Use this as a wildcard or free-hit lens; it is not a recommendation to take multiple transfer hits."],
        }

    def _reliability(self, dashboard: dict[str, Any]) -> dict[str, Any]:
        reliability = dashboard.get("reliability") or {}
        status = reliability.get("status") or "collecting"
        score = reliability.get("reliability_score")
        if status != "calibrated" or score is None:
            answer = "Rolling historical backtesting is collecting completed gameweeks. Until the sample is sufficient, recommendation confidence is deliberately capped at 65%."
        else:
            answer = f"The rolling backtest currently reports a {float(score) * 100:.0f}% reliability score across {reliability.get('evaluated_events', 0)} evaluated gameweeks and {reliability.get('sample_size', 0)} player observations."
        evidence = [
            self._evidence("Calibration status", str(status).title(), "Rolling backtest"),
            self._evidence("Evaluated GWs", str(reliability.get("evaluated_events") or 0), "Rolling backtest"),
            self._evidence("Sample size", str(reliability.get("sample_size") or 0), "Rolling backtest"),
        ]
        if score is not None:
            evidence.append(self._evidence("Reliability score", f"{float(score) * 100:.0f}%", "Rolling calibration"))
        if reliability.get("mae") is not None:
            evidence.append(self._evidence("Mean absolute error", f"{float(reliability['mae']):.2f} pts", "Rolling backtest"))
        return {
            "intent": "reliability",
            "headline": "Calibration is active" if status == "calibrated" else "Reliability is still collecting",
            "answer": answer,
            "evidence": evidence,
            "sources": ["Pre-deadline projection snapshots", "Completed FPL gameweeks", "Rolling calibration"],
            "caveats": ["A confidence score measures model evidence; it does not guarantee an individual outcome."],
        }

    def _risks(self, dashboard: dict[str, Any]) -> dict[str, Any]:
        alerts = dashboard.get("current_squad", {}).get("alerts") or []
        if not alerts:
            answer = "No material availability, rotation or transfer alerts are present in the current squad snapshot."
        else:
            answer = " The model's highest-priority alerts are: " + " ".join(
                f"{item['player']}: {item['message']}" for item in alerts[:3]
            )
        return {
            "intent": "risks",
            "headline": f"{len(alerts)} squad alert{'s' if len(alerts) != 1 else ''}",
            "answer": answer.strip(),
            "evidence": [self._evidence(item["player"], item["message"], "Squad risk scan") for item in alerts[:5]],
            "sources": ["Official FPL availability news", "Minutes model", "Transfer optimizer"],
            "caveats": ["Late team news can change after the latest official-data refresh."],
        }

    def _fixtures(self, dashboard: dict[str, Any]) -> dict[str, Any]:
        outlook = dashboard.get("current_squad", {}).get("fixture_outlook") or {}
        best = outlook.get("best") or []
        difficult = outlook.get("difficult") or []
        answer = "Fixture scores are position-aware. "
        answer += f"Best current-squad run: {best[0]['name']} ({best[0]['label']})." if best else "No squad fixture path is available."
        if difficult:
            answer += f" Most difficult: {difficult[0]['name']} ({difficult[0]['label']})."
        return {
            "intent": "fixtures",
            "headline": "Your squad's next-three-GW fixture shape",
            "answer": answer,
            "evidence": [self._evidence(item["name"], f"{item['label']} · score {float(item['score']):.1f}", "Position-aware fixture model") for item in best[:3]],
            "sources": ["Official FPL fixtures", "Position-aware fixture model"],
            "caveats": ["Fixture quality is only one component of expected points."],
        }

    def _budget(self, dashboard: dict[str, Any]) -> dict[str, Any]:
        manager = dashboard.get("manager") or {}
        transfers = dashboard.get("transfers") or {}
        return {
            "intent": "budget",
            "headline": f"£{float(manager.get('bank') or 0):.1f}m in the bank",
            "answer": f"You have {manager.get('free_transfers', 0)} free transfers. The current optimizer applies a {float(transfers.get('transfer_cost') or 0):.0f}-point transfer cost and a {float(transfers.get('opportunity_cost') or 0):.1f}-point opportunity cost to candidate moves.",
            "evidence": [
                self._evidence("Bank", f"£{float(manager.get('bank') or 0):.1f}m", "Imported manager context"),
                self._evidence("Free transfers", str(manager.get("free_transfers") or 0), "Imported manager context"),
                self._evidence("Transfer cost", f"-{float(transfers.get('transfer_cost') or 0):.0f} pts", "Transfer optimizer"),
            ],
            "sources": ["Imported manager context", "Transfer optimizer"],
            "caveats": ["Exact affordability uses imported selling prices where the public endpoint provides them."],
        }

    def _player(self, player: dict[str, Any], dashboard: dict[str, Any]) -> dict[str, Any]:
        horizon = int(dashboard["filters"]["horizon"])
        row = self._projection(int(player["id"]), horizon)
        payload = self.repository.current_projection_payloads().get(int(player["id"]), {})
        return {
            "intent": "player",
            "headline": f"{row['web_name']} projection",
            "answer": f"{row['web_name']} projects {float(row.get('expected_points') or 0):.1f} points over {horizon} gameweeks, with {float(row.get('expected_minutes') or 0):.0f} expected minutes per fixture and {float(row.get('no_play_probability') or 0) * 100:.0f}% no-play risk per fixture.",
            "evidence": [
                self._evidence(f"{horizon}-GW xPts", f"{float(row.get('expected_points') or 0):.1f}", "Projection model"),
                self._evidence("Expected minutes / fixture", f"{float(row.get('expected_minutes') or 0):.0f}", "Minutes model"),
                self._evidence("Price", f"£{float(row.get('price') or 0):.1f}m", "Official FPL data"),
                self._evidence("Value", f"{float(row.get('value') or 0):.2f} pts/£m", "Projection model"),
                self._evidence("Fixture score", f"{self._fixture_score(payload, horizon):.1f}", "Position-aware fixture model"),
                self._evidence("Confidence", f"{float(row.get('confidence') or 0) * 100:.0f}%", "Projection model"),
            ],
            "sources": ["Projection model", "Minutes model", "Official FPL player data"],
            "caveats": ["Player projections do not by themselves establish that a transfer is legal or optimal for your squad."],
        }

    def _projection(self, player_id: int, horizon: int) -> dict[str, Any]:
        for player in self.repository.projections(horizon=horizon, limit=1000):
            if int(player["id"]) == player_id:
                return player
        raise ValueError("No current projection is available for that player")

    def _match_player(self, value: str) -> dict[str, Any] | None:
        needle = self._normalise(value)
        if not needle:
            return None
        players = self.repository.projections(horizon=6, limit=1000)
        exact = [player for player in players if self._normalise(player["web_name"]) == needle]
        if exact:
            return exact[0]
        contained = [
            player for player in players
            if self._normalise(player["web_name"]) in needle or needle in self._normalise(player["web_name"])
        ]
        return contained[0] if len(contained) == 1 else None

    @staticmethod
    def _unavailable(intent: str, answer: str) -> dict[str, Any]:
        return {
            "intent": intent,
            "headline": "Calculation unavailable",
            "answer": answer,
            "evidence": [],
            "sources": ["FPL AI decision engine"],
            "caveats": [],
        }

    def _unsupported(self) -> dict[str, Any]:
        return {
            "intent": "unsupported",
            "headline": "That is outside my calculated FPL context",
            "answer": "I only explain installed player projections, your imported squad, transfer and squad optimizer results, fixtures, risks, budget, and rolling calibration. I will not invent an answer outside those outputs.",
            "evidence": [],
            "sources": [],
            "caveats": [],
            "follow_up_questions": self.STARTERS[:4],
        }
