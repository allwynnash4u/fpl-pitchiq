from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    project_root: Path
    data_dir: Path
    cache_dir: Path
    database_path: Path
    api_base_url: str = "https://fantasy.premierleague.com/api"
    host: str = "127.0.0.1"
    port: int = 8765

    @classmethod
    def from_environment(cls) -> "Settings":
        project_root = Path(__file__).resolve().parent.parent
        data_dir = Path(os.environ.get("FPL_DATA_DIR", project_root / "data"))
        return cls(
            project_root=project_root,
            data_dir=data_dir,
            cache_dir=data_dir / "cache",
            database_path=Path(
                os.environ.get("FPL_DATABASE_PATH", data_dir / "fpl.sqlite3")
            ),
            api_base_url=os.environ.get(
                "FPL_API_BASE_URL", "https://fantasy.premierleague.com/api"
            ).rstrip("/"),
            host=os.environ.get("FPL_HOST", "127.0.0.1"),
            port=int(os.environ.get("FPL_PORT", "8765")),
        )

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

