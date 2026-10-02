from __future__ import annotations

import json
import threading
import tempfile
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from fpl_engine.data.repository import Repository
from fpl_engine.web import make_handler


class VerificationTests(unittest.TestCase):
    def test_team_verification_checks_all_decision_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repository = Repository(Path(temp) / "fpl.sqlite3")
            squad = [
                {"id": index, "position_id": position, "team_id": index}
                for index, position in enumerate([1, 1, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 4, 4, 4], start=1)
            ]
            repository.profile = lambda: {"team_id": 3484868, "bank": 1.0, "free_transfers": 1, "current_gameweek": 6, "manager": {"bank": 1.0, "free_transfers": 1}}
            repository.squad = lambda: squad
            app = SimpleNamespace(
                repository=repository,
                service=SimpleNamespace(import_team=lambda *args, **kwargs: {"status": "ready"}),
                transfer_optimizer=SimpleNamespace(suggestions=lambda: {"status": "ready", "suggestions": []}),
                squad_optimizer=SimpleNamespace(optimize=lambda **_: {"status": "ready", "formation": "3-4-3", "squad": []}),
                chip_planner=SimpleNamespace(plan=lambda **_: {"status": "ready", "summary": {"headline": "Ready"}}),
                dashboard_service=SimpleNamespace(build=lambda **_: {"status": "ready", "planning_event": {"id": 4}, "manager": {"bank": 1.0, "free_transfers": 1}, "decision": {"title": "Do Nothing"}}),
                explanation_service=SimpleNamespace(suggestions=lambda: {"questions": ["What transfer should I make?"]}),
            )
            handler = make_handler(app)
            handler.log_message = lambda *_: None
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{server.server_port}/api/verification/team/3484868"
                ) as response:
                    payload = json.load(response)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

            self.assertTrue(payload["ok"])
            self.assertTrue(all(payload["verification"]["checks"].values()))
            self.assertEqual(payload["verification"]["position_counts"], {"1": 2, "2": 5, "3": 5, "4": 3})
            self.assertEqual(payload["verification"]["max_players_per_club"], 1)
            self.assertEqual(payload["verification"]["checks"]["current_event_present"], True)
            self.assertEqual(payload["verification"]["checks"]["bank_present"], True)
            self.assertEqual(payload["verification"]["checks"]["free_transfers_present"], True)
            self.assertEqual(payload["verification"]["checks"]["do_nothing_baseline_present"], True)
            self.assertEqual(payload["chip_plan"]["status"], "ready")
            self.assertTrue(payload["assistant"]["questions"])


if __name__ == "__main__":
    unittest.main()
