from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fpl_engine.data.repository import Repository
from fpl_engine.models import MODEL_VERSION
from fpl_engine.models.projection import ProjectionEngine


@dataclass(frozen=True)
class ProjectionSummary:
    run_id: int
    model_version: str
    planning_event: int
    player_count: int
    history_rows: int
    calibration_status: str

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class ProjectionService:
    def __init__(self, repository: Repository, engine: ProjectionEngine | None = None):
        self.repository = repository
        self.engine = engine or ProjectionEngine()

    def generate(self) -> ProjectionSummary:
        inputs = self.repository.projection_inputs()
        if not inputs["players"] or not inputs["fixtures"]:
            raise ValueError("Install official player and fixture data before generating projections")
        projections, metadata = self.engine.project_all(inputs)
        run_id = self.repository.save_projection_run(
            model_version=MODEL_VERSION,
            planning_event=metadata["planning_event"],
            event_deadline_time=inputs.get("planning_event_deadline"),
            max_horizon=8,
            data_sync_id=inputs["data_sync_id"],
            notes=(
                "Transparent Phase 2 projection. Rolling evaluation only closes forecasts saved before "
                "each deadline; calibration is applied automatically after the minimum evidence threshold."
            ),
            projections=projections,
        )
        return ProjectionSummary(
            run_id=run_id,
            model_version=MODEL_VERSION,
            planning_event=metadata["planning_event"],
            player_count=metadata["player_count"],
            history_rows=metadata["history_rows"],
            calibration_status=metadata["calibration_status"],
        )
