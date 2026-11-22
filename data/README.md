# Data files

- `diabetes.csv` — 1,000 patient rows used by the benchmark.
- `consent1000.csv` — one matched consent row per patient (1,000 rows).

The performance consent file intentionally contains `consent_granted=True` and `revoked=False` for every row so all three consensus mechanisms execute the same successful transaction mix. Negative consent/revocation/expiry behavior is exercised separately by `benchmark/functional_tests.py`.

**Provenance requirement:** the package does not contain enough information to identify the authoritative bibliographic source/transformation history of `diabetes.csv`. Add the exact source citation and any synthesis/extension procedure to the manuscript/repository before publication. `consent1000.csv` should be described as the experiment's consent-policy dataset and its generation procedure should be documented if it was programmatically constructed.
