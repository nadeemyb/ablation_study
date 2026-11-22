#!/usr/bin/env python3
"""Offline self-tests for the DP noise generator. No Besu/IPFS required."""
from __future__ import annotations
import math, random, statistics, sys
import dp_engine

fail=[]
passed=0
def check(cond,msg):
    global passed
    print(('PASS ' if cond else 'FAIL ')+msg)
    if cond: passed += 1
    else: fail.append(msg)

# Distribution smoke tests: seeded so validation is reproducible.
rng=random.Random(20260914)
lap=[dp_engine.laplace_unit(rng) for _ in range(40000)]
check(abs(statistics.mean(lap)) < 0.04, f'Laplace unit mean near 0 ({statistics.mean(lap):.5f})')
check(abs(statistics.mean(abs(x) for x in lap)-1.0) < 0.04, f'Laplace E|X| near 1 ({statistics.mean(abs(x) for x in lap):.5f})')

rng=random.Random(20260914)
gauss=[dp_engine.standard_normal(rng) for _ in range(40000)]
check(abs(statistics.mean(gauss)) < 0.04, f'Gaussian mean near 0 ({statistics.mean(gauss):.5f})')
check(abs(statistics.pstdev(gauss)-1.0) < 0.04, f'Gaussian std near 1 ({statistics.pstdev(gauss):.5f})')

# Independent privacy-equation check for analytic Gaussian calibration.
def implied_delta(eps, sensitivity, sigma):
    phi=dp_engine.normal_cdf
    a=sensitivity/(2.0*sigma)-eps*sigma/sensitivity
    b=-sensitivity/(2.0*sigma)-eps*sigma/sensitivity
    return phi(a)-math.exp(eps)*phi(b)

for eps in [0.1,0.5,1.0]:
    target=1e-5
    sigma=dp_engine.analytic_gaussian_sigma(20.0,eps,target)
    got=implied_delta(eps,20.0,sigma)
    check(sigma>0, f'analytic Gaussian sigma positive at epsilon={eps} ({sigma:.6f})')
    check(abs(got-target) < 5e-10, f'analytic Gaussian privacy equation matches delta at epsilon={eps} ({got:.9g})')

# Every mechanism must actually perturb and stay within clipping bounds.
for mech in ['Laplace','Gaussian','Exponential']:
    a=dp_engine.privatize(true_value=120.0,epsilon=0.5,sensitivity=20.0,mechanism=mech,rng=random.Random(12345),lo=0.0,hi=200.0)
    b=dp_engine.privatize(true_value=120.0,epsilon=0.5,sensitivity=20.0,mechanism=mech,rng=random.Random(12345),lo=0.0,hi=200.0)
    check(a==b, f'{mech} deterministic under identical seeded benchmark RNG')
    check(0.0 <= a['released_value'] <= 200.0, f'{mech} released value respects declared bounds')
    check(abs(a['sampled_noise']) > 1e-12, f'{mech} generated non-zero perturbation ({a["sampled_noise"]:.6f})')

print(f'\nDP generator tests: {passed} passed, {len(fail)} failed')
if fail: sys.exit(1)
