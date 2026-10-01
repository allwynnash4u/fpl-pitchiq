# FPL Copilot

FPL Copilot is a local, data-first Fantasy Premier League decision product built around one promise: **your best FPL move, explained**. It combines official data ingestion, explainable projections, rolling calibration, squad-specific transfers, legal squad optimization, a unified decision dashboard, a grounded assistant, deadline-safe backtesting, and an AI chip planner.

The recommendation layer always compares a move with **Do Nothing**, removes negative expected-value alternatives, prices hits and transfer opportunity cost explicitly, and exposes its model version, freshness, confidence definition, assumptions, risks, and 1/3/6-gameweek gain.

The persistent **Best Transfer Opportunities** bar compares legal same-position replacements with keeping the current squad. The **Squad optimizer** builds a legal 15 under the available budget and three-per-club rule, then selects the best valid XI, bench order, captain and vice-captain. The **AI Chip Planner** compares using or saving Bench Boost, Triple Captain, Wildcard and Free Hit across the next eight projected Gameweeks.

The **Dashboard** is the default view. It keeps the transfer decision, deadline, current-XI forecast, resources, squad alerts, player projection shape, fixture outlook, wildcard rebuild and model reliability in one reconciled surface. Its horizon and risk controls update the decision and optimizer context together.

The top-right **Team rating** is a display-only, 0–100 relative projection index for the imported 15-player squad. It compares each player's selected-window expected points with available regular players in the same FPL position, weighs the starting XI (captain twice) at 80% and bench depth at 20%, and shows the split, comparison-pool sizes and data freshness on expansion. It does not change the projection model or represent model confidence, manager rank, win probability or guaranteed points. If the squad or comparison pool is incomplete, it shows no score.

The **AI assistant** explains those calculations in plain language. It can answer transfer, captaincy, squad, risk, fixture, budget, player and reliability questions, including “Why not Player X?”. Its scenario tools compare roll/one/two-transfer routes, preview a named transfer, compare two players, and test a captaincy change without changing the real squad. Every answer includes the calculated evidence and source used. The local explanation engine deliberately refuses unsupported questions rather than inventing football statistics.

The **Backtesting** view asks whether the model actually adds predictive value. It compares the projected-points XI with recent-points, season-points, xGI and fixture-score baselines; tracks MAE, bias, rank correlation, prediction intervals and start-probability calibration; and scores saved transfer, hit and captain decisions only after their gameweeks finish.

The **Decision ledger** is a separate manager-specific view of the latest advice saved before each deadline. It checks public post-deadline FPL picks against that locked squad and shows a one-gameweek net-points comparison with holding the outgoing player only when the move, lineup, transfer cost and official result are directly comparable. Unfollowed, older and ambiguous decisions stay visible but never inflate the verified points total. See [`docs/DECISION_LEDGER.md`](docs/DECISION_LEDGER.md).

The **AI Chip Planner** calculates each chip's incremental value for the imported squad, compares every visible Gameweek, builds legal Wildcard and Free Hit squads, scores Bench Boost readiness, ranks Triple Captain candidates, and simulates collision-free chip sequences. It recommends saving when no opportunity clears a declared threshold.

## Quick start

Requirements: Python 3.11+ and an internet connection for data refreshes. There are no third-party runtime dependencies.

On Windows, double-click `run.cmd` or run:

```powershell
.\run.cmd
```

On any platform:

```bash
python -m fpl_engine serve --open
```

The application opens at `http://127.0.0.1:8765`. On the first launch it tries to download official FPL data. If the network is unavailable, the dashboard still starts and reports that no valid dataset is installed.

## Team setup

Open **My team**, enter the numeric FPL Team ID, current gameweek (or leave automatic), bank balance in millions, free transfers, planning horizon, and risk preference. The public official endpoint can import a squad only after that gameweek's deadline; if the current picks are not public yet, the importer walks backwards to the latest available gameweek and labels the imported gameweek.

For decision planning, “current gameweek” means the next playable gameweek. If the official feed still marks a completed round as `is_current`, the application advances the planning context to the event marked `is_next`.

Public endpoints do not reliably expose the authenticated manager's exact selling prices or authoritative free-transfer count. Copilot still shows model forecasts and single-transfer ideas using current market prices as provisional selling-price estimates, clearly labels affordability as unverified, and keeps two-transfer budget plans unavailable. An optional **My team → Sync exact FPL selling prices** flow accepts one pasted official My Team JSON response to import all 15 current players, exact selling prices, bank and available free transfers together; no password, session cookie or individually entered prices are needed. The pasted response is not stored wholesale. The per-player form remains a fallback.

The persistent **Squad-specific decision brief** shows the current starting XI's mean official FDR over the selected planning window, counts favourable and difficult player-fixtures and blanks, and compares official sell/buy FDR on transfer cards. FDR is a 1–5 source rating (lower is easier); it is displayed separately from Pitch IQ's position-aware fixture score and does not change the projection model.

For exact affordability, import that live snapshot or use the manual fallback. The app stores the decision fields locally and invalidates the precision confirmation after 24 hours, a new planning gameweek, a squad/import change, or a change in current market prices. An automatic data refresh preserves confirmed local inputs when no newer public picks exist; an explicit re-import replaces them. Stale or invalid official data still withholds action advice, but missing private selling prices alone do not hide forecasts or Ask Copilot answers. The dashboard and optimizer share saved planning horizon and risk preferences. The pasted snapshot is manager-supplied data, not an authenticated API connection from Copilot.

Before a gameweek deadline, official FPL also keeps a manager's new picks private. When an import falls back to the previous published gameweek, use **Record a transfer FPL has not published yet** on **My team** to replace the outgoing player locally. The local update enforces matching FPL positions and the three-player club limit; re-import after the deadline to verify it against the official published squad.

## Updating data

Use **Refresh official data** in the browser or run:

```bash
python -m fpl_engine update
python -m fpl_engine project
python -m fpl_engine backtest
python -m fpl_engine chips
```

Useful commands:

```bash
python -m fpl_engine status
python -m fpl_engine validate
python -m fpl_engine import-team 123456 --bank 1.5 --free-transfers 2
python -m unittest discover -s tests -v
```

Use `--force` with `update` to bypass a fresh local cache. Cache files and SQLite state are stored under `data/` and are excluded from version control.

## Data sources

Phases 1–8 use public JSON endpoints served by the official Fantasy Premier League site:

- `/api/bootstrap-static/` for gameweeks, clubs, positions, players, prices, availability, ownership, season totals, official expected metrics, and set-piece order.
- `/api/fixtures/` for schedule, home/away teams, scores, status, and official difficulty values. The raw FDR values are stored as source data, not treated as the future projection model.
- `/api/entry/{team_id}/`, `/api/entry/{team_id}/history/`, and `/api/entry/{team_id}/event/{gw}/picks/` for public team identity, recorded chip use and published picks.
- `/api/event/{gw}/live/` for gameweek-level starts, minutes, points, xG/xA, BPS, saves, cards, and defensive contributions.

Each successful core-data and gameweek-history refresh is stored in one SQLite transaction. Invalid downloads never replace the last valid dataset. A projection is shown only when it belongs to the latest successful data sync, so a failed regeneration cannot silently pair old projections with a new player snapshot. Raw responses are cached with fetch timestamps and stale-cache fallback is clearly recorded.

## What the current numbers mean

The **Player data** page shows official source observations. The **Projections** page shows Phase 2 numerical forecasts and points-per-£1m over 1/3/5/6/8-gameweek horizons. Select a player to see the minutes distribution, capacity adjustment, per-gameweek fixtures, position-specific fixture score, team and player goal expectations, clean-sheet probabilities, and selected-horizon component decomposition.

Confidence combines minutes sample size, observed consistency, availability clarity, performance exposure, and—once enough completed rounds exist—measured reliability. Forecasts are only evaluated when they were saved before that gameweek's deadline. Calibration remains in **collecting** mode until at least 3 completed forecast gameweeks and 100 player-gameweeks exist; before then, it does not alter expected points and transfer confidence is capped. See [`docs/PHASE2_MODEL.md`](docs/PHASE2_MODEL.md) and [`docs/PHASE3_TRANSFERS_AND_CALIBRATION.md`](docs/PHASE3_TRANSFERS_AND_CALIBRATION.md).

The Phase 4 optimizer uses those forecasts without changing their means. Conservative, balanced and aggressive profiles change selection utility using floor/ceiling and confidence, while the displayed expected points remain the model's original estimates. See [`docs/PHASE4_SQUAD_OPTIMIZER.md`](docs/PHASE4_SQUAD_OPTIMIZER.md).

Phase 5 composes those existing result objects rather than creating a second set of calculations. See [`docs/PHASE5_DECISION_DASHBOARD.md`](docs/PHASE5_DECISION_DASHBOARD.md).

Phase 6 adds a deterministic, source-bounded language interface after the numerical engine. It does not alter rankings or projections. See [`docs/PHASE6_GROUNDED_ASSISTANT.md`](docs/PHASE6_GROUNDED_ASSISTANT.md).

Phase 7 extends the rolling foundation into a complete no-look-ahead evaluation report and manager-decision audit trail. See [`docs/PHASE7_BACKTESTING_AND_CALIBRATION.md`](docs/PHASE7_BACKTESTING_AND_CALIBRATION.md).

Phase 8 adds team-specific chip timing, legal one-week and rebuild optimizers, explainable preparation moves and an eight-Gameweek strategy simulator. See [`docs/PHASE8_AI_CHIP_PLANNER.md`](docs/PHASE8_AI_CHIP_PLANNER.md).
