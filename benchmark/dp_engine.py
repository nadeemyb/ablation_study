#!/usr/bin/env python3
"""Differential-privacy mechanism primitives used by the Besu experiment.

The blockchain contract determines/charges epsilon. This module generates the
statistical perturbation in a trusted off-chain privacy engine. The perturbed
result (not the raw noise) is then committed to the smart contract.
"""
from __future__ import annotations

import bisect
import math
import random
from typing import Any


def laplace_unit(rng: random.Random) -> float:
    """Sample Laplace(0,1) using inverse CDF."""
    u = rng.random() - 0.5
    if u == 0.0:
        return 0.0
    return -math.copysign(1.0, u) * math.log(1.0 - 2.0 * abs(u))


def standard_normal(rng: random.Random) -> float:
    """Sample N(0,1) with Box-Muller."""
    u1 = max(rng.random(), 1e-15)
    u2 = rng.random()
    return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def analytic_gaussian_sigma(sensitivity: float, epsilon: float, delta: float, tol: float = 1e-12) -> float:
    """Balle-Wang (ICML 2018) analytic Gaussian calibration.

    This mirrors the authors' reference implementation. In particular the
    conversion from the binary-search variable s to alpha is
    sqrt(1+s/2) +/- sqrt(s/2), not sqrt(1+s) +/- sqrt(s).
    """
    if sensitivity < 0 or epsilon <= 0 or not (0 < delta < 1) or tol <= 0:
        raise ValueError("invalid analytic Gaussian parameters")
    if sensitivity == 0:
        return 0.0
    ee = math.exp(epsilon)

    def case_a(s: float) -> float:
        return normal_cdf(math.sqrt(epsilon * s)) - ee * normal_cdf(-math.sqrt(epsilon * (s + 2.0)))

    def case_b(s: float) -> float:
        return normal_cdf(-math.sqrt(epsilon * s)) - ee * normal_cdf(-math.sqrt(epsilon * (s + 2.0)))

    threshold = case_a(0.0)
    if abs(delta - threshold) <= tol:
        alpha = 1.0
    elif delta > threshold:
        lo, hi = 0.0, 1.0
        while case_a(hi) < delta:
            lo, hi = hi, 2.0 * hi
        for _ in range(200):
            mid = (lo + hi) / 2.0
            value = case_a(mid)
            if abs(value - delta) <= tol:
                lo = hi = mid
                break
            if value < delta:
                lo = mid
            else:
                hi = mid
        s = (lo + hi) / 2.0
        alpha = math.sqrt(1.0 + s / 2.0) - math.sqrt(s / 2.0)
    else:
        lo, hi = 0.0, 1.0
        while case_b(hi) > delta:
            lo, hi = hi, 2.0 * hi
        for _ in range(200):
            mid = (lo + hi) / 2.0
            value = case_b(mid)
            if abs(value - delta) <= tol:
                lo = hi = mid
                break
            if value > delta:
                lo = mid
            else:
                hi = mid
        s = (lo + hi) / 2.0
        alpha = math.sqrt(1.0 + s / 2.0) + math.sqrt(s / 2.0)

    return alpha * sensitivity / math.sqrt(2.0 * epsilon)


def exponential_sample(
    true_value: float,
    epsilon: float,
    sensitivity: float,
    rng: random.Random,
    lo: float,
    hi: float,
    candidates_count: int = 1001,
) -> float:
    """Discrete exponential mechanism with utility -|candidate-true_value|."""
    if epsilon <= 0 or sensitivity <= 0 or hi <= lo or candidates_count < 3:
        raise ValueError("invalid exponential mechanism parameters")
    step = (hi - lo) / (candidates_count - 1)
    candidates = [lo + i * step for i in range(candidates_count)]
    log_weights = [epsilon * (-abs(v - true_value)) / (2.0 * sensitivity) for v in candidates]
    maximum = max(log_weights)
    weights = [math.exp(x - maximum) for x in log_weights]
    total = sum(weights)
    cdf = []
    running = 0.0
    for weight in weights:
        running += weight / total
        cdf.append(running)
    cdf[-1] = 1.0
    idx = bisect.bisect_left(cdf, rng.random())
    return candidates[min(idx, len(candidates) - 1)]


def privatize(
    *,
    true_value: float,
    epsilon: float,
    sensitivity: float,
    mechanism: str,
    rng: random.Random,
    lo: float,
    hi: float,
    gaussian_delta: float = 1e-5,
    exponential_candidates: int = 1001,
) -> dict[str, Any]:
    if not (lo <= true_value <= hi):
        raise ValueError("true value outside declared clipping bounds")
    if epsilon <= 0 or sensitivity <= 0:
        raise ValueError("epsilon and sensitivity must be positive")

    if mechanism == "Laplace":
        scale = sensitivity / epsilon
        noise = scale * laplace_unit(rng)
        raw = true_value + noise
        released = max(lo, min(hi, raw))
        parameter_name, parameter = "laplace_scale", scale
    elif mechanism == "Gaussian":
        sigma = analytic_gaussian_sigma(sensitivity, epsilon, gaussian_delta)
        noise = sigma * standard_normal(rng)
        raw = true_value + noise
        released = max(lo, min(hi, raw))
        parameter_name, parameter = "gaussian_sigma", sigma
    elif mechanism == "Exponential":
        released = exponential_sample(true_value, epsilon, sensitivity, rng, lo, hi, exponential_candidates)
        noise = released - true_value
        raw = released
        parameter_name, parameter = "candidate_count", exponential_candidates
    else:
        raise ValueError(f"unsupported DP mechanism: {mechanism}")

    return {
        "true_value": true_value,
        "released_value": released,
        "sampled_noise": noise,
        "unclipped_value": raw,
        "was_clipped": raw < lo or raw > hi,
        "parameter_name": parameter_name,
        "parameter": parameter,
    }
