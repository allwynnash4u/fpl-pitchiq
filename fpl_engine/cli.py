from __future__ import annotations

import argparse
import json
import sys

from fpl_engine.app import create_application
from fpl_engine.data.client import FplApiError
from fpl_engine.data.validation import DataValidationError


def _json(value: object) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local FPL data and decision engine")
    subparsers = parser.add_subparsers(dest="command")
    serve = subparsers.add_parser("serve", help="start the local browser application")
    serve.add_argument("--host", default=None, help="bind host (defaults to loopback)")
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--open", action="store_true", help="open a browser tab")
    serve.add_argument("--no-initial-update", action="store_true")
    update = subparsers.add_parser("update", help="refresh official FPL data")
    update.add_argument("--force", action="store_true", help="bypass fresh cache entries")
    subparsers.add_parser("project", help="generate Phase 2 player projections from installed data")
    subparsers.add_parser("backtest", help="close eligible pre-deadline forecasts and update calibration")
    subparsers.add_parser("chips", help="calculate the team-specific eight-gameweek chip plan")
    subparsers.add_parser("status", help="show installed dataset status")
    subparsers.add_parser("validate", help="show stored data-quality results")
    import_team = subparsers.add_parser("import-team", help="import a public team by ID")
    import_team.add_argument("team_id", type=int)
    import_team.add_argument("--gameweek", type=int)
    import_team.add_argument("--bank", type=float)
    import_team.add_argument("--free-transfers", type=int, default=1)
    import_team.add_argument("--horizon", type=int, default=6, choices=(1, 3, 5, 6, 8))
    import_team.add_argument(
        "--risk", default="balanced", choices=("conservative", "balanced", "aggressive")
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    effective_argv = sys.argv[1:] if argv is None else argv
    if not effective_argv:
        effective_argv = ["serve"]
    args = build_parser().parse_args(effective_argv)
    command = args.command
    app = create_application()
    try:
        if command == "serve":
            from fpl_engine.web import serve

            serve(
                app,
                host=args.host or app.settings.host,
                port=args.port or app.settings.port,
                open_browser=args.open,
                initial_update=not args.no_initial_update,
            )
            return 0
        if command == "update":
            sync = app.service.refresh(force=args.force)
            backtest = app.backtest_service.refresh()
            projection = app.projection_service.generate()
            decision_snapshot = app.backtest_service.capture_current_decision(
                app.transfer_optimizer.suggestions()
            )
            _json(
                {
                    "sync": sync.as_dict(),
                    "backtest": backtest.as_dict(),
                    "projection": projection.as_dict(),
                    "decision_snapshot": decision_snapshot,
                }
            )
            return 0
        if command == "project":
            projection = app.projection_service.generate()
            decision_snapshot = app.backtest_service.capture_current_decision(
                app.transfer_optimizer.suggestions()
            )
            _json({"projection": projection.as_dict(), "decision_snapshot": decision_snapshot})
            return 0
        if command == "backtest":
            result = app.backtest_service.refresh()
            _json({"backtest": result.as_dict(), "report": app.backtest_service.report()})
            return 0
        if command == "chips":
            _json(app.chip_planner.plan())
            return 0
        if command == "status":
            _json(app.repository.status())
            return 0
        if command == "validate":
            _json({"status": app.repository.status(), "issues": app.repository.quality_issues()})
            return 0
        if command == "import-team":
            app.repository.save_profile(
                {
                    "team_id": args.team_id,
                    "current_gameweek": args.gameweek,
                    "bank": args.bank,
                    "free_transfers": args.free_transfers,
                    "horizon": args.horizon,
                    "risk_preference": args.risk,
                }
            )
            _json(app.service.import_team(args.team_id, gameweek=args.gameweek))
            return 0
    except (FplApiError, DataValidationError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        if isinstance(exc, DataValidationError):
            _json(exc.report.as_dict())
        return 2
    return 1
