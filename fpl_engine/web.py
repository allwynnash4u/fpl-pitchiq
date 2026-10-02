from __future__ import annotations

import json
import mimetypes
import threading
import urllib.parse
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from fpl_engine.app import Application
from fpl_engine.data.client import FplApiError
from fpl_engine.data.validation import DataValidationError


class ApiRequestError(ValueError):
    pass


def _optional_int(values: dict[str, list[str]], key: str) -> int | None:
    value = values.get(key, [""])[0]
    return int(value) if value else None


def make_handler(app: Application) -> type[BaseHTTPRequestHandler]:
    static_root = Path(__file__).resolve().parent / "static"

    class Handler(BaseHTTPRequestHandler):
        server_version = "FplAiSelector/0.1"

        def log_message(self, format: str, *args: Any) -> None:
            print(f"[web] {self.address_string()} - {format % args}")

        def _json_response(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: int, message: str, details: Any = None) -> None:
            payload = {"ok": False, "error": message}
            if details is not None:
                payload["details"] = details
            self._json_response(payload, status)

        def _read_json(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ApiRequestError("Invalid Content-Length") from exc
            if length > 1_000_000:
                raise ApiRequestError("Request body is too large")
            if length <= 0:
                return {}
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ApiRequestError("Request body must be valid JSON") from exc
            if not isinstance(value, dict):
                raise ApiRequestError("JSON body must be an object")
            return value

        def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path.startswith("/api/"):
                self._handle_api_get(parsed)
                return
            self._serve_static(parsed.path)

        def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
            parsed = urllib.parse.urlparse(self.path)
            try:
                body = self._read_json()
                if parsed.path == "/api/update":
                    summary = app.service.refresh(force=bool(body.get("force", False)))
                    backtest = app.backtest_service.refresh()
                    projection = app.projection_service.generate()
                    decision_snapshot = app.backtest_service.capture_current_decision(
                        app.transfer_optimizer.suggestions()
                    )
                    self._json_response(
                        {
                            "ok": True,
                            "summary": summary.as_dict(),
                            "backtest": backtest.as_dict(),
                            "projection": projection.as_dict(),
                            "decision_snapshot": decision_snapshot,
                        }
                    )
                elif parsed.path == "/api/project":
                    projection = app.projection_service.generate()
                    decision_snapshot = app.backtest_service.capture_current_decision(
                        app.transfer_optimizer.suggestions()
                    )
                    self._json_response({"ok": True, "projection": projection.as_dict(), "decision_snapshot": decision_snapshot})
                elif parsed.path == "/api/backtest":
                    backtest = app.backtest_service.refresh()
                    self._json_response({"ok": True, "backtest": backtest.as_dict(), "report": app.backtest_service.report()})
                elif parsed.path == "/api/chip-plan":
                    self._json_response(
                        app.chip_planner.plan(
                            forced_chip=str(body.get("chip") or "") or None,
                            forced_event=int(body["event"]) if body.get("event") is not None else None,
                        )
                    )
                elif parsed.path == "/api/profile":
                    self._json_response({"ok": True, "profile": app.repository.save_profile(body)})
                elif parsed.path == "/api/planning-preferences":
                    self._json_response({"ok": True, "profile": app.repository.update_planning_preferences(
                        horizon=body.get("horizon"), risk_preference=body.get("risk_preference"),
                    )})
                elif parsed.path == "/api/confirm-manager-context":
                    planning = app.repository.status().get("planning_event")
                    planning_event = planning.get("id") if isinstance(planning, dict) else planning
                    if planning_event is None:
                        raise ApiRequestError("Current planning gameweek is unavailable; refresh official data first")
                    self._json_response({"ok": True, **app.repository.confirm_manager_context(
                        bank=body.get("bank"),
                        free_transfers=body.get("free_transfers"),
                        selling_prices=body.get("selling_prices"),
                        squad_current=body.get("squad_current"),
                        planning_event=int(planning_event),
                    )})
                elif parsed.path == "/api/import-my-team-snapshot":
                    planning = app.repository.status().get("planning_event")
                    planning_event = planning.get("id") if isinstance(planning, dict) else planning
                    if planning_event is None:
                        raise ApiRequestError("Current planning gameweek is unavailable; refresh official data first")
                    self._json_response({"ok": True, **app.repository.import_private_team_snapshot(
                        body.get("snapshot"), planning_event=int(planning_event),
                    )})
                elif parsed.path == "/api/import-team":
                    profile = app.repository.save_profile(body)
                    if not profile.get("team_id"):
                        raise ApiRequestError("A positive Team ID is required")
                    result = app.service.import_team(
                        int(profile["team_id"]), gameweek=profile.get("current_gameweek")
                    )
                    self._json_response(
                        {
                            "ok": True,
                            "result": result,
                            "profile": app.repository.profile(),
                            "squad": app.repository.squad(),
                        }
                    )
                elif parsed.path == "/api/manual-transfer":
                    if body.get("sell_player_id") is None or body.get("buy_player_id") is None:
                        raise ApiRequestError("sell_player_id and buy_player_id are required")
                    manual = app.repository.apply_manual_transfer(
                        int(body["sell_player_id"]), int(body["buy_player_id"])
                    )
                    decision_snapshot = app.backtest_service.capture_current_decision(
                        app.transfer_optimizer.suggestions()
                    )
                    self._json_response({"ok": True, **manual, "decision_snapshot": decision_snapshot})
                elif parsed.path == "/api/chat":
                    self._json_response(
                        app.explanation_service.answer(
                            str(body.get("question") or ""),
                            horizon=int(body["horizon"]) if body.get("horizon") is not None else None,
                            risk=str(body["risk"]) if body.get("risk") is not None else None,
                            scenario=body.get("scenario"),
                        )
                    )
                else:
                    self._error(HTTPStatus.NOT_FOUND, "API route not found")
            except ApiRequestError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            except ValueError as exc:
                self._error(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc))
            except FplApiError as exc:
                status = HTTPStatus.NOT_FOUND if exc.status == 404 else HTTPStatus.BAD_GATEWAY
                self._error(status, str(exc))
            except DataValidationError as exc:
                self._error(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc), exc.report.as_dict())
            except Exception as exc:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Unexpected local error: {exc}")

        def _handle_api_get(self, parsed: urllib.parse.ParseResult) -> None:
            try:
                query = urllib.parse.parse_qs(parsed.query)
                if parsed.path == "/api/health":
                    self._json_response({"ok": True, "service": "fpl_engine"})
                elif parsed.path == "/api/status":
                    self._json_response(
                        {
                            "phase": 8,
                            "phase_name": "AI chip planning and season strategy",
                            **app.repository.status(),
                        }
                    )
                elif parsed.path == "/api/quality":
                    self._json_response({"issues": app.repository.quality_issues()})
                elif parsed.path == "/api/profile":
                    self._json_response(
                        {"profile": app.repository.profile(), "squad": app.repository.squad()}
                    )
                elif parsed.path == "/api/squad":
                    self._json_response({"squad": app.repository.squad()})
                elif parsed.path == "/api/teams":
                    self._json_response({"teams": app.repository.teams()})
                elif parsed.path == "/api/players":
                    self._json_response(
                        {
                            "players": app.repository.players(
                                query=query.get("q", [""])[0],
                                team_id=_optional_int(query, "team"),
                                position_id=_optional_int(query, "position"),
                                status=query.get("status", [None])[0],
                                sort=query.get("sort", ["total_points"])[0],
                                descending=query.get("direction", ["desc"])[0] != "asc",
                                limit=_optional_int(query, "limit") or 100,
                            )
                        }
                    )
                elif parsed.path == "/api/fixtures":
                    self._json_response(
                        {
                            "fixtures": app.repository.fixtures(
                                start_event=_optional_int(query, "start"),
                                horizon=_optional_int(query, "horizon") or 8,
                            )
                        }
                    )
                elif parsed.path == "/api/projection-status":
                    self._json_response({"projection": app.repository.projection_status()})
                elif parsed.path == "/api/calibration":
                    self._json_response({"calibration": app.repository.calibration_status()})
                elif parsed.path == "/api/backtest-report":
                    self._json_response(app.backtest_service.report())
                elif parsed.path == "/api/decision-ledger":
                    self._json_response(app.backtest_service.decision_ledger(
                        refresh_official=query.get("refresh", ["0"])[0] == "1"
                    ))
                elif parsed.path == "/api/chip-planner":
                    self._json_response(app.chip_planner.plan())
                elif parsed.path == "/api/transfer-suggestions":
                    suggestions = app.transfer_optimizer.suggestions()
                    optimized = app.squad_optimizer.optimize()
                    suggestions["final_decision"] = app.dashboard_service.final_decision(
                        suggestions,
                        optimized,
                    )
                    app.backtest_service.capture_current_decision(suggestions)
                    self._json_response(suggestions)
                elif parsed.path == "/api/squad-optimizer":
                    optimizer_options = {}
                    selected_horizon = _optional_int(query, "horizon")
                    selected_risk = query.get("risk", [None])[0]
                    if selected_horizon is not None:
                        optimizer_options["horizon_override"] = selected_horizon
                    if selected_risk is not None:
                        optimizer_options["risk_override"] = selected_risk
                    self._json_response(app.squad_optimizer.optimize(**optimizer_options))
                elif parsed.path.startswith("/api/verification/team/"):
                    team_id = int(parsed.path.rsplit("/", 1)[-1])
                    if team_id <= 0:
                        raise ApiRequestError("Invalid public FPL Team ID")
                    profile = app.repository.profile()
                    if int(profile.get("team_id") or 0) != team_id:
                        result = app.service.import_team(team_id, gameweek=profile.get("current_gameweek"))
                        profile = app.repository.profile()
                    else:
                        result = None
                    squad = app.repository.squad()
                    position_counts = {}
                    club_counts = {}
                    for player in squad:
                        position = int(player.get("position_id") or 0)
                        club = int(player.get("team_id") or 0)
                        position_counts[position] = position_counts.get(position, 0) + 1
                        club_counts[club] = club_counts.get(club, 0) + 1
                    squad_shape_ok = (
                        len(squad) == 15
                        and position_counts == {1: 2, 2: 5, 3: 5, 4: 3}
                        and max(club_counts.values(), default=0) <= 3
                    )
                    manager = profile.get("manager") or {}
                    planning_event = profile.get("current_gameweek")
                    checks = {
                        "team_id": int(profile.get("team_id") or 0) == team_id,
                        "complete_squad": len(squad) == 15,
                        "legal_squad_shape": squad_shape_ok,
                        "current_event_present": planning_event is not None,
                        "bank_present": isinstance(manager.get("bank"), (int, float)),
                        "free_transfers_present": isinstance(manager.get("free_transfers"), int),
                    }
                    self._json_response({
                        "ok": all(checks.values()),
                        "team_id": team_id,
                        "import_result": result,
                        "profile": profile,
                        "squad": squad,
                        "verification": {
                            "checks": checks,
                            "position_counts": position_counts,
                            "max_players_per_club": max(club_counts.values(), default=0),
                        },
                    })
                elif parsed.path == "/api/dashboard":
                    horizon = _optional_int(query, "horizon")
                    risk = query.get("risk", [None])[0]
                    dashboard = app.dashboard_service.build(horizon=horizon, risk=risk)
                    if isinstance(dashboard.get("transfers"), dict):
                        app.backtest_service.capture_current_decision(dashboard["transfers"])
                    self._json_response(dashboard)
                elif parsed.path == "/api/chat-suggestions":
                    self._json_response(app.explanation_service.suggestions())
                elif parsed.path == "/api/projections":
                    self._json_response(
                        {
                            "projection": app.repository.projection_status(),
                            "players": app.repository.projections(
                                horizon=_optional_int(query, "horizon") or 6,
                                position_id=_optional_int(query, "position"),
                                team_id=_optional_int(query, "team"),
                                query=query.get("q", [""])[0],
                                sort=query.get("sort", ["xpts"])[0],
                                limit=_optional_int(query, "limit") or 200,
                            ),
                        }
                    )
                elif parsed.path.startswith("/api/projection/player/"):
                    player_id = int(parsed.path.rsplit("/", 1)[-1])
                    detail = app.repository.projection_detail(player_id)
                    if detail is None:
                        self._error(HTTPStatus.NOT_FOUND, "Projection not found")
                    else:
                        self._json_response({"projection": detail})
                else:
                    self._error(HTTPStatus.NOT_FOUND, "API route not found")
            except ValueError as exc:
                self._error(HTTPStatus.BAD_REQUEST, f"Invalid query parameter: {exc}")
            except Exception as exc:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Unexpected local error: {exc}")

        def _serve_static(self, path: str) -> None:
            names = {
                "/": "index.html",
                "/index.html": "index.html",
                "/app.js": "app.js",
                "/styles.css": "styles.css",
                "/assets/fpl-pitch-half.png": "assets/fpl-pitch-half.png",
            }
            name = names.get(path)
            if name is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            target = static_root / name
            try:
                body = target.read_bytes()
            except OSError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self'; script-src 'self'; "
                "img-src 'self' https://fantasy.premierleague.com data:",
            )
            self.end_headers()
            self.wfile.write(body)

    return Handler


def serve(
    app: Application,
    *,
    host: str,
    port: int,
    open_browser: bool,
    initial_update: bool,
) -> None:
    if initial_update:
        try:
            summary = app.service.refresh(force=False)
            backtest = app.backtest_service.refresh()
            projection = app.projection_service.generate()
            app.backtest_service.capture_current_decision(app.transfer_optimizer.suggestions())
            print(
                f"Official data ready: {summary.players} players, {summary.fixtures} fixtures "
                f"({summary.bootstrap_source}/{summary.fixtures_source})."
            )
            print(
                f"Phase 2 projections ready: {projection.player_count} players from GW{projection.planning_event}."
            )
            print(
                f"Rolling evaluation: {backtest.evaluated_events} completed forecast gameweek(s), "
                f"status {backtest.status}."
            )
        except Exception as exc:
            print(f"Initial refresh unavailable; starting with installed local data: {exc}")
    elif app.repository.status().get("ready") and app.repository.projection_status() is None:
        try:
            projection = app.projection_service.generate()
            app.backtest_service.capture_current_decision(app.transfer_optimizer.suggestions())
            print(f"Phase 2 projections ready: {projection.player_count} players.")
        except Exception as exc:
            print(f"Projection generation unavailable: {exc}")
    server = ThreadingHTTPServer((host, port), make_handler(app))
    url = f"http://{host}:{port}"
    print(f"FPL AI Selector is running at {url}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.35, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping FPL AI Selector.")
    finally:
        server.server_close()
