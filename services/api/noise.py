"""Conservative interpretation of corroborated road-LDWN source intervals."""

import math
from dataclasses import dataclass

METHOD_VERSION = "road-ldwn-intervals-v1"
SOURCE_MIN = -24.139999389648438
NIGHT_METHOD_VERSION = "road-ln-intervals-v1"
NIGHT_SOURCE_MIN = -33.569999689999996


@dataclass(frozen=True)
class NoiseBand:
    lower: float | None
    upper: float | None
    label: str

    def conservative_score(self) -> float:
        # The >=80 class always scores zero under the frozen 45–75 rule.
        if self.upper is None:
            return 0.0
        return 100 * max(0.0, min(1.0, (75 - self.upper) / 30))


def source_band(lower: float, upper: float) -> NoiseBand | None:
    if not math.isfinite(lower) or not math.isfinite(upper):
        return None
    if math.isclose(lower, SOURCE_MIN, rel_tol=0, abs_tol=1e-6) and upper == 55:
        return NoiseBand(None, 55, "poniżej 55")
    if lower in {55, 60, 65, 70, 75} and upper == lower + 5:
        return NoiseBand(lower, upper, f"{lower:g}–<{upper:g}")
    if lower == upper == 80:
        return NoiseBand(80, None, "co najmniej 80")
    return None


def night_source_band(lower: float, upper: float) -> NoiseBand | None:
    """Road LN only: corroborated layer20 pairs and renderer, never LDWN classes."""
    if not math.isfinite(lower) or not math.isfinite(upper):
        return None
    if math.isclose(lower, NIGHT_SOURCE_MIN, rel_tol=0, abs_tol=1e-6) and upper == 50:
        return NoiseBand(None, 50, "poniżej 50")
    if lower in {50, 55, 60, 65, 70} and upper == lower + 5:
        return NoiseBand(lower, upper, f"{lower:g}–<{upper:g}")
    if lower == upper == 75:
        return NoiseBand(75, None, "co najmniej 75")
    return None
