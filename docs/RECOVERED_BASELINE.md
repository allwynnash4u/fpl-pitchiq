# Recovered PitchIQ baseline

The original PitchIQ project was recovered from the local Codex workspace on 2026-10-01.

## Verified locally
- 126 files recovered from the original archive.
- Python compilation passes.
- 88/88 automated tests pass.
- Runtime status reports a valid FPL dataset with 652 players, 20 teams, 38 events and 380 fixtures in the recovered SQLite snapshot.
- The recovered application is Phase 8: AI chip planning and season strategy.

## Core implementation recovered
- resilient official FPL data client/cache/validation
- position-aware projection model
- expected-minutes model
- fixture model
- legal transfer optimizer with Do Nothing baseline and hit economics
- legal squad optimizer
- decision dashboard
- grounded explanation engine
- decision ledger
- walk-forward backtesting/calibration
- chip planner
- browser UI and accessibility/design QA
- automated tests

## Evidence limitation
The recovered SQLite snapshot has four projection runs, all for GW4, and the current backtest report is still collecting with zero evaluated forecast gameweeks. Therefore no numerical model-accuracy or decision-success claim is supported yet.

The recovered data snapshot is also stale relative to the recovery date; the validator reports stale availability/news warnings. Refreshing official FPL data is required before using it for current-season decisions.

## Migration
The full cleaned source is preserved in the recovery ZIP supplied with this task. This repository branch is the migration staging point; generated Python bytecode is intentionally excluded.
