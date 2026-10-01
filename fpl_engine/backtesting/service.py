from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
from math import sqrt
import random
from statistics import fmean
from typing import Any
from zoneinfo import ZoneInfo

from fpl_engine.data.repository import Repository
from fpl_engine.data.client import FplApiClient, FplApiError
from fpl_engine.decision_safety import data_warnings


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _ranks(values: list[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    result = [0.0] * len(values)
    cursor = 0
    while cursor < len(indexed):
        end = cursor + 1
        while end < len(indexed) and indexed[end][1] == indexed[cursor][1]:
            end += 1
        average_rank = (cursor + end - 1) / 2.0
        for index in range(cursor, end):
            result[indexed[index][0]] = average_rank
        cursor = end
    return result


def _bootstrap_ci(values: list[float], *, seed: int = 42, rounds: int = 1000) -> tuple[float, float] | None:
    """Deterministic percentile bootstrap CI for an already-computed metric sample."""
    if len(values) < 2:
        return None
    rng = random.Random(seed)
    n = len(values)
    samples = []
    for _ in range(rounds):
        draw = [values[rng.randrange(n)] for _ in range(n)]
        samples.append(fmean(draw))
    samples.sort()
    lo = samples[max(0, int(0.025 * (rounds - 1)))]
    hi = samples[min(rounds - 1, int(0.975 * (rounds - 1)))]
    return round(lo, 4), round(hi, 4)


def _correlation(left: list[float], right: list[float]) -> float | None:
    if len(left) < 2 or len(right) != len(left):
        return None
    left_mean, right_mean = fmean(left), fmean(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right))
    left_sum = sum((x - left_mean) ** 2 for x in left)
    right_sum = sum((y - right_mean) ** 2 for y in right)
    denominator = sqrt(left_sum * right_sum)
    return numerator / denominator if denominator else None


@dataclass(frozen=True)
class BacktestSummary:
    snapshot_id: int
    status: str
    evaluated_events: int
    sample_size: int
    mae: float | None
    rmse: float | None
    rank_correlation: float | None
    start_brier: float | None
    interval_coverage: float | None
    reliability_score: float | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class BacktestService:
    """Close stored pre-deadline forecasts against official finished-GW outcomes."""

    MIN_CALIBRATION_EVENTS = 3
    MIN_CALIBRATION_ROWS = 100

    def __init__(self, repository: Repository, client: FplApiClient | None = None):
        self.repository = repository
        self.client = client

    @staticmethod
    def _event_finalised(state: dict[str, Any] | None, *, now: datetime | None = None) -> bool:
        if not state or not state.get("finished") or not state.get("data_checked"):
            return False
        fixtures = state.get("fixtures") or []
        if not fixtures or any(not row.get("finished") or row.get("finished_provisional") for row in fixtures):
            return False
        kickoffs = [_parse_timestamp(row.get("kickoff_time")) for row in fixtures]
        if any(value is None or value.tzinfo is None for value in kickoffs):
            return False
        uk = ZoneInfo("Europe/London")
        final_date = max(value.astimezone(uk) for value in kickoffs).date()
        lock = datetime.combine(final_date + timedelta(days=1), time(9, 0), tzinfo=uk)
        return (now or datetime.now(timezone.utc)).astimezone(uk) >= lock

    def capture_current_decision(self, transfer_result: dict[str, Any]) -> dict[str, Any] | None:
        """Persist the current manager decision before the deadline for honest later scoring."""
        status = self.repository.status()
        if data_warnings(status) or transfer_result.get("decision_safety") not in {"ready", "estimate_only"}:
            return None
        planning = status.get("planning_event") or status.get("next_event")
        if not isinstance(planning, dict) or not planning.get("id") or not planning.get("deadline_time"):
            return None
        deadline = _parse_timestamp(planning.get("deadline_time"))
        if deadline is None or datetime.now(deadline.tzinfo) > deadline:
            return None
        profile = self.repository.profile() or {}
        imported_event = int(profile.get("imported_gameweek") or 0)
        warning = str(profile.get("source_warning") or "")
        if imported_event < int(planning["id"]) and "Local pending transfer recorded:" not in warning:
            return None
        squad = self.repository.squad()
        starter_ids = [
            int(player["id"])
            for player in squad
            if int(player.get("squad_position") or 99) <= 11
        ]
        one_week = {
            int(player["id"]): player
            for player in self.repository.projections(horizon=1, limit=1000)
        }
        captain_pool = [one_week[player_id] for player_id in starter_ids if player_id in one_week]
        captain = max(
            captain_pool,
            key=lambda player: (float(player.get("expected_points") or 0), float(player.get("confidence") or 0)),
            default=None,
        )
        suggestions = transfer_result.get("suggestions") or []
        transfer = suggestions[0] if suggestions else {"action": "unavailable"}
        captain_payload = (
            {
                "id": int(captain["id"]),
                "name": captain["web_name"],
                "predicted_points": float(captain.get("expected_points") or 0),
            }
            if captain
            else {}
        )
        snapshot_id = self.repository.save_decision_snapshot(
            planning_event=int(planning["id"]),
            event_deadline_time=str(planning["deadline_time"]),
            transfer=transfer,
            captain=captain_payload,
            squad=starter_ids,
            team_id=int(profile["team_id"]) if profile.get("team_id") else None,
            squad_picks=[
                {
                    "id": int(player["id"]),
                    "position": int(player.get("squad_position") or 0),
                    "multiplier": int(player.get("multiplier") or 0),
                    "is_captain": bool(player.get("is_captain")),
                    "is_vice_captain": bool(player.get("is_vice_captain")),
                }
                for player in squad
            ],
            context={
                "model_version": transfer_result.get("model_version"),
                "horizon": transfer_result.get("horizon"),
                "decision_safety": transfer_result.get("decision_safety"),
            },
        )
        return {
            "snapshot_id": snapshot_id,
            "planning_event": int(planning["id"]),
            "transfer_action": transfer.get("action"),
            "captain": captain_payload.get("name"),
        }

    def decision_ledger(self, *, refresh_official: bool = False) -> dict[str, Any]:
        """Audit advice matched by official picks without crediting hypothetical moves."""
        profile = self.repository.profile() or {}
        team_id = profile.get("team_id")
        if not team_id:
            return {"status": "connect_team", "summary": {}, "records": [],
                    "tracking": {"ready": False, "issues": ["Connect your FPL team to start tracking decisions."]}}
        status = self.repository.status()
        planning = status.get("planning_event") or status.get("next_event")
        planning_id = planning.get("id") if isinstance(planning, dict) else planning
        tracking_issues = data_warnings(status)
        if planning_id and int(profile.get("imported_gameweek") or 0) < int(planning_id) and (
            "Local pending transfer recorded:" not in str(profile.get("source_warning") or "")
        ):
            tracking_issues.append(
                f"GW{planning_id} picks are not public yet. Import one official My Team snapshot "
                "to lock a current squad before the deadline; no individual price entry is needed."
            )
        snapshots = self.repository.decision_snapshots_for_ledger(int(team_id))
        latest: dict[int, dict[str, Any]] = {}
        for snapshot in snapshots:
            latest[int(snapshot["planning_event"])] = snapshot
        records = [
            self._ledger_record(snapshot, int(team_id), refresh_official=refresh_official)
            for _, snapshot in sorted(latest.items(), reverse=True)
        ]
        verified = [row for row in records if row["status"] == "verified_transfer"]
        return {
            "status": "ready",
            "summary": {
                "verified_net_points": round(sum(row["realized"]["net_points"] for row in verified), 1),
                "verified_transfers": len(verified),
                "positive_transfers": sum(row["realized"]["net_points"] > 0 for row in verified),
                "unverified_decisions": sum(row["status"] not in {"verified_transfer", "verified_hold", "not_followed", "pending"} for row in records),
                "not_followed": sum(row["status"] == "not_followed" for row in records),
                "pending": sum(row["status"] == "pending" for row in records),
            },
            "records": records,
            "tracking": {"ready": not tracking_issues, "issues": tracking_issues},
            "methodology": (
                "Verified points count only an official squad that matches one saved transfer, with the same other 14 players, "
                "unchanged lineup and captaincy, one official transfer, matching hit cost, no chip or "
                "automatic substitutions, and both compared players appearing. The alternative is holding "
                "the outgoing player in the same slot. This measures the matched move, not proof that advice caused it; "
                "other decisions are not counted as earned points."
            ),
        }

    def _ledger_record(self, snapshot: dict[str, Any], team_id: int, *, refresh_official: bool = False) -> dict[str, Any]:
        event_id = int(snapshot["planning_event"])
        transfer = snapshot.get("transfer") or {}
        action = transfer.get("action") or "unavailable"
        context = snapshot.get("context") or {}
        opportunity = float(transfer.get("opportunity_cost") or 0)
        predicted_1 = (
            round(float(transfer["gain_1"]) + opportunity, 2)
            if action == "transfer" and transfer.get("gain_1") is not None else None
        )
        record = {
            "gameweek": event_id,
            "snapshot_at": snapshot["created_at"],
            "action": action,
            "sell": transfer.get("sell"),
            "buy": transfer.get("buy"),
            "predicted_gw1_direct": predicted_1,
            "predicted_horizon_gain": transfer.get("net_expected_gain"),
            "horizon": context.get("horizon"),
            "model_version": context.get("model_version"),
            "input_quality": context.get("decision_safety"),
            "recommended_captain": snapshot.get("captain"),
            "actual_captain": None,
            "status": "pending",
            "explanation": "Awaiting official, finalised gameweek results.",
            "realized": None,
        }
        if not self._event_finalised(self.repository.backtest_event_state(event_id)):
            return record
        baseline = snapshot.get("squad_picks")
        if snapshot.get("team_id") != team_id or not isinstance(baseline, list) or len(baseline) != 15:
            record.update(status="unverified", explanation="This older decision lacks a locked 15-player team snapshot, so points added cannot be verified.")
            return record
        if self.client is None:
            record.update(status="unverified", explanation="Official post-deadline picks are unavailable to the ledger.")
            return record
        try:
            fetched = self.client.picks(team_id, event_id, force=refresh_official)
            official = fetched.data
        except (FplApiError, OSError, ValueError) as exc:
            record.update(status="unverified", explanation=f"Official GW{event_id} picks could not be checked: {exc}")
            return record
        if not isinstance(official, dict) or not isinstance(official.get("picks"), list) or len(official["picks"]) != 15:
            record.update(status="unverified", explanation="Official post-deadline picks are incomplete.")
            return record
        picks = official["picks"]
        try:
            actual_captain = next((pick for pick in picks if pick.get("is_captain")), None)
            record["actual_captain"] = int(actual_captain["element"]) if actual_captain else None
            actual_ids = [int(pick["element"]) for pick in picks]
            baseline_ids = [int(pick["id"]) for pick in baseline]
        except (KeyError, TypeError, ValueError):
            record.update(status="unverified", explanation="Official or saved picks have incomplete player identifiers.")
            return record
        if len(set(actual_ids)) != 15 or len(set(baseline_ids)) != 15:
            record.update(status="unverified", explanation="The official or locked squad has duplicate player IDs.")
            return record
        history = official.get("entry_history") or {}
        if not isinstance(history, dict):
            history = {}
        transfers = history.get("event_transfers")
        official_cost = history.get("event_transfers_cost")
        try:
            transfer_count = int(transfers) if transfers is not None else None
            cost = int(official_cost) if official_cost is not None else None
        except (TypeError, ValueError):
            transfer_count, cost = None, None
        if action == "do_nothing":
            if set(actual_ids) == set(baseline_ids) and transfer_count == 0:
                record.update(status="verified_hold", explanation="Official picks show no transfer, matching the saved hold advice. No points are claimed for holding.")
            else:
                record.update(status="not_followed", explanation="The official squad or transfer count changed; the saved hold did not match.")
            return record
        if action != "transfer" or not transfer.get("sell") or not transfer.get("buy"):
            record.update(status="unverified", explanation="No actionable transfer was saved for this deadline.")
            return record
        sell_id = int(transfer["sell"]["id"])
        buy_id = int(transfer["buy"]["id"])
        if buy_id not in actual_ids or sell_id in actual_ids:
            record.update(status="not_followed", explanation="The suggested buy and sell do not match the official post-deadline squad.")
            return record
        if set(actual_ids) != (set(baseline_ids) - {sell_id} | {buy_id}):
            record.update(status="followed_unscored", explanation="The suggested move appears in the official squad, but other squad changes prevent clean attribution.")
            return record
        if transfer_count != 1 or cost is None or cost != int(transfer.get("hit_cost") or 0):
            record.update(status="followed_unscored", explanation="The official transfer count or hit cost differs from the locked recommendation.")
            return record
        if official.get("active_chip") or official.get("automatic_subs"):
            record.update(status="followed_unscored", explanation="A chip or automatic substitution makes a simple points comparison unsafe.")
            return record
        before = {int(pick["id"]): pick for pick in baseline}
        after = {int(pick["element"]): pick for pick in picks}
        if sell_id not in before or buy_id not in after:
            record.update(status="followed_unscored", explanation="The locked player positions are incomplete.")
            return record
        sell_slot, buy_slot = before[sell_id], after[buy_id]
        def role(pick: dict[str, Any]) -> tuple[int, int, bool, bool]:
            return (int(pick.get("position") or 0), int(pick.get("multiplier") or 0),
                    bool(pick.get("is_captain")), bool(pick.get("is_vice_captain")))
        if role(sell_slot) != role(buy_slot) or role(buy_slot)[2] or role(buy_slot)[3] or any(
            role(before[player_id]) != role(after[player_id])
            for player_id in before if player_id != sell_id
        ):
            record.update(status="followed_unscored", explanation="The starting XI, bench order, or captaincy changed after the saved decision.")
            return record
        actuals = self.repository.actual_player_gameweek(event_id)
        if buy_id not in actuals or sell_id not in actuals or any(
            int(actuals[player_id].get("minutes") or 0) <= 0 for player_id in (buy_id, sell_id)
        ):
            record.update(status="followed_unscored", explanation="One compared player did not appear, so auto-substitution effects need a full legal replay.")
            return record
        multiplier = role(buy_slot)[1]
        if multiplier not in (0, 1):
            record.update(status="followed_unscored", explanation="The transfer involved a captaincy multiplier that needs a full legal replay.")
            return record
        buy_points = int(actuals[buy_id]["total_points"]) * multiplier
        hold_points = int(actuals[sell_id]["total_points"]) * multiplier
        net = buy_points - hold_points - cost
        record.update(
            status="verified_transfer",
            explanation="Official picks match this one saved move; the rest of the XI and bench are unchanged.",
            realized={
                "buy_points": buy_points,
                "hold_points": hold_points,
                "hit_cost": cost,
                "net_points": net,
            },
        )
        return record

    def refresh(self) -> BacktestSummary:
        eligible_runs: dict[int, dict[str, Any]] = {}
        for run in self.repository.projection_runs_for_backtest():
            created = _parse_timestamp(run.get("created_at"))
            deadline = _parse_timestamp(run.get("deadline_time"))
            if created is None or deadline is None or created > deadline:
                continue
            event_id = int(run["planning_event"])
            previous = eligible_runs.get(event_id)
            if previous is None or (created, int(run["id"])) > (
                _parse_timestamp(previous["created_at"]),
                int(previous["id"]),
            ):
                eligible_runs[event_id] = run

        for event_id, run in eligible_runs.items():
            if not self._event_finalised(self.repository.backtest_event_state(event_id)):
                continue
            event_key = str(run["deadline_time"])
            actuals = self.repository.actual_player_gameweek(event_id)
            evaluation_rows: list[dict[str, Any]] = []
            for record in self.repository.projection_payloads_for_run(int(run["id"])):
                player_id = int(record["player_id"])
                actual = actuals.get(player_id)
                if actual is None:
                    continue
                payload = record["payload"]
                week = next(
                    (item for item in payload.get("per_gameweek", []) if int(item["event_id"]) == event_id),
                    None,
                )
                if week is None:
                    continue
                expected_minutes = float(payload.get("minutes", {}).get("expected_minutes") or 0)
                if int(actual.get("minutes") or 0) <= 0 and expected_minutes < 15.0:
                    continue
                horizon = payload.get("horizons", {}).get("1", {})
                baselines = payload.get("baseline_inputs") or {}
                evaluation_rows.append(
                    {
                        "event_key": event_key,
                        "event_id": event_id,
                        "run_id": int(run["id"]),
                        "player_id": player_id,
                        "predicted_points": float(week.get("expected_points") or 0),
                        "actual_points": float(actual.get("total_points") or 0),
                        "predicted_start": float(payload.get("minutes", {}).get("start_probability") or 0),
                        "actual_start": int(int(actual.get("starts") or 0) > 0),
                        "confidence": float(payload.get("confidence") or 0),
                        "predicted_floor": float(horizon.get("floor") or 0),
                        "predicted_ceiling": float(horizon.get("ceiling") or 0),
                        "recent_points_baseline": baselines.get("recent_points"),
                        "total_points_baseline": baselines.get("total_points"),
                        "xgi_baseline": baselines.get("xgi_per_90"),
                        "fdr_baseline": baselines.get("fdr_score"),
                    }
                )
            self.repository.save_backtest_evaluations(
                event_key=event_key,
                event_id=event_id,
                run_id=int(run["id"]),
                rows=evaluation_rows,
            )

        evaluations = self.repository.backtest_evaluations()
        snapshot = self._calculate_snapshot(evaluations)
        snapshot_id = self.repository.save_calibration_snapshot(snapshot)
        return BacktestSummary(
            snapshot_id=snapshot_id,
            status=snapshot["status"],
            evaluated_events=snapshot["evaluated_events"],
            sample_size=snapshot["sample_size"],
            mae=snapshot["mae"],
            rmse=snapshot["rmse"],
            rank_correlation=snapshot["rank_correlation"],
            start_brier=snapshot["start_brier"],
            interval_coverage=snapshot["interval_coverage"],
            reliability_score=snapshot["reliability_score"],
        )

    def report(self) -> dict[str, Any]:
        evaluations = self.repository.backtest_evaluations()
        calibration = self.repository.calibration_status() or self._calculate_snapshot(evaluations)
        # Confidence intervals are derived from the raw evaluation rows on every report
        # so older stored calibration snapshots cannot silently hide uncertainty.
        fresh_calibration = self._calculate_snapshot(evaluations)
        for key in ("mae_ci95", "rmse_ci95", "start_brier_ci95", "interval_coverage_ci95"):
            calibration[key] = fresh_calibration.get(key)
        projection = self.repository.projection_status() or {}
        events = self._event_report(evaluations)
        benchmarks = self._benchmark_report(evaluations)
        decisions = self._decision_report()
        model = next((item for item in benchmarks if item["key"] == "model"), None)
        available_baselines = [
            item for item in benchmarks if item["key"] != "model" and item["status"] == "ready"
        ]
        best_baseline = max(available_baselines, key=lambda item: item["average_points"], default=None)
        predictive_value = None
        if model and model["status"] == "ready" and best_baseline:
            predictive_value = round(model["average_points"] - best_baseline["average_points"], 2)
        return {
            "status": calibration.get("status", "collecting"),
            "generated_at": datetime.now().astimezone().isoformat(),
            "model": {
                "version": projection.get("model_version"),
                "planning_event": projection.get("planning_event"),
                "forecast_created_at": projection.get("created_at"),
                "deadline_time": projection.get("event_deadline_time"),
                "data_sync_id": projection.get("data_sync_id"),
            },
            "summary": {
                "evaluated_events": int(calibration.get("evaluated_events") or 0),
                "sample_size": int(calibration.get("sample_size") or 0),
                "mae": calibration.get("mae"),
                "mae_ci95": calibration.get("mae_ci95"),
                "rmse": calibration.get("rmse"),
                "rmse_ci95": calibration.get("rmse_ci95"),
                "mean_bias": calibration.get("mean_bias"),
                "rank_correlation": calibration.get("rank_correlation"),
                "start_brier": calibration.get("start_brier"),
                "start_brier_ci95": calibration.get("start_brier_ci95"),
                "interval_coverage": calibration.get("interval_coverage"),
                "interval_coverage_ci95": calibration.get("interval_coverage_ci95"),
                "reliability_score": calibration.get("reliability_score"),
                "predictive_value_vs_best_baseline": predictive_value,
            },
            "benchmarks": benchmarks,
            "events": events,
            "start_calibration": calibration.get("bins") or [],
            "decisions": decisions,
            "methodology": {
                "selection_size": 11,
                "confidence_intervals": "Deterministic percentile bootstrap, 1,000 resamples, seed 42; intervals are omitted when fewer than 2 observations exist.",
                "forecast_rule": "Latest saved forecast at or before each official deadline",
                "baseline_rule": "Features captured inside the same pre-deadline projection payload",
                "calibration_threshold": {
                    "events": self.MIN_CALIBRATION_EVENTS,
                    "player_gameweeks": self.MIN_CALIBRATION_ROWS,
                },
                "limitations": [
                    "Benchmark comparisons begin only after baseline inputs are stored in a pre-deadline snapshot.",
                    "Transfer, hit and captain outcomes begin only after a saved decision gameweek finishes.",
                    "Six-gameweek transfer gain is shown only after all six target gameweeks finish.",
                ],
            },
        }

    @staticmethod
    def _event_report(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[int(row["event_id"])].append(row)
        result = []
        for event_id, event_rows in sorted(grouped.items()):
            errors = [float(row["predicted_points"]) - float(row["actual_points"]) for row in event_rows]
            correlation = _correlation(
                _ranks([float(row["predicted_points"]) for row in event_rows]),
                _ranks([float(row["actual_points"]) for row in event_rows]),
            )
            coverage = fmean(
                float(float(row["predicted_floor"]) <= float(row["actual_points"]) <= float(row["predicted_ceiling"]))
                for row in event_rows
            )
            result.append(
                {
                    "event": event_id,
                    "samples": len(event_rows),
                    "mae": round(fmean(abs(error) for error in errors), 3),
                    "bias": round(fmean(errors), 3),
                    "rank_correlation": round(correlation, 3) if correlation is not None else None,
                    "interval_coverage": round(coverage, 3),
                }
            )
        return result

    @staticmethod
    def _benchmark_report(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        definitions = (
            ("model", "Highest projected points", "predicted_points"),
            ("recent", "Highest recent points", "recent_points_baseline"),
            ("season", "Highest total points", "total_points_baseline"),
            ("xgi", "Highest xGI", "xgi_baseline"),
            ("fdr", "Best fixture score", "fdr_baseline"),
        )
        grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[int(row["event_id"])].append(row)
        results = []
        for key, label, field in definitions:
            event_points = []
            for event_rows in grouped.values():
                eligible = [row for row in event_rows if row.get(field) is not None]
                if not eligible:
                    continue
                selected = sorted(
                    eligible,
                    key=lambda row: float(row.get(field) or 0),
                    reverse=True,
                )[: min(11, len(eligible))]
                event_points.append(sum(float(row["actual_points"]) for row in selected))
            results.append(
                {
                    "key": key,
                    "label": label,
                    "status": "ready" if event_points else "collecting",
                    "events": len(event_points),
                    "average_points": round(fmean(event_points), 2) if event_points else None,
                    "total_points": round(sum(event_points), 2) if event_points else None,
                }
            )
        model = next((item for item in results if item["key"] == "model"), None)
        if model and model["average_points"] is not None:
            for item in results:
                item["delta_vs_model"] = (
                    round(float(item["average_points"]) - float(model["average_points"]), 2)
                    if item["average_points"] is not None
                    else None
                )
        return results

    def _decision_report(self) -> dict[str, Any]:
        snapshots = self.repository.decision_snapshots_for_backtest()
        latest: dict[int, dict[str, Any]] = {}
        for snapshot in snapshots:
            latest[int(snapshot["planning_event"])] = snapshot
        transfer_results = []
        hit_results = []
        long_results = []
        captain_results = []
        for event_id, snapshot in sorted(latest.items()):
            if not self._event_finalised(self.repository.backtest_event_state(event_id)):
                continue
            actuals = self.repository.actual_player_gameweek(event_id)
            transfer = snapshot.get("transfer") or {}
            if transfer.get("action") == "transfer":
                sell_id = int(transfer["sell"]["id"])
                buy_id = int(transfer["buy"]["id"])
                if sell_id in actuals and buy_id in actuals:
                    hit = float(transfer.get("hit_cost") or 0)
                    net = float(actuals[buy_id].get("total_points") or 0) - float(actuals[sell_id].get("total_points") or 0) - hit
                    transfer_results.append(net)
                    if hit > 0:
                        hit_results.append(net)
                    sell_window = self.repository.actual_points_window(sell_id, event_id, 6)
                    buy_window = self.repository.actual_points_window(buy_id, event_id, 6)
                    if sell_window["complete"] and buy_window["complete"]:
                        long_results.append(float(buy_window["points"]) - float(sell_window["points"]) - hit)
            captain = snapshot.get("captain") or {}
            squad_ids = [int(player_id) for player_id in snapshot.get("squad") or []]
            captain_id = captain.get("id")
            squad_actuals = [actuals[player_id] for player_id in squad_ids if player_id in actuals]
            if captain_id is not None and int(captain_id) in actuals and squad_actuals:
                captain_points = float(actuals[int(captain_id)].get("total_points") or 0)
                best_points = max(float(row.get("total_points") or 0) for row in squad_actuals)
                captain_results.append((captain_points, best_points))

        def score(values: list[float]) -> dict[str, Any]:
            return {
                "evaluated": len(values),
                "successful": sum(value > 0 for value in values),
                "success_rate": round(sum(value > 0 for value in values) / len(values), 4) if values else None,
                "average_net_points": round(fmean(values), 2) if values else None,
            }

        return {
            "transfers": score(transfer_results),
            "hits": score(hit_results),
            "long_term_transfers": score(long_results),
            "captaincy": {
                "evaluated": len(captain_results),
                "successes": sum(captain == best for captain, best in captain_results),
                "success_rate": round(sum(captain == best for captain, best in captain_results) / len(captain_results), 4) if captain_results else None,
                "average_points": round(fmean(captain for captain, _ in captain_results), 2) if captain_results else None,
                "average_best_available": round(fmean(best for _, best in captain_results), 2) if captain_results else None,
            },
        }

    @classmethod
    def _calculate_snapshot(cls, rows: list[dict[str, Any]]) -> dict[str, Any]:
        sample_size = len(rows)
        events = sorted({str(row.get("event_key") or row["event_id"]) for row in rows})
        status = (
            "calibrated"
            if len(events) >= cls.MIN_CALIBRATION_EVENTS and sample_size >= cls.MIN_CALIBRATION_ROWS
            else "collecting"
        )
        if not rows:
            return {
                "evaluated_events": 0,
                "sample_size": 0,
                "mae": None,
                "rmse": None,
                "mean_bias": None,
                "rank_correlation": None,
                "start_brier": None,
                "interval_coverage": None,
                "reliability_score": None,
                "points_intercept": 0.0,
                "points_slope": 1.0,
                "status": status,
                "bins": [],
            }

        predicted = [float(row["predicted_points"]) for row in rows]
        actual = [float(row["actual_points"]) for row in rows]
        errors = [forecast - outcome for forecast, outcome in zip(predicted, actual)]
        mae = fmean(abs(error) for error in errors)
        rmse = sqrt(fmean(error**2 for error in errors))
        mean_bias = fmean(errors)
        start_brier = fmean(
            (float(row["predicted_start"]) - int(row["actual_start"])) ** 2 for row in rows
        )
        coverage = fmean(
            float(float(row["predicted_floor"]) <= float(row["actual_points"]) <= float(row["predicted_ceiling"]))
            for row in rows
        )
        by_event: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_event[int(row["event_id"])].append(row)
        event_correlations = []
        for event_rows in by_event.values():
            correlation = _correlation(
                _ranks([float(row["predicted_points"]) for row in event_rows]),
                _ranks([float(row["actual_points"]) for row in event_rows]),
            )
            if correlation is not None:
                event_correlations.append(correlation)
        rank_correlation = fmean(event_correlations) if event_correlations else None

        x_mean, y_mean = fmean(predicted), fmean(actual)
        denominator = sum((value - x_mean) ** 2 for value in predicted)
        raw_slope = (
            sum((x - x_mean) * (y - y_mean) for x, y in zip(predicted, actual))
            / (denominator + 0.05 * sample_size)
            if denominator
            else 1.0
        )
        raw_intercept = y_mean - raw_slope * x_mean
        slope = max(0.5, min(1.5, raw_slope)) if status == "calibrated" else 1.0
        intercept = max(-2.0, min(2.0, raw_intercept)) if status == "calibrated" else 0.0

        bins = []
        for lower in (0.0, 0.2, 0.4, 0.6, 0.8):
            members = [
                row
                for row in rows
                if lower <= float(row["predicted_start"]) < lower + 0.2
                or (lower == 0.8 and float(row["predicted_start"]) == 1.0)
            ]
            if members:
                bins.append(
                    {
                        "lower": lower,
                        "upper": lower + 0.2,
                        "count": len(members),
                        "predicted": round(fmean(float(row["predicted_start"]) for row in members), 4),
                        "actual": round(fmean(int(row["actual_start"]) for row in members), 4),
                    }
                )
        point_score = max(0.0, 1.0 - mae / 5.0)
        rank_score = 0.5 if rank_correlation is None else max(0.0, min(1.0, (rank_correlation + 1.0) / 2.0))
        reliability_score = 0.5 * point_score + 0.25 * (1.0 - start_brier) + 0.25 * rank_score
        mae_ci = _bootstrap_ci([abs(error) for error in errors])
        rmse_ci = _bootstrap_ci([error**2 for error in errors])
        brier_ci = _bootstrap_ci([
            (float(row["predicted_start"]) - int(row["actual_start"])) ** 2 for row in rows
        ])
        coverage_ci = _bootstrap_ci([
            float(float(row["predicted_floor"]) <= float(row["actual_points"]) <= float(row["predicted_ceiling"]))
            for row in rows
        ])
        return {
            "evaluated_events": len(events),
            "sample_size": sample_size,
            "mae": round(mae, 4),
            "mae_ci95": mae_ci,
            "rmse": round(rmse, 4),
            "rmse_ci95": (round(sqrt(rmse_ci[0]), 4), round(sqrt(rmse_ci[1]), 4)) if rmse_ci else None,
            "mean_bias": round(mean_bias, 4),
            "rank_correlation": round(rank_correlation, 4) if rank_correlation is not None else None,
            "start_brier": round(start_brier, 4),
            "start_brier_ci95": brier_ci,
            "interval_coverage": round(coverage, 4),
            "interval_coverage_ci95": coverage_ci,
            "reliability_score": round(reliability_score, 4),
            "points_intercept": round(intercept, 4),
            "points_slope": round(slope, 4),
            "status": status,
            "bins": bins,
        }
