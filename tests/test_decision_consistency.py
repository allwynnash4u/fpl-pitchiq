from __future__ import annotations

import unittest

from fpl_engine.dashboard.service import DashboardService


class DecisionConsistencyTests(unittest.TestCase):
    def test_optimizer_hold_overrides_transfer_opportunity(self) -> None:
        transfers = {
            "status": "ready",
            "decision_safety": "estimate_only",
            "suggestions": [
                {
                    "action": "transfer",
                    "sell": {"id": 10, "name": "Palmer"},
                    "buy": {"id": 20, "name": "Groß"},
                    "net_expected_gain": 6.9,
                    "gain_3": 3.2,
                    "gain_6": 6.9,
                    "confidence": 0.65,
                    "why": "Higher expected minutes",
                }
            ],
        }
        optimized = {
            "status": "ready",
            "confidence": 0.65,
            "continuity_penalty_per_change": 2.1,
            "comparison": {
                "has_imported_squad": True,
                "players_kept": 15,
                "changes": 0,
                "transfers_out": [],
                "transfers_in": [],
            },
        }

        decision = DashboardService.final_decision(transfers, optimized)

        self.assertEqual(decision["action"], "hold")
        self.assertEqual(decision["title"], "Do Nothing / Roll")
        self.assertEqual(decision["source"], "squad_optimizer")
        self.assertEqual(decision["opportunity"]["sell"]["name"], "Palmer")

    def test_optimizer_single_change_uses_matching_transfer_evidence(self) -> None:
        transfer = {
            "action": "transfer",
            "sell": {"id": 10, "name": "Palmer"},
            "buy": {"id": 20, "name": "Groß"},
            "net_expected_gain": 6.9,
            "gain_3": 3.2,
            "gain_6": 6.9,
            "confidence": 0.65,
            "why": "Higher expected minutes",
        }
        transfers = {
            "status": "ready",
            "decision_safety": "estimate_only",
            "suggestions": [transfer],
        }
        optimized = {
            "status": "ready",
            "confidence": 0.65,
            "continuity_penalty_per_change": 2.1,
            "comparison": {
                "has_imported_squad": True,
                "players_kept": 14,
                "changes": 1,
                "transfers_out": [{"id": 10, "name": "Palmer"}],
                "transfers_in": [{"id": 20, "name": "Groß"}],
            },
        }

        decision = DashboardService.final_decision(transfers, optimized)

        self.assertEqual(decision["action"], "transfer")
        self.assertEqual(decision["title"], "Palmer → Groß")
        self.assertIs(decision["transfer"], transfer)
        self.assertEqual(decision["net_gain"], 6.9)


if __name__ == "__main__":
    unittest.main()
