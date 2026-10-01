from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from fpl_engine.decision_safety import manager_fingerprint

from fpl_engine.data.client import FetchResult
from fpl_engine.data.normalization import NormalizedDataset
from fpl_engine.data.validation import DataQualityReport


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, deadline_time TEXT,
    finished INTEGER NOT NULL, data_checked INTEGER NOT NULL,
    is_previous INTEGER NOT NULL, is_current INTEGER NOT NULL, is_next INTEGER NOT NULL,
    average_entry_score INTEGER, highest_score INTEGER, raw_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY, code INTEGER, name TEXT NOT NULL, short_name TEXT NOT NULL,
    strength INTEGER, strength_overall_home INTEGER, strength_overall_away INTEGER,
    strength_attack_home INTEGER, strength_attack_away INTEGER,
    strength_defence_home INTEGER, strength_defence_away INTEGER, raw_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY, singular_name TEXT NOT NULL, short_name TEXT NOT NULL,
    squad_select INTEGER, squad_min_play INTEGER, squad_max_play INTEGER, raw_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY, code INTEGER, first_name TEXT, second_name TEXT, web_name TEXT NOT NULL,
    team_id INTEGER NOT NULL REFERENCES teams(id), position_id INTEGER NOT NULL REFERENCES positions(id),
    now_cost INTEGER NOT NULL, status TEXT, news TEXT, news_added TEXT,
    chance_this INTEGER, chance_next INTEGER, selected_by_percent REAL,
    total_points INTEGER, event_points INTEGER, points_per_game REAL, form REAL,
    minutes INTEGER, starts INTEGER, goals_scored INTEGER, assists INTEGER,
    clean_sheets INTEGER, goals_conceded INTEGER, own_goals INTEGER,
    penalties_saved INTEGER, penalties_missed INTEGER, yellow_cards INTEGER, red_cards INTEGER,
    saves INTEGER, bonus INTEGER, bps INTEGER, influence REAL, creativity REAL, threat REAL,
    ict_index REAL, expected_goals REAL, expected_assists REAL,
    expected_goal_involvements REAL, expected_goals_conceded REAL,
    defensive_contribution INTEGER,
    corners_order INTEGER, direct_freekicks_order INTEGER, penalties_order INTEGER,
    raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_players_team ON players(team_id);
CREATE INDEX IF NOT EXISTS idx_players_position ON players(position_id);
CREATE INDEX IF NOT EXISTS idx_players_points ON players(total_points DESC);
CREATE TABLE IF NOT EXISTS fixtures (
    id INTEGER PRIMARY KEY, code INTEGER, event_id INTEGER REFERENCES events(id), kickoff_time TEXT,
    team_h_id INTEGER NOT NULL REFERENCES teams(id), team_a_id INTEGER NOT NULL REFERENCES teams(id),
    team_h_score INTEGER, team_a_score INTEGER, team_h_difficulty INTEGER, team_a_difficulty INTEGER,
    started INTEGER NOT NULL, finished INTEGER NOT NULL, finished_provisional INTEGER NOT NULL,
    minutes INTEGER, stats_json TEXT NOT NULL, raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fixtures_event ON fixtures(event_id);
CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, completed_at TEXT NOT NULL,
    bootstrap_fetched_at TEXT NOT NULL, fixtures_fetched_at TEXT NOT NULL,
    bootstrap_source TEXT NOT NULL, fixtures_source TEXT NOT NULL,
    used_stale_data INTEGER NOT NULL, player_count INTEGER NOT NULL,
    fixture_count INTEGER NOT NULL, warning_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS validation_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT, checked_at TEXT NOT NULL,
    severity TEXT NOT NULL, code TEXT NOT NULL, message TEXT NOT NULL,
    entity TEXT NOT NULL, entity_id TEXT
);
CREATE TABLE IF NOT EXISTS profiles (
    profile_id TEXT PRIMARY KEY, team_id INTEGER, manager_name TEXT, team_name TEXT,
    current_gameweek INTEGER, bank_tenths INTEGER, free_transfers INTEGER NOT NULL DEFAULT 1,
    horizon INTEGER NOT NULL DEFAULT 6, risk_preference TEXT NOT NULL DEFAULT 'balanced',
    imported_gameweek INTEGER, updated_at TEXT NOT NULL, source_warning TEXT,
    entry_json TEXT, chip_history_json TEXT NOT NULL DEFAULT '[]',
    manager_confirmation_json TEXT
);
CREATE TABLE IF NOT EXISTS squad_picks (
    profile_id TEXT NOT NULL REFERENCES profiles(profile_id) ON DELETE CASCADE,
    player_id INTEGER NOT NULL REFERENCES players(id), position INTEGER NOT NULL,
    multiplier INTEGER NOT NULL, is_captain INTEGER NOT NULL, is_vice_captain INTEGER NOT NULL,
    purchase_price INTEGER, selling_price INTEGER, raw_json TEXT NOT NULL,
    PRIMARY KEY(profile_id, player_id)
);
CREATE TABLE IF NOT EXISTS player_gameweeks (
    player_id INTEGER NOT NULL REFERENCES players(id), event_id INTEGER NOT NULL REFERENCES events(id),
    minutes INTEGER NOT NULL, starts INTEGER NOT NULL, played INTEGER NOT NULL, total_points INTEGER NOT NULL,
    goals_scored INTEGER NOT NULL, assists INTEGER NOT NULL, clean_sheets INTEGER NOT NULL,
    goals_conceded INTEGER NOT NULL, saves INTEGER NOT NULL, bonus INTEGER NOT NULL, bps INTEGER NOT NULL,
    yellow_cards INTEGER NOT NULL, red_cards INTEGER NOT NULL, own_goals INTEGER NOT NULL,
    penalties_missed INTEGER NOT NULL, expected_goals REAL NOT NULL, expected_assists REAL NOT NULL,
    expected_goals_conceded REAL NOT NULL, defensive_contribution INTEGER NOT NULL,
    raw_json TEXT NOT NULL, PRIMARY KEY(player_id,event_id)
);
CREATE INDEX IF NOT EXISTS idx_player_gameweeks_event ON player_gameweeks(event_id);
CREATE TABLE IF NOT EXISTS projection_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, model_version TEXT NOT NULL,
    planning_event INTEGER NOT NULL, event_deadline_time TEXT,
    max_horizon INTEGER NOT NULL, data_sync_id INTEGER,
    notes TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS player_projections (
    run_id INTEGER NOT NULL REFERENCES projection_runs(id) ON DELETE CASCADE,
    player_id INTEGER NOT NULL,
    xpts_1 REAL NOT NULL, xpts_3 REAL NOT NULL, xpts_5 REAL NOT NULL,
    xpts_6 REAL NOT NULL, xpts_8 REAL NOT NULL, value_6 REAL NOT NULL,
    expected_minutes REAL NOT NULL, start_probability REAL NOT NULL,
    sixty_probability REAL NOT NULL, no_play_probability REAL NOT NULL,
    confidence REAL NOT NULL, floor_6 REAL NOT NULL, median_6 REAL NOT NULL,
    ceiling_6 REAL NOT NULL, payload_json TEXT NOT NULL,
    PRIMARY KEY(run_id,player_id)
);
CREATE INDEX IF NOT EXISTS idx_player_projections_run_xpts ON player_projections(run_id,xpts_6 DESC);
CREATE TABLE IF NOT EXISTS backtest_evaluations (
    event_key TEXT NOT NULL, event_id INTEGER NOT NULL,
    run_id INTEGER NOT NULL REFERENCES projection_runs(id) ON DELETE CASCADE,
    player_id INTEGER NOT NULL, predicted_points REAL NOT NULL, actual_points REAL NOT NULL,
    predicted_start REAL NOT NULL, actual_start INTEGER NOT NULL, confidence REAL NOT NULL,
    predicted_floor REAL NOT NULL, predicted_ceiling REAL NOT NULL,
    recent_points_baseline REAL, total_points_baseline REAL,
    xgi_baseline REAL, fdr_baseline REAL,
    PRIMARY KEY(event_key,player_id)
);
CREATE INDEX IF NOT EXISTS idx_backtest_evaluations_run ON backtest_evaluations(run_id);
CREATE TABLE IF NOT EXISTS calibration_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
    evaluated_events INTEGER NOT NULL, sample_size INTEGER NOT NULL,
    mae REAL, rmse REAL, mean_bias REAL, rank_correlation REAL,
    start_brier REAL, interval_coverage REAL, reliability_score REAL,
    points_intercept REAL NOT NULL, points_slope REAL NOT NULL,
    status TEXT NOT NULL, bins_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decision_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
    planning_event INTEGER NOT NULL, event_deadline_time TEXT,
    transfer_json TEXT NOT NULL, captain_json TEXT NOT NULL,
    squad_json TEXT NOT NULL, team_id INTEGER,
    squad_picks_json TEXT, context_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_decision_snapshots_event
    ON decision_snapshots(planning_event,created_at);
"""


class Repository:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=15)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(players)")}
            if "defensive_contribution" not in columns:
                connection.execute("ALTER TABLE players ADD COLUMN defensive_contribution INTEGER")
            projection_run_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(projection_runs)")
            }
            if "event_deadline_time" not in projection_run_columns:
                connection.execute("ALTER TABLE projection_runs ADD COLUMN event_deadline_time TEXT")
            profile_columns = {row[1] for row in connection.execute("PRAGMA table_info(profiles)")}
            if "chip_history_json" not in profile_columns:
                connection.execute(
                    "ALTER TABLE profiles ADD COLUMN chip_history_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "manager_confirmation_json" not in profile_columns:
                connection.execute("ALTER TABLE profiles ADD COLUMN manager_confirmation_json TEXT")
            decision_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(decision_snapshots)")
            }
            for column, kind in (
                ("team_id", "INTEGER"),
                ("squad_picks_json", "TEXT"),
                ("context_json", "TEXT"),
            ):
                if column not in decision_columns:
                    connection.execute(f"ALTER TABLE decision_snapshots ADD COLUMN {column} {kind}")
            evaluation_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(backtest_evaluations)")
            }
            if "event_key" not in evaluation_columns:
                connection.executescript(
                    """CREATE TABLE backtest_evaluations_v2 (
                           event_key TEXT NOT NULL, event_id INTEGER NOT NULL,
                           run_id INTEGER NOT NULL REFERENCES projection_runs(id) ON DELETE CASCADE,
                           player_id INTEGER NOT NULL, predicted_points REAL NOT NULL,
                           actual_points REAL NOT NULL, predicted_start REAL NOT NULL,
                           actual_start INTEGER NOT NULL, confidence REAL NOT NULL,
                           predicted_floor REAL NOT NULL, predicted_ceiling REAL NOT NULL,
                           PRIMARY KEY(event_key,player_id)
                       );
                       INSERT INTO backtest_evaluations_v2
                           SELECT 'legacy:' || event_id,event_id,run_id,player_id,predicted_points,
                                  actual_points,predicted_start,actual_start,confidence,
                                  predicted_floor,predicted_ceiling
                           FROM backtest_evaluations;
                       DROP TABLE backtest_evaluations;
                       ALTER TABLE backtest_evaluations_v2 RENAME TO backtest_evaluations;
                       CREATE INDEX idx_backtest_evaluations_run
                           ON backtest_evaluations(run_id);"""
                )
                evaluation_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(backtest_evaluations)")
                }
            for column in (
                "recent_points_baseline",
                "total_points_baseline",
                "xgi_baseline",
                "fdr_baseline",
            ):
                if column not in evaluation_columns:
                    connection.execute(f"ALTER TABLE backtest_evaluations ADD COLUMN {column} REAL")
            projection_foreign_keys = connection.execute(
                "PRAGMA foreign_key_list(player_projections)"
            ).fetchall()
            if any(row[2] == "players" for row in projection_foreign_keys):
                connection.executescript(
                    """CREATE TABLE player_projections_v2 (
                           run_id INTEGER NOT NULL REFERENCES projection_runs(id) ON DELETE CASCADE,
                           player_id INTEGER NOT NULL,
                           xpts_1 REAL NOT NULL, xpts_3 REAL NOT NULL, xpts_5 REAL NOT NULL,
                           xpts_6 REAL NOT NULL, xpts_8 REAL NOT NULL, value_6 REAL NOT NULL,
                           expected_minutes REAL NOT NULL, start_probability REAL NOT NULL,
                           sixty_probability REAL NOT NULL, no_play_probability REAL NOT NULL,
                           confidence REAL NOT NULL, floor_6 REAL NOT NULL, median_6 REAL NOT NULL,
                           ceiling_6 REAL NOT NULL, payload_json TEXT NOT NULL,
                           PRIMARY KEY(run_id,player_id)
                       );
                       INSERT INTO player_projections_v2 SELECT * FROM player_projections;
                       DROP TABLE player_projections;
                       ALTER TABLE player_projections_v2 RENAME TO player_projections;
                       CREATE INDEX IF NOT EXISTS idx_player_projections_run_xpts
                           ON player_projections(run_id,xpts_6 DESC);"""
                )

    @staticmethod
    def _planning_event(current: sqlite3.Row | None, next_event: sqlite3.Row | None) -> sqlite3.Row | None:
        """Choose the gameweek a manager can still make transfers for."""
        if current is not None and not bool(current["finished"]):
            deadline = current["deadline_time"]
            if not deadline:
                return current
            try:
                parsed = datetime.fromisoformat(str(deadline).replace("Z", "+00:00"))
                if parsed > datetime.now(timezone.utc):
                    return current
            except ValueError:
                return current
        return next_event or current

    @staticmethod
    def _insert_many(connection: sqlite3.Connection, table: str, rows: Iterable[dict[str, Any]]) -> None:
        rows = list(rows)
        if not rows:
            return
        columns = list(rows[0])
        placeholders = ",".join(f":{column}" for column in columns)
        connection.executemany(
            f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})", rows
        )

    def replace_dataset(
        self,
        dataset: NormalizedDataset,
        report: DataQualityReport,
        bootstrap_fetch: FetchResult,
        fixtures_fetch: FetchResult,
        player_gameweeks: Iterable[dict[str, Any]] = (),
    ) -> None:
        player_gameweeks = list(player_gameweeks)
        with self._connect() as connection:
            # Profile preferences survive refreshes; picks are re-imported because player IDs can change by season.
            connection.execute("DELETE FROM squad_picks")
            connection.execute("DELETE FROM player_gameweeks")
            connection.execute("DELETE FROM fixtures")
            connection.execute("DELETE FROM players")
            connection.execute("DELETE FROM positions")
            connection.execute("DELETE FROM teams")
            connection.execute("DELETE FROM events")
            self._insert_many(connection, "events", dataset.events)
            self._insert_many(connection, "teams", dataset.teams)
            self._insert_many(connection, "positions", dataset.positions)
            self._insert_many(connection, "players", dataset.players)
            self._insert_many(connection, "fixtures", dataset.fixtures)
            self._insert_many(connection, "player_gameweeks", player_gameweeks)
            connection.execute("DELETE FROM validation_issues")
            connection.executemany(
                """INSERT INTO validation_issues
                   (checked_at,severity,code,message,entity,entity_id)
                   VALUES (?,?,?,?,?,?)""",
                [
                    (
                        report.checked_at,
                        issue.severity,
                        issue.code,
                        issue.message,
                        issue.entity,
                        issue.entity_id,
                    )
                    for issue in report.issues
                ],
            )
            connection.execute(
                """INSERT INTO sync_runs
                   (completed_at,bootstrap_fetched_at,fixtures_fetched_at,bootstrap_source,
                    fixtures_source,used_stale_data,player_count,fixture_count,warning_count)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    datetime.now(timezone.utc).isoformat(),
                    bootstrap_fetch.fetched_at.isoformat(),
                    fixtures_fetch.fetched_at.isoformat(),
                    bootstrap_fetch.source,
                    fixtures_fetch.source,
                    int(bootstrap_fetch.stale or fixtures_fetch.stale),
                    len(dataset.players),
                    len(dataset.fixtures),
                    report.warnings,
                ),
            )

    def status(self) -> dict[str, Any]:
        with self._connect() as connection:
            counts = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("players", "teams", "events", "fixtures")
            }
            current = connection.execute(
                "SELECT id,name,deadline_time,finished FROM events WHERE is_current=1 LIMIT 1"
            ).fetchone()
            next_event = connection.execute(
                "SELECT id,name,deadline_time,finished FROM events WHERE is_next=1 LIMIT 1"
            ).fetchone()
            planning_event = self._planning_event(current, next_event)
            sync = connection.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
            severities = dict(
                connection.execute(
                    "SELECT severity,COUNT(*) FROM validation_issues GROUP BY severity"
                ).fetchall()
            )
            return {
                "ready": counts["players"] > 0 and counts["fixtures"] > 0,
                "counts": counts,
                "current_event": dict(planning_event) if planning_event else None,
                "source_current_event": dict(current) if current else None,
                "next_event": dict(next_event) if next_event else None,
                "planning_event": dict(planning_event) if planning_event else None,
                "last_sync": dict(sync) if sync else None,
                "quality": {
                    "errors": severities.get("error", 0),
                    "warnings": severities.get("warning", 0),
                    "info": severities.get("info", 0),
                },
            }

    def quality_issues(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT severity,code,message,entity,entity_id,checked_at
                       FROM validation_issues
                       ORDER BY CASE severity WHEN 'error' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, id"""
                )
            ]

    def players(
        self,
        *,
        query: str = "",
        team_id: int | None = None,
        position_id: int | None = None,
        status: str | None = None,
        sort: str = "total_points",
        descending: bool = True,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        sort_columns = {
            "web_name": "p.web_name",
            "price": "p.now_cost",
            "total_points": "p.total_points",
            "form": "p.form",
            "ownership": "p.selected_by_percent",
            "minutes": "p.minutes",
            "xg": "p.expected_goals",
            "xa": "p.expected_assists",
        }
        order = sort_columns.get(sort, "p.total_points")
        clauses: list[str] = []
        parameters: list[Any] = []
        if query:
            clauses.append("(p.web_name LIKE ? OR p.first_name LIKE ? OR p.second_name LIKE ?)")
            value = f"%{query}%"
            parameters.extend((value, value, value))
        if team_id is not None:
            clauses.append("p.team_id=?")
            parameters.append(team_id)
        if position_id is not None:
            clauses.append("p.position_id=?")
            parameters.append(position_id)
        if status:
            clauses.append("p.status=?")
            parameters.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        direction = "DESC" if descending else "ASC"
        sql = f"""SELECT p.id,p.web_name,p.first_name,p.second_name,p.now_cost/10.0 AS price,
                         p.status,p.news,p.chance_next,p.selected_by_percent,p.total_points,
                         p.event_points,p.points_per_game,p.form,p.minutes,p.starts,p.goals_scored,
                         p.assists,p.clean_sheets,p.bonus,p.bps,p.expected_goals,p.expected_assists,
                         p.expected_goal_involvements,p.penalties_order,p.corners_order,
                         t.id AS team_id,t.short_name AS team,pos.id AS position_id,pos.short_name AS position
                  FROM players p JOIN teams t ON t.id=p.team_id
                  JOIN positions pos ON pos.id=p.position_id{where}
                  ORDER BY {order} {direction}, p.id ASC LIMIT ?"""
        parameters.append(max(1, min(limit, 1000)))
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(sql, parameters)]

    def teams(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT id,code,name,short_name,strength,strength_attack_home,strength_attack_away,strength_defence_home,strength_defence_away FROM teams ORDER BY name"
                )
            ]

    def fixtures(self, *, start_event: int | None = None, horizon: int = 8) -> list[dict[str, Any]]:
        with self._connect() as connection:
            if start_event is None:
                row = connection.execute(
                    "SELECT id FROM events WHERE is_next=1 UNION ALL SELECT id FROM events WHERE is_current=1 LIMIT 1"
                ).fetchone()
                start_event = row[0] if row else 1
            end_event = start_event + max(1, min(horizon, 38)) - 1
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT f.id,f.event_id,e.name AS event_name,f.kickoff_time,
                              h.id AS team_h_id,h.short_name AS team_h,
                              a.id AS team_a_id,a.short_name AS team_a,
                              f.team_h_score,f.team_a_score,f.team_h_difficulty,f.team_a_difficulty,
                              f.started,f.finished
                       FROM fixtures f
                       LEFT JOIN events e ON e.id=f.event_id
                       JOIN teams h ON h.id=f.team_h_id JOIN teams a ON a.id=f.team_a_id
                       WHERE f.event_id BETWEEN ? AND ?
                       ORDER BY f.event_id,f.kickoff_time,f.id""",
                    (start_event, end_event),
                )
            ]

    def save_profile(self, profile: dict[str, Any]) -> dict[str, Any]:
        allowed_risks = {"conservative", "balanced", "aggressive"}
        risk = str(profile.get("risk_preference", "balanced")).lower()
        if risk not in allowed_risks:
            raise ValueError("risk_preference must be conservative, balanced, or aggressive")
        horizon = int(profile.get("horizon", 6))
        if horizon not in {1, 3, 5, 6, 8}:
            raise ValueError("horizon must be one of 1, 3, 5, 6, or 8")
        free_transfers = int(profile.get("free_transfers", 1))
        if not 0 <= free_transfers <= 5:
            raise ValueError("free_transfers must be between 0 and 5")
        bank = profile.get("bank")
        bank_tenths = None if bank in (None, "") else round(float(bank) * 10)
        if bank_tenths is not None and not 0 <= bank_tenths <= 5000:
            raise ValueError("bank must be between 0 and 500 million")
        values = {
            "profile_id": "default",
            "team_id": int(profile["team_id"]) if profile.get("team_id") not in (None, "") else None,
            "current_gameweek": int(profile["current_gameweek"]) if profile.get("current_gameweek") not in (None, "") else None,
            "bank_tenths": bank_tenths,
            "free_transfers": free_transfers,
            "horizon": horizon,
            "risk_preference": risk,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO profiles
                   (profile_id,team_id,current_gameweek,bank_tenths,free_transfers,horizon,risk_preference,updated_at)
                   VALUES (:profile_id,:team_id,:current_gameweek,:bank_tenths,:free_transfers,:horizon,:risk_preference,:updated_at)
                   ON CONFLICT(profile_id) DO UPDATE SET
                   team_id=excluded.team_id,current_gameweek=excluded.current_gameweek,
                   bank_tenths=excluded.bank_tenths,free_transfers=excluded.free_transfers,
                   horizon=excluded.horizon,risk_preference=excluded.risk_preference,updated_at=excluded.updated_at,
                   manager_confirmation_json=NULL""",
                values,
            )
        return self.profile() or {}

    def profile(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM profiles WHERE profile_id='default'").fetchone()
            if not row:
                return None
            result = dict(row)
            result["bank"] = None if result["bank_tenths"] is None else result["bank_tenths"] / 10
            result.pop("bank_tenths", None)
            result.pop("entry_json", None)
            try:
                result["chip_history"] = json.loads(result.pop("chip_history_json") or "[]")
            except (TypeError, json.JSONDecodeError):
                result["chip_history"] = []
            try:
                result["manager_confirmation"] = json.loads(result.pop("manager_confirmation_json") or "null")
            except (TypeError, json.JSONDecodeError):
                result["manager_confirmation"] = None
            return result

    def update_planning_preferences(self, *, horizon: Any, risk_preference: Any) -> dict[str, Any]:
        try:
            selected_horizon = int(horizon)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("horizon must be one of 1, 3, 5, 6, or 8") from exc
        if selected_horizon not in {1, 3, 5, 6, 8}:
            raise ValueError("horizon must be one of 1, 3, 5, 6, or 8")
        selected_risk = str(risk_preference).lower()
        if selected_risk not in {"conservative", "balanced", "aggressive"}:
            raise ValueError("risk_preference must be conservative, balanced, or aggressive")
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE profiles SET horizon=?,risk_preference=?,updated_at=? WHERE profile_id='default'",
                (selected_horizon, selected_risk, datetime.now(timezone.utc).isoformat()),
            )
            if result.rowcount != 1:
                raise ValueError("Connect your FPL team before setting planning preferences")
        return self.profile() or {}

    def confirm_manager_context(self, *, bank: Any, free_transfers: Any,
                                selling_prices: dict[str, Any], squad_current: bool,
                                planning_event: int) -> dict[str, Any]:
        if squad_current is not True:
            raise ValueError("Confirm that all 15 players match your current FPL squad")
        if not isinstance(selling_prices, dict):
            raise ValueError("selling_prices must be an object containing all 15 players")
        try:
            bank_value = float(bank)
            bank_tenths = round(bank_value * 10)
            transfers = int(free_transfers)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("Bank and free transfers must be valid numbers") from exc
        if (not 0 <= bank_tenths <= 5000 or abs(bank_value * 10 - bank_tenths) > 0.001
                or not 0 <= transfers <= 5 or str(free_transfers) != str(transfers)):
            raise ValueError("Bank or free transfers are outside the allowed range")
        squad = self.squad()
        if len(squad) != 15:
            raise ValueError("Import a complete 15-player squad before confirming")
        required = {str(player["id"]) for player in squad}
        if set(selling_prices) != required:
            raise ValueError("Enter an exact selling price for each of the 15 current players")
        prices: dict[int, int] = {}
        for player in squad:
            value = selling_prices[str(player["id"])]
            try:
                tenths = round(float(value) * 10)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"Invalid selling price for {player['web_name']}") from exc
            current_tenths = round(float(player["current_price"]) * 10)
            if not 1 <= tenths <= current_tenths or abs(float(value) * 10 - tenths) > 0.001:
                raise ValueError(f"Selling price for {player['web_name']} must be in £0.1m steps and no higher than current price")
            prices[int(player["id"])] = tenths
        with self._connect() as connection:
            profile_row = connection.execute("SELECT * FROM profiles WHERE profile_id='default'").fetchone()
            if not profile_row or not profile_row["team_id"]:
                raise ValueError("Connect an FPL team before confirming")
            connection.execute(
                "UPDATE profiles SET bank_tenths=?,free_transfers=?,manager_confirmation_json=NULL WHERE profile_id='default'",
                (bank_tenths, transfers),
            )
            for player_id, tenths in prices.items():
                connection.execute(
                    "UPDATE squad_picks SET selling_price=? WHERE profile_id='default' AND player_id=?",
                    (tenths, player_id),
                )
            confirmed_profile = {"team_id": profile_row["team_id"], "bank": bank_tenths / 10,
                                 "free_transfers": transfers}
            confirmed_squad = [
                {**player, "selling_price": prices[int(player["id"])]} for player in squad
            ]
            confirmation = {
                "planning_event": planning_event,
                "confirmed_at": datetime.now(timezone.utc).isoformat(),
                "fingerprint": manager_fingerprint(confirmed_profile, confirmed_squad),
            }
            connection.execute(
                "UPDATE profiles SET updated_at=?,manager_confirmation_json=? WHERE profile_id='default'",
                (confirmation["confirmed_at"], json.dumps(confirmation, separators=(",", ":"))),
            )
        return {"profile": self.profile(), "squad": self.squad()}

    def import_private_team_snapshot(self, payload: dict[str, Any], *, planning_event: int) -> dict[str, Any]:
        """Import only team decision fields copied from FPL's authenticated my-team response."""
        if not isinstance(payload, dict):
            raise ValueError("Paste the complete JSON response from FPL My Team")
        picks = payload.get("picks")
        transfers = payload.get("transfers")
        if not isinstance(picks, list) or len(picks) != 15 or not isinstance(transfers, dict):
            raise ValueError("FPL My Team data must include 15 picks and transfer details")
        try:
            bank_tenths = int(transfers["bank"])
            limit = transfers["limit"]
            made = int(transfers["made"])
            allowance = int(limit)
            free_transfers = max(0, allowance - made)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("FPL My Team data does not include a usable bank and free-transfer allowance") from exc
        if (not 0 <= bank_tenths <= 5000 or not 0 <= allowance <= 5
                or made < 0 or not 0 <= free_transfers <= 5):
            raise ValueError("FPL bank or available free transfers are outside the expected range")
        try:
            normalized = [
                {
                    "element": int(pick["element"]),
                    "position": int(pick["position"]),
                    "multiplier": int(pick.get("multiplier", 0)),
                    "is_captain": bool(pick.get("is_captain")),
                    "is_vice_captain": bool(pick.get("is_vice_captain")),
                    "purchase_price": int(pick["purchase_price"]),
                    "selling_price": int(pick["selling_price"]),
                }
                for pick in picks if isinstance(pick, dict)
            ]
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("Every FPL pick must contain a player, position, purchase and selling price") from exc
        if (len(normalized) != 15 or len({pick["element"] for pick in normalized}) != 15
                or {pick["position"] for pick in normalized} != set(range(1, 16))
                or sum(pick["is_captain"] for pick in normalized) != 1
                or sum(pick["is_vice_captain"] for pick in normalized) != 1):
            raise ValueError("FPL My Team picks must be 15 distinct, ordered players with captain and vice-captain")
        with self._connect() as connection:
            profile = connection.execute("SELECT * FROM profiles WHERE profile_id='default'").fetchone()
            if not profile or not profile["team_id"]:
                raise ValueError("Connect your FPL Team ID before importing My Team data")
            if payload.get("entry") is not None and int(payload["entry"]) != int(profile["team_id"]):
                raise ValueError("The pasted FPL data belongs to a different Team ID")
            player_rows = connection.execute(
                "SELECT id,position_id,now_cost FROM players WHERE id IN (" + ",".join("?" for _ in normalized) + ")",
                [pick["element"] for pick in normalized],
            ).fetchall()
            player_info = {int(row["id"]): row for row in player_rows}
            if len(player_info) != 15:
                raise ValueError("One or more pasted players are missing from current official data; refresh first")
            position_counts = {1: 0, 2: 0, 3: 0, 4: 0}
            for pick in normalized:
                row = player_info[pick["element"]]
                position_counts[int(row["position_id"])] += 1
                if (not 1 <= pick["selling_price"] <= int(row["now_cost"])
                        or pick["purchase_price"] < 1):
                    raise ValueError("Pasted prices are invalid for current official player prices; refresh first")
            if position_counts != {1: 2, 2: 5, 3: 5, 4: 3}:
                raise ValueError("The pasted squad does not meet FPL position quotas")
            stamp = datetime.now(timezone.utc).isoformat()
            connection.execute("DELETE FROM squad_picks WHERE profile_id='default'")
            for pick in normalized:
                connection.execute(
                    """INSERT INTO squad_picks
                       (profile_id,player_id,position,multiplier,is_captain,is_vice_captain,
                        purchase_price,selling_price,raw_json)
                       VALUES ('default',?,?,?,?,?,?,?,?)""",
                    (pick["element"], pick["position"], pick["multiplier"],
                     int(pick["is_captain"]), int(pick["is_vice_captain"]),
                     pick["purchase_price"], pick["selling_price"],
                     json.dumps(pick, separators=(",", ":"))),
                )
            connection.execute(
                """UPDATE profiles SET bank_tenths=?,free_transfers=?,current_gameweek=?,
                   imported_gameweek=?,source_warning=NULL,updated_at=?,manager_confirmation_json=NULL
                   WHERE profile_id='default'""",
                (bank_tenths, free_transfers, planning_event, planning_event, stamp),
            )
            confirmation = {
                "planning_event": planning_event,
                "confirmed_at": stamp,
                "source": "manager_supplied_fpl_snapshot",
                "fingerprint": manager_fingerprint(
                    {"team_id": profile["team_id"], "bank": bank_tenths / 10,
                     "free_transfers": free_transfers},
                    [{"id": pick["element"], "squad_position": pick["position"],
                      "selling_price": pick["selling_price"],
                      "current_price": player_info[pick["element"]]["now_cost"] / 10}
                     for pick in normalized],
                ),
            }
            connection.execute(
                "UPDATE profiles SET manager_confirmation_json=? WHERE profile_id='default'",
                (json.dumps(confirmation, separators=(",", ":")),),
            )
        return {"profile": self.profile(), "squad": self.squad()}

    def save_imported_team(
        self,
        *,
        entry: dict[str, Any],
        picks: dict[str, Any],
        requested_gameweek: int,
        imported_gameweek: int,
        source_warning: str | None,
        chip_history: list[dict[str, Any]] | None = None,
    ) -> None:
        team_id = int(entry["id"])
        entry_history = picks.get("entry_history") or {}
        with self._connect() as connection:
            existing = connection.execute("SELECT * FROM profiles WHERE profile_id='default'").fetchone()
            bank_tenths = existing["bank_tenths"] if existing and existing["bank_tenths"] is not None else entry_history.get("bank")
            free_transfers = existing["free_transfers"] if existing else 1
            horizon = existing["horizon"] if existing else 6
            risk = existing["risk_preference"] if existing else "balanced"
            saved_gameweek = existing["current_gameweek"] if existing else None
            current_gameweek = max(int(saved_gameweek or 1), requested_gameweek)
            connection.execute(
                """INSERT INTO profiles
                   (profile_id,team_id,manager_name,team_name,current_gameweek,bank_tenths,
                    free_transfers,horizon,risk_preference,imported_gameweek,updated_at,source_warning,
                    entry_json,chip_history_json)
                   VALUES ('default',?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(profile_id) DO UPDATE SET
                   team_id=excluded.team_id,manager_name=excluded.manager_name,team_name=excluded.team_name,
                   current_gameweek=excluded.current_gameweek,bank_tenths=excluded.bank_tenths,
                   free_transfers=excluded.free_transfers,horizon=excluded.horizon,
                   risk_preference=excluded.risk_preference,imported_gameweek=excluded.imported_gameweek,
                   updated_at=excluded.updated_at,source_warning=excluded.source_warning,
                   entry_json=excluded.entry_json,chip_history_json=excluded.chip_history_json,
                   manager_confirmation_json=NULL""",
                (
                    team_id,
                    f"{entry.get('player_first_name', '')} {entry.get('player_last_name', '')}".strip(),
                    entry.get("name"),
                    current_gameweek,
                    bank_tenths,
                    free_transfers,
                    horizon,
                    risk,
                    imported_gameweek,
                    datetime.now(timezone.utc).isoformat(),
                    source_warning,
                    json.dumps(entry, separators=(",", ":")),
                    json.dumps(chip_history or [], separators=(",", ":")),
                ),
            )
            connection.execute("DELETE FROM squad_picks WHERE profile_id='default'")
            for pick in picks.get("picks", []):
                connection.execute(
                    """INSERT INTO squad_picks
                       (profile_id,player_id,position,multiplier,is_captain,is_vice_captain,
                        purchase_price,selling_price,raw_json)
                       VALUES ('default',?,?,?,?,?,?,?,?)""",
                    (
                        int(pick["element"]),
                        int(pick["position"]),
                        int(pick.get("multiplier", 0)),
                        int(bool(pick.get("is_captain"))),
                        int(bool(pick.get("is_vice_captain"))),
                        pick.get("purchase_price"),
                        pick.get("selling_price"),
                        json.dumps(pick, separators=(",", ":")),
                    ),
                )

    def squad(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT s.position AS squad_position,s.multiplier,s.is_captain,s.is_vice_captain,
                              s.purchase_price,s.selling_price,p.id,p.web_name,p.now_cost/10.0 AS current_price,
                              p.team_id,p.status,p.news,p.total_points,p.event_points,p.minutes,p.selected_by_percent,
                               t.short_name AS team,t.code AS team_code,pos.short_name AS player_position
                       FROM squad_picks s JOIN players p ON p.id=s.player_id
                       JOIN teams t ON t.id=p.team_id JOIN positions pos ON pos.id=p.position_id
                       WHERE s.profile_id='default' ORDER BY s.position"""
                )
            ]

    def apply_manual_transfer(self, sell_player_id: int, buy_player_id: int) -> dict[str, Any]:
        """Apply a locally declared pending transfer when current FPL picks are private."""
        sell_player_id = int(sell_player_id)
        buy_player_id = int(buy_player_id)
        if sell_player_id == buy_player_id:
            raise ValueError("Choose two different players")
        with self._connect() as connection:
            profile = connection.execute(
                "SELECT * FROM profiles WHERE profile_id='default'"
            ).fetchone()
            if profile is None:
                raise ValueError("Import an FPL squad before recording a pending transfer")
            owned = connection.execute(
                """SELECT s.*,p.web_name,p.position_id,p.team_id
                   FROM squad_picks s JOIN players p ON p.id=s.player_id
                   WHERE s.profile_id='default' ORDER BY s.position"""
            ).fetchall()
            seller = next((row for row in owned if int(row["player_id"]) == sell_player_id), None)
            if seller is None:
                raise ValueError("The outgoing player is not in the imported squad")
            if any(int(row["player_id"]) == buy_player_id for row in owned):
                raise ValueError("The incoming player is already in the squad")
            buyer = connection.execute(
                "SELECT id,web_name,position_id,team_id FROM players WHERE id=?",
                (buy_player_id,),
            ).fetchone()
            if buyer is None:
                raise ValueError("The incoming player is not in the current player database")
            if int(buyer["position_id"]) != int(seller["position_id"]):
                raise ValueError("A pending transfer must replace a player in the same FPL position")
            club_count = sum(
                1
                for row in owned
                if int(row["team_id"]) == int(buyer["team_id"])
                and int(row["player_id"]) != sell_player_id
            )
            if club_count >= 3:
                raise ValueError("This transfer would exceed the three-player club limit")

            raw = {
                "element": buy_player_id,
                "position": int(seller["position"]),
                "multiplier": int(seller["multiplier"]),
                "is_captain": bool(seller["is_captain"]),
                "is_vice_captain": bool(seller["is_vice_captain"]),
                "manual_pending_transfer": {
                    "sell": sell_player_id,
                    "buy": buy_player_id,
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                },
            }
            connection.execute(
                """UPDATE squad_picks
                   SET player_id=?,purchase_price=NULL,selling_price=NULL,raw_json=?
                   WHERE profile_id='default' AND player_id=?""",
                (buy_player_id, json.dumps(raw, separators=(",", ":")), sell_player_id),
            )
            message = (
                f"Local pending transfer recorded: {seller['web_name']} → {buyer['web_name']}. "
                "Official FPL cannot verify this squad change until the gameweek deadline."
            )
            existing_warning = str(profile["source_warning"] or "").strip()
            warning = f"{existing_warning} {message}".strip()
            connection.execute(
                "UPDATE profiles SET source_warning=?,updated_at=?,manager_confirmation_json=NULL WHERE profile_id='default'",
                (warning, datetime.now(timezone.utc).isoformat()),
            )
        return {
            "sell": {"id": sell_player_id, "name": seller["web_name"]},
            "buy": {"id": buy_player_id, "name": buyer["web_name"]},
            "profile": self.profile(),
            "squad": self.squad(),
        }

    def replace_player_gameweeks(self, rows: Iterable[dict[str, Any]]) -> int:
        rows = list(rows)
        with self._connect() as connection:
            connection.execute("DELETE FROM player_gameweeks")
            self._insert_many(connection, "player_gameweeks", rows)
        return len(rows)

    def projection_inputs(self) -> dict[str, Any]:
        """Return typed observations required by the Phase 2 numerical models."""
        with self._connect() as connection:
            players = [
                dict(row)
                for row in connection.execute(
                    """SELECT id,web_name,team_id,position_id,now_cost,status,news,chance_next,
                              selected_by_percent,total_points,minutes,starts,goals_scored,assists,
                              clean_sheets,goals_conceded,own_goals,penalties_saved,penalties_missed,
                              yellow_cards,red_cards,saves,bonus,bps,expected_goals,expected_assists,
                              expected_goal_involvements,expected_goals_conceded,defensive_contribution,
                              corners_order,direct_freekicks_order,penalties_order
                       FROM players"""
                )
            ]
            teams = [
                dict(row)
                for row in connection.execute(
                    """SELECT id,name,short_name,strength,strength_overall_home,strength_overall_away,
                              strength_attack_home,strength_attack_away,
                              strength_defence_home,strength_defence_away FROM teams"""
                )
            ]
            fixtures = [
                dict(row)
                for row in connection.execute(
                    """SELECT id,event_id,kickoff_time,team_h_id,team_a_id,team_h_score,team_a_score,
                              team_h_difficulty,team_a_difficulty,started,finished FROM fixtures
                       ORDER BY event_id,id"""
                )
            ]
            histories = [
                dict(row)
                for row in connection.execute(
                    """SELECT player_id,event_id,minutes,starts,played,total_points,goals_scored,assists,
                              clean_sheets,goals_conceded,saves,bonus,bps,yellow_cards,red_cards,
                              own_goals,penalties_missed,expected_goals,expected_assists,
                              expected_goals_conceded,defensive_contribution
                       FROM player_gameweeks ORDER BY player_id,event_id"""
                )
            ]
            current = connection.execute(
                "SELECT id,name,deadline_time,finished FROM events WHERE is_current=1 LIMIT 1"
            ).fetchone()
            next_event = connection.execute(
                "SELECT id,name,deadline_time,finished FROM events WHERE is_next=1 LIMIT 1"
            ).fetchone()
            event = self._planning_event(current, next_event)
            if event is None:
                event = connection.execute(
                    "SELECT COALESCE(MAX(id),1) AS id,NULL AS deadline_time FROM events WHERE finished=1"
                ).fetchone()
            sync = connection.execute("SELECT id FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
            calibration_row = connection.execute(
                "SELECT * FROM calibration_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
            calibration = dict(calibration_row) if calibration_row else None
            if calibration is not None:
                calibration["bins"] = json.loads(calibration.pop("bins_json"))
            return {
                "planning_event": int(event["id"]),
                "planning_event_deadline": event["deadline_time"],
                "data_sync_id": int(sync["id"]) if sync else None,
                "players": players,
                "teams": teams,
                "fixtures": fixtures,
                "histories": histories,
                "calibration": calibration,
            }

    def save_projection_run(
        self,
        *,
        model_version: str,
        planning_event: int,
        event_deadline_time: str | None,
        max_horizon: int,
        data_sync_id: int | None,
        notes: str,
        projections: Iterable[dict[str, Any]],
    ) -> int:
        rows = list(projections)
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO projection_runs
                   (created_at,model_version,planning_event,event_deadline_time,max_horizon,data_sync_id,notes)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    datetime.now(timezone.utc).isoformat(),
                    model_version,
                    planning_event,
                    event_deadline_time,
                    max_horizon,
                    data_sync_id,
                    notes,
                ),
            )
            run_id = int(cursor.lastrowid)
            connection.executemany(
                """INSERT INTO player_projections
                   (run_id,player_id,xpts_1,xpts_3,xpts_5,xpts_6,xpts_8,value_6,
                    expected_minutes,start_probability,sixty_probability,no_play_probability,
                    confidence,floor_6,median_6,ceiling_6,payload_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        run_id,
                        row["player_id"],
                        row["xpts_1"],
                        row["xpts_3"],
                        row["xpts_5"],
                        row["xpts_6"],
                        row["xpts_8"],
                        row["value_6"],
                        row["expected_minutes"],
                        row["start_probability"],
                        row["sixty_probability"],
                        row["no_play_probability"],
                        row["confidence"],
                        row["floor_6"],
                        row["median_6"],
                        row["ceiling_6"],
                        row["payload_json"],
                    )
                    for row in rows
                ],
            )
        return run_id

    def projection_status(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT r.*,COUNT(p.player_id) AS player_count
                   FROM projection_runs r LEFT JOIN player_projections p ON p.run_id=r.id
                   WHERE r.id=(
                       SELECT MAX(id) FROM projection_runs
                       WHERE data_sync_id=(SELECT MAX(id) FROM sync_runs)
                   )
                   GROUP BY r.id"""
            ).fetchone()
            return dict(row) if row else None

    def projections(
        self,
        *,
        horizon: int = 6,
        position_id: int | None = None,
        team_id: int | None = None,
        query: str = "",
        sort: str = "xpts",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        horizon_columns = {1: "xpts_1", 3: "xpts_3", 5: "xpts_5", 6: "xpts_6", 8: "xpts_8"}
        xpts_column = horizon_columns.get(horizon, "xpts_6")
        sort_columns = {
            "xpts": f"pp.{xpts_column}",
            "value": f"pp.{xpts_column}/NULLIF(p.now_cost/10.0,0)",
            "minutes": "pp.expected_minutes",
            "start": "pp.start_probability",
            "ceiling": "pp.ceiling_6",
            "confidence": "pp.confidence",
            "price": "p.now_cost",
        }
        order = sort_columns.get(sort, f"pp.{xpts_column}")
        current_run = """(
            SELECT MAX(id) FROM projection_runs
            WHERE data_sync_id=(SELECT MAX(id) FROM sync_runs)
        )"""
        clauses = [f"pp.run_id={current_run}"]
        params: list[Any] = []
        if position_id is not None:
            clauses.append("p.position_id=?")
            params.append(position_id)
        if team_id is not None:
            clauses.append("p.team_id=?")
            params.append(team_id)
        if query:
            clauses.append("(p.web_name LIKE ? OR p.first_name LIKE ? OR p.second_name LIKE ?)")
            value = f"%{query}%"
            params.extend((value, value, value))
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    f"""SELECT p.id,p.web_name,p.now_cost/10.0 AS price,p.status,p.news,p.chance_next,
                               p.selected_by_percent,t.id AS team_id,t.short_name AS team,
                               pos.id AS position_id,pos.short_name AS position,
                               CASE ? WHEN 1 THEN pp.xpts_1 WHEN 3 THEN pp.xpts_3
                                      WHEN 5 THEN pp.xpts_5 WHEN 8 THEN pp.xpts_8 ELSE pp.xpts_6 END AS expected_points,
                               (CASE ? WHEN 1 THEN pp.xpts_1 WHEN 3 THEN pp.xpts_3
                                      WHEN 5 THEN pp.xpts_5 WHEN 8 THEN pp.xpts_8 ELSE pp.xpts_6 END)
                                   / NULLIF(p.now_cost/10.0,0) AS value,
                               pp.expected_minutes,pp.start_probability,pp.sixty_probability,
                               pp.no_play_probability,pp.confidence,pp.floor_6,pp.median_6,
                               pp.ceiling_6,pp.value_6
                        FROM player_projections pp JOIN players p ON p.id=pp.player_id
                        JOIN teams t ON t.id=p.team_id JOIN positions pos ON pos.id=p.position_id
                        WHERE {' AND '.join(clauses)} ORDER BY {order} DESC,p.id LIMIT ?""",
                    [horizon, horizon, *params, max(1, min(limit, 1000))],
                )
            ]

    def projection_detail(self, player_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT pp.payload_json FROM player_projections pp
                   WHERE pp.player_id=? AND pp.run_id=(
                       SELECT MAX(id) FROM projection_runs
                       WHERE data_sync_id=(SELECT MAX(id) FROM sync_runs)
                   )""",
                (player_id,),
            ).fetchone()
            return json.loads(row["payload_json"]) if row else None

    def current_projection_payloads(self) -> dict[int, dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT pp.player_id,pp.payload_json FROM player_projections pp
                   WHERE pp.run_id=(
                       SELECT MAX(id) FROM projection_runs
                       WHERE data_sync_id=(SELECT MAX(id) FROM sync_runs)
                   )"""
            ).fetchall()
            return {int(row["player_id"]): json.loads(row["payload_json"]) for row in rows}

    def projection_runs_for_backtest(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT r.id,r.created_at,r.model_version,r.planning_event,r.data_sync_id,
                              e.deadline_time,e.finished
                       FROM projection_runs r JOIN events e
                         ON e.id=r.planning_event AND e.deadline_time=r.event_deadline_time
                       WHERE e.finished=1 ORDER BY r.planning_event,r.created_at,r.id"""
                )
            ]

    def projection_payloads_for_run(self, run_id: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return [
                {"player_id": int(row["player_id"]), "payload": json.loads(row["payload_json"])}
                for row in connection.execute(
                    "SELECT player_id,payload_json FROM player_projections WHERE run_id=?",
                    (run_id,),
                )
            ]

    def actual_player_gameweek(self, event_id: int) -> dict[int, dict[str, Any]]:
        with self._connect() as connection:
            return {
                int(row["player_id"]): dict(row)
                for row in connection.execute(
                    """SELECT player_id,event_id,minutes,starts,total_points
                       FROM player_gameweeks WHERE event_id=?""",
                    (event_id,),
                )
            }

    def backtest_event_state(self, event_id: int) -> dict[str, Any] | None:
        """Return the official event and fixture state needed for a final-results gate."""
        with self._connect() as connection:
            event = connection.execute(
                "SELECT finished,data_checked FROM events WHERE id=?", (event_id,)
            ).fetchone()
            if event is None:
                return None
            fixtures = connection.execute(
                "SELECT kickoff_time,finished,finished_provisional FROM fixtures WHERE event_id=?",
                (event_id,),
            ).fetchall()
            return {
                "finished": bool(event["finished"]),
                "data_checked": bool(event["data_checked"]),
                "fixtures": [dict(fixture) for fixture in fixtures],
            }

    def save_backtest_evaluations(
        self, *, event_key: str, event_id: int, run_id: int, rows: Iterable[dict[str, Any]]
    ) -> int:
        rows = list(rows)
        with self._connect() as connection:
            connection.execute("DELETE FROM backtest_evaluations WHERE event_key=?", (event_key,))
            connection.executemany(
                """INSERT INTO backtest_evaluations
                   (event_key,event_id,run_id,player_id,predicted_points,actual_points,predicted_start,
                    actual_start,confidence,predicted_floor,predicted_ceiling,recent_points_baseline,
                    total_points_baseline,xgi_baseline,fdr_baseline)
                   VALUES (:event_key,:event_id,:run_id,:player_id,:predicted_points,:actual_points,
                           :predicted_start,:actual_start,:confidence,:predicted_floor,:predicted_ceiling,
                           :recent_points_baseline,:total_points_baseline,:xgi_baseline,:fdr_baseline)""",
                [
                    {
                        **row,
                        "recent_points_baseline": row.get("recent_points_baseline"),
                        "total_points_baseline": row.get("total_points_baseline"),
                        "xgi_baseline": row.get("xgi_baseline"),
                        "fdr_baseline": row.get("fdr_baseline"),
                    }
                    for row in rows
                ],
            )
        return len(rows)

    def backtest_evaluations(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT event_key,event_id,run_id,player_id,predicted_points,actual_points,
                              predicted_start,actual_start,confidence,predicted_floor,predicted_ceiling,
                              recent_points_baseline,total_points_baseline,xgi_baseline,fdr_baseline
                       FROM backtest_evaluations ORDER BY event_id,player_id"""
                )
            ]

    def save_decision_snapshot(
        self,
        *,
        planning_event: int,
        event_deadline_time: str | None,
        transfer: dict[str, Any],
        captain: dict[str, Any],
        squad: list[int],
        team_id: int | None = None,
        squad_picks: list[dict[str, Any]] | None = None,
        context: dict[str, Any] | None = None,
    ) -> int:
        transfer_json = json.dumps(transfer, separators=(",", ":"))
        captain_json = json.dumps(captain, separators=(",", ":"))
        squad_json = json.dumps([int(player_id) for player_id in squad], separators=(",", ":"))
        squad_picks_json = json.dumps(squad_picks, separators=(",", ":")) if squad_picks is not None else None
        context_json = json.dumps(context, separators=(",", ":")) if context is not None else None
        with self._connect() as connection:
            previous = connection.execute(
                """SELECT id,event_deadline_time,transfer_json,captain_json,squad_json,
                          squad_picks_json,context_json
                   FROM decision_snapshots
                   WHERE planning_event=? AND team_id IS ? ORDER BY id DESC LIMIT 1""",
                (int(planning_event), team_id),
            ).fetchone()
            if previous and (
                previous["event_deadline_time"] == event_deadline_time
                and previous["transfer_json"] == transfer_json
                and previous["captain_json"] == captain_json
                and previous["squad_json"] == squad_json
                and previous["squad_picks_json"] == squad_picks_json
                and previous["context_json"] == context_json
            ):
                return int(previous["id"])
            cursor = connection.execute(
                """INSERT INTO decision_snapshots
                   (created_at,planning_event,event_deadline_time,transfer_json,captain_json,squad_json,
                    team_id,squad_picks_json,context_json)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    datetime.now(timezone.utc).isoformat(),
                    int(planning_event),
                    event_deadline_time,
                    transfer_json,
                    captain_json,
                    squad_json,
                    team_id,
                    squad_picks_json,
                    context_json,
                ),
            )
            return int(cursor.lastrowid)

    def decision_snapshots_for_backtest(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT d.* FROM decision_snapshots d JOIN events e ON e.id=d.planning_event
                   WHERE e.finished=1 AND d.event_deadline_time=e.deadline_time
                     AND d.created_at<=d.event_deadline_time
                   ORDER BY d.planning_event,d.created_at,d.id"""
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["transfer"] = json.loads(item.pop("transfer_json"))
            item["captain"] = json.loads(item.pop("captain_json"))
            item["squad"] = json.loads(item.pop("squad_json"))
            item["squad_picks"] = json.loads(item.pop("squad_picks_json") or "null")
            item["context"] = json.loads(item.pop("context_json") or "null")
            result.append(item)
        return result

    def decision_snapshots_for_ledger(self, team_id: int) -> list[dict[str, Any]]:
        """Pre-deadline decisions for this manager, including pending GWs and legacy rows."""
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT d.* FROM decision_snapshots d JOIN events e ON e.id=d.planning_event
                   WHERE d.event_deadline_time=e.deadline_time
                     AND d.created_at<=d.event_deadline_time
                     AND (d.team_id=? OR d.team_id IS NULL)
                   ORDER BY d.planning_event,d.created_at,d.id""",
                (int(team_id),),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["transfer"] = json.loads(item.pop("transfer_json"))
            item["captain"] = json.loads(item.pop("captain_json"))
            item["squad"] = json.loads(item.pop("squad_json"))
            item["squad_picks"] = json.loads(item.pop("squad_picks_json") or "null")
            item["context"] = json.loads(item.pop("context_json") or "null")
            result.append(item)
        return result

    def actual_points_window(self, player_id: int, start_event: int, horizon: int) -> dict[str, Any]:
        with self._connect() as connection:
            finished = [
                int(row["id"])
                for row in connection.execute(
                    "SELECT id FROM events WHERE id>=? AND id<? AND finished=1 ORDER BY id",
                    (int(start_event), int(start_event) + int(horizon)),
                )
            ]
            if len(finished) != int(horizon):
                return {"complete": False, "points": None, "events": finished}
            placeholders = ",".join("?" for _ in finished)
            row = connection.execute(
                f"SELECT COALESCE(SUM(total_points),0) AS points FROM player_gameweeks WHERE player_id=? AND event_id IN ({placeholders})",
                (int(player_id), *finished),
            ).fetchone()
        return {"complete": True, "points": float(row["points"]), "events": finished}

    def save_calibration_snapshot(self, snapshot: dict[str, Any]) -> int:
        values = {
            **snapshot,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "bins_json": json.dumps(snapshot.get("bins", []), separators=(",", ":")),
        }
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO calibration_snapshots
                   (created_at,evaluated_events,sample_size,mae,rmse,mean_bias,rank_correlation,
                    start_brier,interval_coverage,reliability_score,points_intercept,points_slope,
                    status,bins_json)
                   VALUES (:created_at,:evaluated_events,:sample_size,:mae,:rmse,:mean_bias,
                           :rank_correlation,:start_brier,:interval_coverage,:reliability_score,
                           :points_intercept,:points_slope,:status,:bins_json)""",
                values,
            )
            return int(cursor.lastrowid)

    def calibration_status(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM calibration_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["bins"] = json.loads(result.pop("bins_json"))
            return result
