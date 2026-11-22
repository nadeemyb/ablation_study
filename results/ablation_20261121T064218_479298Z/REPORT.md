# CABDP epsilon factor ablation

All 135 authorized score combinations were evaluated. Each factor was varied across all contexts of the other factors. Every context was strictly increasing in the configured score. These are deterministic policy calculations; repetitions and significance tests are not appropriate.

| Factor | Contexts | Minimum epsilon range | Mean range | Maximum range |
|---|---:|---:|---:|---:|
| Purpose | 27 | 0.073431 | 0.186411 | 0.347242 |
| Access | 45 | 0.073431 | 0.194012 | 0.347242 |
| Validity | 45 | 0.115374 | 0.267676 | 0.458775 |
| Sensitivity | 45 | 0.115374 | 0.267676 | 0.458775 |

## Interpretation and limits

Increasing purpose/access/validity scores increases epsilon; moving from High to Low data sensitivity also increases epsilon. The mapping gives all four factors the same one-quarter exponent. Differences in observed ranges reflect their configured score ranges and the other scores; they do not establish a universal importance ranking or optimality of the policy scores. Larger epsilon represents a weaker DP guarantee for an otherwise fixed mechanism.

The grid weights all policy combinations equally; it is not a population estimate from E1/E2/E3. No protected glucose data, noise samples, releases, cumulative budgets or on-chain authorization were exercised. Invalid/expired/revoked/None-access cases are excluded rather than interpreted as valid releases. Source inspection matched score mappings and arithmetic; an independent integer fourth-root algorithm matched all 135 Python outputs. Solidity was not compiled or executed. This checks arithmetic, not a formal privacy proof or independent peer review.

## Draft reviewer response

We added a controlled factor ablation of the existing privacy-parameter allocation rule. Purpose, access level, remaining consent validity and data sensitivity were varied individually while the other factors were fixed. The study covers all 135 policy combinations and reports matched factor ranges across all contexts, together with three illustrative baseline sweeps. The results demonstrate the direction and context-dependent magnitude of each factor’s effect on epsilon. We clarify that the scores are policy design choices and that the experiment evaluates parameter allocation rather than proving optimal scores or measuring individual privacy loss.

This response is a draft for integration: the manuscript itself has not yet been edited.