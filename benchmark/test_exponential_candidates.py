"""Independent inverse-CDF checks, including valid zero perturbations."""
import math
import dp_engine

class FixedRng:
    def __init__(self, u): self.u = u
    def random(self): return self.u

def main():
    epsilon, sensitivity = 0.5, 8
    tail = math.exp(-epsilon*100/(2*sensitivity))
    p_tail = tail / (1+2*tail)
    cases = [(p_tail/2, 0), (0.5, 100), (1-p_tail/2, 200)]
    for u, expected in cases:
        got = dp_engine.exponential_sample(100, epsilon, sensitivity, FixedRng(u), 0, 200, 3)
        assert got == expected, (u, got, expected)
    release = dp_engine.privatize(true_value=100, epsilon=epsilon, sensitivity=sensitivity,
        mechanism='Exponential', rng=FixedRng(0.5), lo=0, hi=200, exponential_candidates=3)
    assert release['sampled_noise'] == 0 and release['released_value'] == 100
    print('Exponential independent CDF/candidate tests: 4 passed, 0 failed')

if __name__ == '__main__': main()
