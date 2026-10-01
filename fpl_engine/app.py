from __future__ import annotations

from dataclasses import dataclass

from fpl_engine.backtesting.service import BacktestService
from fpl_engine.config import Settings
from fpl_engine.data.cache import DiskJsonCache
from fpl_engine.data.client import FplApiClient
from fpl_engine.data.repository import Repository
from fpl_engine.data.service import DataService
from fpl_engine.models.service import ProjectionService
from fpl_engine.dashboard.service import DashboardService
from fpl_engine.explanations.service import ExplanationService
from fpl_engine.optimizer.transfer import TransferOptimizer
from fpl_engine.optimizer.squad import SquadOptimizer
from fpl_engine.chips.service import ChipPlannerService


@dataclass(frozen=True)
class Application:
    settings: Settings
    repository: Repository
    client: FplApiClient
    service: DataService
    projection_service: ProjectionService
    backtest_service: BacktestService
    transfer_optimizer: TransferOptimizer
    squad_optimizer: SquadOptimizer
    chip_planner: ChipPlannerService
    dashboard_service: DashboardService
    explanation_service: ExplanationService


def create_application(settings: Settings | None = None) -> Application:
    settings = settings or Settings.from_environment()
    settings.ensure_directories()
    repository = Repository(settings.database_path)
    client = FplApiClient(settings.api_base_url, DiskJsonCache(settings.cache_dir))
    service = DataService(client, repository)
    projection_service = ProjectionService(repository)
    backtest_service = BacktestService(repository, client)
    transfer_optimizer = TransferOptimizer(repository)
    squad_optimizer = SquadOptimizer(repository)
    chip_planner = ChipPlannerService(repository, squad_optimizer)
    dashboard_service = DashboardService(repository, transfer_optimizer, squad_optimizer)
    explanation_service = ExplanationService(repository, dashboard_service, chip_planner)
    return Application(
        settings,
        repository,
        client,
        service,
        projection_service,
        backtest_service,
        transfer_optimizer,
        squad_optimizer,
        chip_planner,
        dashboard_service,
        explanation_service,
    )
