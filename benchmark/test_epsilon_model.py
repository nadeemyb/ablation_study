#!/usr/bin/env python3
"""Offline verification of the consent-score -> normalized score -> epsilon model.

This mirrors HealthcareConsentDP_Final.sol fixed-point integer arithmetic so the
values reported in the manuscript can be reconstructed without a live chain.
"""
from __future__ import annotations
import math

SCALE = 1_000_000
EPS_MIN = 100_000
EPS_MAX = 1_000_000

PURPOSE = {
    "Research": 200_000,
    "Analysis": 500_000,
    "Both": 650_000,
    "Treatment": 800_000,
    "Commercial": 1_000_000,
}
ACCESS = {"Minor": 200_000, "Partial": 500_000, "Full": 1_000_000}
VALIDITY = {"lt_6_months": 100_000, "6_to_12_months": 500_000, "ge_12_months": 1_000_000}
SENSITIVITY = {"High": 100_000, "Medium": 500_000, "Low": 1_000_000}


def geometric_mean4(a: int, b: int, c: int, d: int) -> int:
    return math.isqrt(math.isqrt(a * b * c * d))


G_MIN = geometric_mean4(200_000, 200_000, 100_000, 100_000)
G_MAX = geometric_mean4(1_000_000, 1_000_000, 1_000_000, 1_000_000)


def derive_epsilon_scaled(p: int, a: int, t: int, s: int) -> tuple[int, int, int]:
    g = geometric_mean4(p, a, t, s)
    g_clamped = max(G_MIN, min(G_MAX, g))
    normalized = ((g_clamped - G_MIN) * SCALE) // (G_MAX - G_MIN)
    epsilon = EPS_MIN + ((EPS_MAX - EPS_MIN) * normalized) // SCALE
    return g, normalized, epsilon


def main() -> None:
    passed = 0
    assert G_MIN == 141_421, G_MIN
    assert G_MAX == 1_000_000, G_MAX
    passed += 2

    # Manuscript worked example: Research, Partial, 6-12 months, Medium.
    g, z, eps = derive_epsilon_scaled(
        PURPOSE["Research"], ACCESS["Partial"], VALIDITY["6_to_12_months"], SENSITIVITY["Medium"]
    )
    assert g == 397_635, g
    assert z == 298_416, z
    assert eps == 368_574, eps
    passed += 3

    # Feasible minimum and maximum map exactly to the operational epsilon range.
    _, _, eps_lo = derive_epsilon_scaled(200_000, 200_000, 100_000, 100_000)
    _, _, eps_hi = derive_epsilon_scaled(1_000_000, 1_000_000, 1_000_000, 1_000_000)
    assert eps_lo == EPS_MIN, eps_lo
    assert eps_hi == EPS_MAX, eps_hi
    passed += 2

    # Default bounded-mean query sensitivity in the benchmark.
    delta_f = (200.0 - 0.0) / 25
    assert delta_f == 8.0
    laplace_scale = delta_f / (eps / SCALE)
    assert abs(laplace_scale - 21.7055) < 0.01, laplace_scale
    passed += 2

    print(f"Epsilon-model tests: {passed} passed, 0 failed")
    print(f"G_MIN={G_MIN/SCALE:.6f}, G_MAX={G_MAX/SCALE:.6f}")
    print(
        "Worked example -> "
        f"g={g/SCALE:.6f}, normalized={z/SCALE:.6f}, epsilon={eps/SCALE:.6f}, "
        f"bounded-mean sensitivity={delta_f:.1f}, Laplace scale={laplace_scale:.4f}"
    )


if __name__ == "__main__":
    main()
