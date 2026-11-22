# Consent-Aware Privacy-Parameter Ablation (CABDP)

Deterministic factor-wise **ablation and sensitivity analysis** of the dynamic
privacy-parameter (ε) derivation rule used in the Consent-Aware Blockchain–Differential
Privacy (**CABDP**) framework.

This study was added in response to a reviewer request to verify the individual
influence of each policy factor — **requester purpose, permitted access level,
remaining consent validity, and data sensitivity** — on the derived privacy
parameter ε. It is a deterministic, offline analysis: it does **not** execute a
live blockchain, generate DP noise, or release any data.

## What it computes

For the four normalized policy-score factors, ε is derived as:

```
g  = (Purpose · Access · Validity · Sensitivity) ^ (1/4)     # geometric mean
ĝ  = clip(g, G_min, G_max) normalized to [0, 1]              # G_min = 0.141421, G_max = 1.0
ε  = 0.10 + 0.90 · ĝ                                          # ε ∈ [0.10, 1.00]
```

A full factorial grid of **5 × 3 × 3 × 3 = 135** valid policy configurations is
evaluated. Each factor is then varied one at a time while the other three are held
fixed, producing **162** matched factor-specific contexts
(27 for Purpose + 45 each for Access, Validity, and Sensitivity).

## Repository layout

```
.
├── run_ablation.py                         # main script
├── benchmark/
│   └── test_epsilon_model.py               # ε-model (score tables + derive_epsilon_scaled)  [REQUIRED]
├── contracts/
│   └── HealthcareConsentDP_Final.sol        # Solidity contract (hashed for provenance)       [REQUIRED]
├── data/                                    # datasets (used by the benchmark; not by the ablation)
│   ├── diabetes.csv                         # public PIMA Indians Diabetes dataset
│   ├── consent1000.csv / e2consent1000.csv / e3consent1000.csv   # synthetic consent-policy profiles
│   └── README.md
├── docs/                                    # optional supporting documentation (01_*.md … 08_*.md)
├── results/                                 # outputs are written here (one timestamped folder per run)
├── EXPERIMENT_MATRIX.csv                    # factorial design reference
├── requirements.txt
└── LICENSE
```

> `run_ablation.py` adds `benchmark/` to the import path and `import`s
> `test_epsilon_model`; it also reads `contracts/HealthcareConsentDP_Final.sol`
> to record a provenance hash. **Both must be present** or the run will not
> complete. The `data/` CSVs feed the separate blockchain-performance benchmark,
> not this ablation.

## Requirements

- Python ≥ 3.12
- `matplotlib` (all other dependencies are in the Python standard library)

```bash
pip install -r requirements.txt
```

## How to run

```bash
python run_ablation.py
```

Each run creates a new `results/ablation_<UTC-timestamp>/` folder containing:

| File | Contents |
|---|---|
| `factor_summary.csv` | per-factor min / mean / max change in ε across its matched contexts |
| `matched_factor_contrasts.csv` | all matched one-factor-at-a-time contrasts |
| `full_grid.csv` | ε for every one of the 135 configurations |
| `baseline_sweeps.csv` | illustrative single-factor sweeps |
| `factor_sensitivity.(png\|svg\|pdf)` | four-panel factor-sensitivity figure |
| `manifest.json` | environment, consistency checks, and SHA-256 source/output hashes |
| `REPORT.md` | human-readable summary |

## Expected results (reproducibility check)

`factor_summary.csv` should reproduce the following mean Δε values exactly:

| Factor | Contexts | Mean Δε |
|---|---|---|
| Purpose | 27 | 0.186411 |
| Access | 45 | 0.194012 |
| Validity | 45 | 0.267676 |
| Sensitivity | 45 | 0.267676 |

`manifest.json` reports `grid_count = 135` and `monotone_contexts = 162`, and an
independent integer fourth-root algorithm matches all 135 ε values. These correspond
to Table 5-11 and Figure 5-11 of the dissertation.

## Data provenance

- `diabetes.csv` — the publicly available **PIMA Indians Diabetes Dataset**
  (e.g., Kaggle: https://www.kaggle.com/datasets/mathchi/diabetes-data-set). It
  contains no direct patient identifiers; identifiers used in the experiments are
  artificial.
- `consent*.csv` — **synthetically constructed** consent-policy profiles for the
  controlled benchmark; they do not represent consent collected from real patients.

## Citation

If you use this code, please cite the dissertation:

> Yaqub Nadeem. *Research on Key Technologies of Secure Medical Data Sharing Based
> on Blockchain.* Doctoral Dissertation, Beijing University of Technology, 2026.

## License

Released under the MIT License — see [LICENSE](LICENSE).
