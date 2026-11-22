"""Deterministic policy-factor sensitivity study; no live blockchain or DP releases."""
import csv
import hashlib
import itertools
import json
import platform
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'benchmark'))
import test_epsilon_model as model

FACTORS = dict(Purpose=model.PURPOSE, Access=model.ACCESS,
               Validity=model.VALIDITY, Sensitivity=model.SENSITIVITY)
NAMES = list(FACTORS)

def epsilon(scores):
    return model.derive_epsilon_scaled(*scores)[2] / model.SCALE

def integer_root(n):
    # Independent algorithm: exact fourth-root binary search.
    lo, hi = 0, 1_000_001
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if mid ** 4 <= n:
            lo = mid
        else:
            hi = mid
    return lo

def write_csv(path, rows):
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

def main():
    out = ROOT / 'results' / datetime.now(timezone.utc).strftime('ablation_%Y%m%dT%H%M%S_%fZ')
    out.mkdir(parents=True, exist_ok=False)
    grid = []
    for labels in itertools.product(*(list(f) for f in FACTORS.values())):
        scores = [FACTORS[name][label] for name, label in zip(NAMES, labels)]
        product = 1
        for score in scores:
            product *= score
        root = integer_root(product)
        expected = 100_000 + (900_000 * (((root - 141_421) * 1_000_000) // (1_000_000 - 141_421))) // 1_000_000
        assert model.derive_epsilon_scaled(*scores)[2] == expected
        grid.append(dict(zip(NAMES, labels), epsilon=epsilon(scores)))
    assert len(grid) == 135
    contrasts, summaries = [], []
    for i, (name, levels) in enumerate(FACTORS.items()):
        others = [n for n in NAMES if n != name]
        ranges = []
        for context in itertools.product(*(FACTORS[n] for n in others)):
            values = []
            for level in levels:
                config = dict(zip(others, context))
                config[name] = level
                val = epsilon([FACTORS[n][config[n]] for n in NAMES])
                contrasts.append(dict(factor=name, **config, epsilon=val))
                values.append(val)
            assert all(a < b for a, b in zip(values, values[1:]))
            ranges.append(max(values) - min(values))
        summaries.append(dict(factor=name, contexts=len(ranges), minimum_range=min(ranges),
                              mean_range=statistics.mean(ranges), maximum_range=max(ranges)))
    baseline_scores = {'Low-score context':[200_000,200_000,100_000,100_000],
                       'Middle-score context':[500_000]*4,
                       'High-score context':[1_000_000]*4}
    baseline_rows = []
    fig, axes = plt.subplots(2, 2, figsize=(11, 7), sharey=True)
    for i, (name, levels) in enumerate(FACTORS.items()):
        ax = axes.flat[i]
        for context, baseline in baseline_scores.items():
            values = []
            for level, score in levels.items():
                scores = baseline.copy()
                scores[i] = score
                val = epsilon(scores)
                values.append(val)
                baseline_rows.append(dict(context=context, factor=name, level=level,
                                          fixed_scores=json.dumps({n:baseline[j]/1e6 for j,n in enumerate(NAMES) if j != i}), epsilon=val))
            ax.plot(list(levels), values, marker='o', label=context)
        ax.set_title(name)
        ax.set_ylim(0.05, 1.05)
        ax.set_ylabel('Derived epsilon')
        ax.tick_params(axis='x', labelrotation=18)
        ax.grid(alpha=.25)
    axes.flat[0].legend(fontsize=8)
    fig.suptitle('CABDP policy-factor sensitivity (other scores held fixed)')
    fig.tight_layout()
    for ext in ('png','pdf','svg'):
        fig.savefig(out / ('factor_sensitivity.' + ext), dpi=220)
    plt.close(fig)
    write_csv(out/'full_grid.csv', grid)
    # Uniform fields: each contrast includes the same four policy labels.
    contrasts = [{k:r[k] for k in ['factor',*NAMES,'epsilon']} for r in contrasts]
    write_csv(out/'matched_factor_contrasts.csv', contrasts)
    write_csv(out/'factor_summary.csv', summaries)
    write_csv(out/'baseline_sweeps.csv', baseline_rows)
    report = ['# CABDP epsilon factor ablation', '',
              'All 135 authorized score combinations were evaluated. Each factor was varied across all contexts of the other factors. Every context was strictly increasing in the configured score. These are deterministic policy calculations; repetitions and significance tests are not appropriate.', '',
              '| Factor | Contexts | Minimum epsilon range | Mean range | Maximum range |',
              '|---|---:|---:|---:|---:|']
    for row in summaries:
        report.append(f"| {row['factor']} | {row['contexts']} | {row['minimum_range']:.6f} | {row['mean_range']:.6f} | {row['maximum_range']:.6f} |")
    report += ['', '## Interpretation and limits', '',
               'Increasing purpose/access/validity scores increases epsilon; moving from High to Low data sensitivity also increases epsilon. The mapping gives all four factors the same one-quarter exponent. Differences in observed ranges reflect their configured score ranges and the other scores; they do not establish a universal importance ranking or optimality of the policy scores. Larger epsilon represents a weaker DP guarantee for an otherwise fixed mechanism.', '',
               'The grid weights all policy combinations equally; it is not a population estimate from E1/E2/E3. No protected glucose data, noise samples, releases, cumulative budgets or on-chain authorization were exercised. Invalid/expired/revoked/None-access cases are excluded rather than interpreted as valid releases. Source inspection matched score mappings and arithmetic; an independent integer fourth-root algorithm matched all 135 Python outputs. Solidity was not compiled or executed. This checks arithmetic, not a formal privacy proof or independent peer review.', '',
               '## Draft reviewer response', '',
               'We added a controlled factor ablation of the existing privacy-parameter allocation rule. Purpose, access level, remaining consent validity and data sensitivity were varied individually while the other factors were fixed. The study covers all 135 policy combinations and reports matched factor ranges across all contexts, together with three illustrative baseline sweeps. The results demonstrate the direction and context-dependent magnitude of each factor’s effect on epsilon. We clarify that the scores are policy design choices and that the experiment evaluates parameter allocation rather than proving optimal scores or measuring individual privacy loss.', '',
               'This response is a draft for integration: the manuscript itself has not yet been edited.']
    (out/'REPORT.md').write_text('\n'.join(report), encoding='utf-8')
    sources = [Path(__file__), ROOT/'benchmark/test_epsilon_model.py', ROOT/'contracts/HealthcareConsentDP_Final.sol', *sorted((ROOT/'docs').glob('0[1-8]_*.md'))]
    manifest = dict(python=sys.version, executable=sys.executable, platform=platform.platform(),
                    matplotlib=matplotlib.__version__, timestamp_utc=datetime.now(timezone.utc).isoformat(),
                    checks={'grid_count':135,'independent_integer_arithmetic_matches':135,'monotone_contexts':sum(r['contexts'] for r in summaries)},
                    sources={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
                    outputs={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in out.iterdir() if p.is_file()})
    (out/'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(json.dumps({'output':str(out),'summary':summaries,'checks':manifest['checks']}, indent=2))

if __name__ == '__main__':
    main()
