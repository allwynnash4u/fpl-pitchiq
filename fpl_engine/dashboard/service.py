from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fpl_engine.data.repository import Repository
from fpl_engine.optimizer.squad import SquadOptimizer
from fpl_engine.optimizer.transfer import TransferOptimizer


ALLOWED_HORIZONS = {1, 3, 5, 6, 8}
ALLOWED_RISKS = {"conservative", "balanced", "aggressive"}


class DashboardService:
    """Compose one source-backed decision object for the Phase 5 dashboard."""

    def __init__(
        self,
        repository: Repository,
        transfer_optimizer: TransferOptimizer,
        squad_optimizer: SquadOptimizer,
    ) -> None:
        self.repository = repository
        self.transfer_optimizer = transfer_optimizer
        self.squad_optimizer = squad_optimizer

    @staticmethod
    def _fixture_path(payload: dict[str, Any], horizon: int = 3) -> dict[str, Any]:
        weeks = payload.get("per_gameweek", [])[:horizon]
        fixtures = []
        scores = []
        for week in weeks:
            week_fixtures = week.get("fixtures", [])
            if not week_fixtures:
                fixtures.append(f"GW{week.get('event_id')} blank")
                continue
            labels = []
            for fixture in week_fixtures:
                opponent = fixture.get("opponent") or "—"
                venue = fixture.get("venue") or ""
                labels.append(f"{opponent} {venue}".strip())
                scores.append(float(fixture.get("position_fixture_score") or 0))
            fixtures.append(f"GW{week.get('event_id')} {' + '.join(labels)}")
        return {
            "label": " · ".join(fixtures) if fixtures else "No fixture path available",
            "score": round(sum(scores) / len(scores), 1) if scores else 0.0,
        }

    @staticmethod
    def _squad_rating(
        squad: list[dict[str, Any]], projections: dict[int, dict[str, Any]], horizon: int
    ) -> dict[str, Any]:
        """Describe the imported squad using existing forecasts; never alter the model."""
        if len(squad) != 15 or sum(int(pick.get("squad_position") or 99) <= 11 for pick in squad) != 11:
            return {"status": "squad_required", "score": None, "horizon": horizon}
        if any(int(pick["id"]) not in projections for pick in squad):
            return {"status": "projection_unavailable", "score": None, "horizon": horizon}

        positions = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
        peers: dict[int, list[float]] = {}
        for position_id in positions:
            peers[position_id] = sorted(
                float(row["expected_points"])
                for row in projections.values()
                if int(row.get("position_id") or 0) == position_id
                and row.get("status") == "a"
                and float(row.get("expected_minutes") or 0) >= 45
                and (row.get("chance_next") is None or int(row["chance_next"]) >= 75)
            )
        if any(len(group) < 10 for group in peers.values()):
            return {"status": "benchmark_unavailable", "score": None, "horizon": horizon}

        starters: list[float] = []
        bench: list[float] = []
        captain_count = 0
        for pick in squad:
            row = projections[int(pick["id"])]
            position_id = int(row.get("position_id") or 0)
            if position_id not in peers:
                return {"status": "benchmark_unavailable", "score": None, "horizon": horizon}
            pool = peers[position_id]
            points = float(row["expected_points"])
            lower = sum(peer < points for peer in pool)
            tied = sum(peer == points for peer in pool)
            percentile = 100 * (lower + tied / 2) / len(pool)
            if int(pick["squad_position"]) <= 11:
                starters.append(percentile)
                if pick.get("is_captain"):
                    starters.append(percentile)
                    captain_count += 1
            else:
                bench.append(percentile)
        if len(starters) != 12 or len(bench) != 4 or captain_count != 1:
            return {"status": "squad_required", "score": None, "horizon": horizon}

        xi_score = sum(starters) / len(starters)
        bench_score = sum(bench) / len(bench)
        return {
            "status": "ready",
            "score": round(0.8 * xi_score + 0.2 * bench_score),
            "xi_score": round(xi_score),
            "bench_score": round(bench_score),
            "horizon": horizon,
            "peer_counts": {positions[position_id]: len(group) for position_id, group in peers.items()},
            "method": "80% starting XI (captain counted twice), 20% bench; each player ranked by forecast points against available same-position players averaging at least 45 expected minutes per fixture.",
        }

    @staticmethod
    def _decision(transfers: dict[str, Any]) -> dict[str, Any]:
        suggestions = transfers.get("suggestions") or []
        if not suggestions:
            return {
                "action": "team_required",
                "title": "Connect your FPL team",
                "summary": transfers.get("message") or "Import a squad to unlock a team-specific decision.",
                "net_gain": 0.0,
                "confidence": 0.0,
            }
        if transfers.get("decision_safety") == "refresh_required":
            return {
                "action": "verification_required",
                "title": "Verify before deciding",
                "summary": " ".join(transfers.get("data_warnings") or []) or "Confirm your team context before acting.",
                "net_gain": 0.0,
                "confidence": 0.0,
            }
        best = suggestions[0]
        if best.get("action") == "do_nothing":
            return {
                "action": "hold",
                "title": "Do Nothing",
                "summary": best.get("why") or "No transfer clears the action threshold.",
                "net_gain": 0.0,
                "confidence": float(best.get("confidence") or 0),
            }
        return {
            "action": "transfer",
            "title": f"{'Potential: ' if transfers.get('decision_safety') == 'estimate_only' else ''}{best['sell']['name']} → {best['buy']['name']}",
            "summary": (best.get("why") or "Best upgrade for the selected planning window.") + (
                " Affordability uses an estimated selling price; check the exact price in FPL before acting."
                if transfers.get("decision_safety") == "estimate_only" else ""
            ),
            "net_gain": float(best.get("net_expected_gain") or 0),
            "confidence": float(best.get("confidence") or 0),
            "sell": best.get("sell"),
            "buy": best.get("buy"),
            "gain_3": float(best.get("gain_3") or 0),
            "gain_6": float(best.get("gain_6") or 0),
            "bank_after": float(best.get("bank_after") or 0),
        }

    def build(self, *, horizon: int | None = None, risk: str | None = None) -> dict[str, Any]:
        profile = self.repository.profile() or {}
        selected_horizon = int(horizon or profile.get("horizon") or 6)
        if selected_horizon not in ALLOWED_HORIZONS:
            raise ValueError("horizon must be one of 1, 3, 5, 6, or 8")
        selected_risk = str(risk or profile.get("risk_preference") or "balanced").lower()
        if selected_risk not in ALLOWED_RISKS:
            raise ValueError("risk must be conservative, balanced, or aggressive")

        status = self.repository.status()
        squad = self.repository.squad()
        calibration = self.repository.calibration_status() or {"status": "collecting"}
        transfer_result = self.transfer_optimizer.suggestions(
            horizon_override=selected_horizon,
            risk_override=selected_risk,
        )
        optimized = self.squad_optimizer.optimize(
            horizon_override=selected_horizon,
            risk_override=selected_risk,
        )
        selected_rows = {
            int(row["id"]): row
            for row in self.repository.projections(horizon=selected_horizon, limit=1000)
        }
        squad_rating = self._squad_rating(squad, selected_rows, selected_horizon)
        one_week_rows = {
            int(row["id"]): row
            for row in self.repository.projections(horizon=1, limit=1000)
        }
        payloads = self.repository.current_projection_payloads()

        starters = [player for player in squad if int(player.get("squad_position") or 99) <= 11]
        starter_ids = {int(player["id"]) for player in starters}
        captain = next((player for player in starters if player.get("is_captain")), None)
        captain_id = int(captain["id"]) if captain else None
        selected_total = sum(
            float(selected_rows.get(player_id, {}).get("expected_points") or 0)
            for player_id in starter_ids
        )
        one_week_total = sum(
            float(one_week_rows.get(player_id, {}).get("expected_points") or 0)
            for player_id in starter_ids
        )
        if captain_id is not None:
            selected_total += float(selected_rows.get(captain_id, {}).get("expected_points") or 0)
            one_week_total += float(one_week_rows.get(captain_id, {}).get("expected_points") or 0)

        alerts: list[dict[str, Any]] = []
        transfer_sellers = {
            int(item["sell"]["id"]): item
            for item in transfer_result.get("suggestions", [])
            if item.get("action") == "transfer" and transfer_result.get("decision_safety") == "ready"
        }
        rankings = []
        fixture_outlook = []
        for player in squad:
            player_id = int(player["id"])
            row = selected_rows.get(player_id, {})
            expected = float(row.get("expected_points") or 0)
            rankings.append(
                {
                    "id": player_id,
                    "name": player.get("web_name"),
                    "team": player.get("team"),
                    "position": player.get("player_position"),
                    "expected_points": round(expected, 2),
                    "confidence": round(float(row.get("confidence") or 0), 4),
                    "starter": player_id in starter_ids,
                }
            )
            fixture = self._fixture_path(payloads.get(player_id, {}))
            fixture_outlook.append(
                {
                    "id": player_id,
                    "name": player.get("web_name"),
                    "team": player.get("team"),
                    "position": player.get("player_position"),
                    **fixture,
                }
            )
            status_code = player.get("status")
            chance = row.get("chance_next")
            no_play = float(row.get("no_play_probability") or 0)
            if status_code != "a" or (chance is not None and int(chance) < 75):
                alerts.append(
                    {
                        "severity": "danger",
                        "player": player.get("web_name"),
                        "message": player.get("news") or "Availability concern",
                    }
                )
            elif no_play >= 0.25 or float(row.get("start_probability") or 0) < 0.45:
                alerts.append(
                    {
                        "severity": "warning",
                        "player": player.get("web_name"),
                        "message": f"Rotation risk: {no_play * 100:.0f}% projected no-play probability.",
                    }
                )
            elif player_id in transfer_sellers:
                suggestion = transfer_sellers[player_id]
                alerts.append(
                    {
                        "severity": "opportunity",
                        "player": player.get("web_name"),
                        "message": f"Upgrade path: {suggestion['buy']['name']} projects {suggestion['net_expected_gain']:+.1f} net points.",
                    }
                )

        severity_order = {"danger": 0, "warning": 1, "opportunity": 2}
        alerts.sort(key=lambda item: severity_order.get(item["severity"], 9))
        rankings.sort(key=lambda item: item["expected_points"], reverse=True)
        fixture_outlook.sort(key=lambda item: item["score"], reverse=True)
        planning_event = status.get("planning_event") or status.get("current_event") or status.get("next_event")
        last_sync = status.get("last_sync") or {}

        return {
            "status": "ready" if status.get("ready") else "data-required",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "planning_event": planning_event,
            "filters": {"horizon": selected_horizon, "risk": selected_risk},
            "manager": {
                "team_id": profile.get("team_id"),
                "team_name": profile.get("team_name"),
                "bank": profile.get("bank"),
                "free_transfers": profile.get("free_transfers"),
                "imported_gameweek": profile.get("imported_gameweek"),
                "squad_connected": len(squad) == 15,
            },
            "decision": self._decision(transfer_result),
            "current_squad": {
                "players": len(squad),
                "starters": len(starters),
                "captain": captain.get("web_name") if captain else None,
                "one_week_expected_points": round(one_week_total, 2),
                "horizon_expected_points": round(selected_total, 2),
                "alerts": alerts[:6],
                "ranking": rankings,
                "fixture_outlook": {
                    "best": fixture_outlook[:4],
                    "difficult": list(reversed(fixture_outlook[-4:])),
                },
            },
            "squad_rating": squad_rating,
            "transfers": transfer_result,
            "optimized_squad": {
                "status": optimized.get("status"),
                "formation": optimized.get("formation"),
                "total_expected_points": optimized.get("total_expected_points"),
                "squad_cost": optimized.get("squad_cost"),
                "bank_remaining": optimized.get("bank_remaining"),
                "captain": optimized.get("captain"),
                "vice_captain": optimized.get("vice_captain"),
                "comparison": optimized.get("comparison"),
            },
            "reliability": calibration,
            "freshness": {
                "last_sync": last_sync.get("completed_at"),
                "bootstrap_source": last_sync.get("bootstrap_source"),
                "fixtures_source": last_sync.get("fixtures_source"),
                "used_stale_data": bool(last_sync.get("used_stale_data")),
                "quality": status.get("quality"),
            },
            "sources": [
                {
                    "name": "Official FPL API snapshot",
                    "datasets": ["bootstrap-static", "fixtures", "entry picks", "event live"],
                    "freshness": last_sync.get("completed_at"),
                },
                {
                    "name": "FPL AI projection and optimization store",
                    "datasets": ["player projections", "rolling calibration", "transfer optimizer", "squad optimizer"],
                    "freshness": self.repository.projection_status().get("created_at") if self.repository.projection_status() else None,
                },
            ],
        }
