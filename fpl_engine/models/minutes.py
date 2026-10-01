from __future__ import annotations

from dataclasses import asdict, dataclass
from math import ceil, sqrt
from typing import Any


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


@dataclass(frozen=True)
class MinutesProjection:
    expected_minutes: float
    start_probability: float
    sixty_probability: float
    under_sixty_probability: float
    no_play_probability: float
    availability_factor: float
    rotation_risk: float
    confidence: float
    observed_team_games: int
    team_capacity_factor: float = 1.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def constrained(self, factor: float) -> "MinutesProjection":
        """Scale mutually exclusive appearance states to a club's 990-minute budget."""
        factor = clamp(factor, 0.0, 1.0)
        start = self.start_probability * factor
        sixty = self.sixty_probability * factor
        under = self.under_sixty_probability * factor
        return MinutesProjection(
            expected_minutes=self.expected_minutes * factor,
            start_probability=round(start, 4),
            sixty_probability=round(sixty, 4),
            under_sixty_probability=round(under, 4),
            no_play_probability=round(clamp(1.0 - sixty - under, 0.0, 1.0), 4),
            availability_factor=self.availability_factor,
            rotation_risk=round(1.0 - start, 4),
            confidence=self.confidence,
            observed_team_games=self.observed_team_games,
            team_capacity_factor=round(self.team_capacity_factor * factor, 4),
        )


class MinutesModel:
    """Beta-shrunk appearance model using official gameweek starts and minutes."""

    START_PRIOR = {1: 0.76, 2: 0.69, 3: 0.64, 4: 0.61}
    CAMEO_PRIOR = {1: 0.03, 2: 0.16, 3: 0.22, 4: 0.23}

    @staticmethod
    def _availability(player: dict[str, Any]) -> float:
        status = player.get("status")
        chance = player.get("chance_next")
        if status == "a":
            return 1.0
        if chance is not None:
            return clamp(float(chance) / 100.0, 0.0, 1.0)
        if status == "d":
            return 0.72
        if status in {"i", "s", "u", "n"}:
            return 0.03
        return 0.85

    def project(
        self,
        player: dict[str, Any],
        history: list[dict[str, Any]],
        scheduled_events: set[int] | dict[int, int],
    ) -> MinutesProjection:
        fixture_counts = (
            {int(event): 1 for event in scheduled_events}
            if isinstance(scheduled_events, set)
            else {int(event): max(1, int(count)) for event, count in scheduled_events.items()}
        )
        recent_events = sorted(fixture_counts)[-8:]
        history_by_event = {int(row["event_id"]): row for row in history}
        position = int(player["position_id"])
        weighted_games = 0.0
        weighted_starts = 0.0
        weighted_cameos = 0.0
        weighted_nonstarts = 0.0
        start_lengths: list[tuple[float, float]] = []
        minute_values: list[float] = []
        for age, event_id in enumerate(reversed(recent_events)):
            weight = 0.78**age
            game_count = fixture_counts[event_id]
            row = history_by_event.get(event_id, {})
            weighted_games += weight * game_count
            starts = min(game_count, max(0, int(row.get("starts") or 0)))
            total_minutes = max(0.0, float(row.get("minutes") or 0))
            reported_appearances = max(0, int(row.get("played") or 0))
            inferred_appearances = min(game_count, ceil(total_minutes / 90.0)) if total_minutes else 0
            appearances = min(game_count, max(starts, reported_appearances, inferred_appearances))
            cameos = max(0, appearances - starts)
            weighted_starts += weight * starts
            weighted_nonstarts += weight * (game_count - starts)
            weighted_cameos += weight * cameos
            per_game_minutes = total_minutes / game_count
            minute_values.extend([per_game_minutes] * game_count)
            if starts:
                estimated_start_minutes = clamp(
                    (total_minutes - cameos * min(28.0, per_game_minutes)) / starts,
                    0.0,
                    90.0,
                )
                start_lengths.extend([(weight, estimated_start_minutes)] * starts)

        prior_strength = 3.0
        start_prior = self.START_PRIOR.get(position, 0.64)
        raw_start = (weighted_starts + prior_strength * start_prior) / (weighted_games + prior_strength)
        availability = self._availability(player)
        start_probability = clamp(raw_start * availability, 0.0, 0.995)

        cameo_strength = 3.0
        cameo_prior = self.CAMEO_PRIOR.get(position, 0.18)
        cameo_given_no_start = (
            weighted_cameos + cameo_strength * cameo_prior
        ) / (weighted_nonstarts + cameo_strength)
        cameo_probability = (1.0 - raw_start) * cameo_given_no_start * availability

        sixty_strength = 3.0
        sixty_weight = sum(weight for weight, minutes in start_lengths if minutes >= 60)
        started_weight = sum(weight for weight, _ in start_lengths)
        sixty_given_start = (sixty_weight + sixty_strength * 0.86) / (started_weight + sixty_strength)
        sixty_probability = clamp(start_probability * sixty_given_start, 0.0, start_probability)
        under_sixty_probability = clamp(
            (start_probability - sixty_probability) + cameo_probability, 0.0, 1.0
        )
        no_play_probability = clamp(1.0 - sixty_probability - under_sixty_probability, 0.0, 1.0)

        long_minutes = 80.0
        if start_lengths:
            numerator = sum(weight * min(minutes, 90.0) for weight, minutes in start_lengths if minutes >= 60)
            denominator = sum(weight for weight, minutes in start_lengths if minutes >= 60)
            if denominator:
                long_minutes = clamp(numerator / denominator, 60.0, 90.0)
        expected_minutes = sixty_probability * long_minutes + under_sixty_probability * 28.0

        if len(minute_values) >= 2:
            mean = sum(minute_values) / len(minute_values)
            spread = sqrt(sum((value - mean) ** 2 for value in minute_values) / len(minute_values))
            consistency = 1.0 - clamp(spread / 45.0, 0.0, 1.0)
        else:
            consistency = 0.35
        observed_games = sum(fixture_counts[event] for event in recent_events)
        sample = observed_games / (observed_games + 5.0)
        availability_clarity = 1.0 if player.get("status") == "a" or player.get("chance_next") is not None else 0.55
        confidence = clamp(0.28 + 0.38 * sample + 0.19 * consistency + 0.1 * availability_clarity, 0.25, 0.9)
        return MinutesProjection(
            expected_minutes=round(expected_minutes, 1),
            start_probability=round(start_probability, 4),
            sixty_probability=round(sixty_probability, 4),
            under_sixty_probability=round(under_sixty_probability, 4),
            no_play_probability=round(no_play_probability, 4),
            availability_factor=round(availability, 4),
            rotation_risk=round(1.0 - start_probability, 4),
            confidence=round(confidence, 4),
            observed_team_games=observed_games,
        )
