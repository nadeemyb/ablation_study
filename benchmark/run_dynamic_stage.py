"""One separately measured E1/E2/E3 stage on a continuous deployed contract.

No reset/deploy occurs here. Stage outputs are immutable; interrupted active
transaction phases require diagnosis rather than blind replay.
"""
import json
import csv
import math
import os
from pathlib import Path
import time
import run_experiment as b
import test_epsilon_model as model

STAGE = os.environ['CONSENT_STAGE'].upper()
assert STAGE in ('E1', 'E2', 'E3')
PREVIOUS = Path(os.environ['PREVIOUS_STAGE_DIR']) if STAGE != 'E1' else None
b.STAGE_ORDER = ['preflight'] + (
    ['setup_requesters', 'prepare_source_ipfs', 'register_patients', 'create_policies']
    if STAGE == 'E1' else ['update_consent']) + [
    'verify_policy_updates', 'submit_requests', 'evaluate_requests',
    'verify_authorization', 'prepare_dp_release', 'dp_releases',
    'verify_dp_readback', 'write_outputs', 'complete']

def read(fn):
    return b._read_only_rpc_with_retry('dynamic-stage verification', lambda: fn.call())

def main():
    rows = b.load_workload()
    for row in rows:
        row['request_id'] = int(STAGE[1]) * 1_000_000 + row['patient_id']
    manifest = b.preflight(rows)
    manifest.update({'consent_stage': STAGE, 'dynamic_consent': True,
                     'dynamic_runner_sha256': b.file_sha256(Path(__file__)),
                     'stage_transactions_per_patient': 5 if STAGE == 'E1' else 4,
                     'consent_date_semantics': 'Duration relative to fixed benchmark reference, applied at stage policy creation',
                     'cumulative_gaussian_delta_basic_composition': int(STAGE[1])*b.GAUSSIAN_DELTA if b.DP_MECHANISM == 'Gaussian' else 0})
    prior = {}
    if PREVIOUS:
        previous_summary = json.loads((PREVIOUS / 'summary.json').read_text())
        for field in ('contract_address', 'contract_code_sha256', 'genesis_hash', 'dp_mechanism',
                      'n_records', 'workers', 'patient_data_sha256'):
            assert manifest[field] == previous_summary['manifest'][field], f'Prior-stage mismatch: {field}'
        prior = {r['patient_id_text']: r for r in b._json_restore(json.loads((PREVIOUS / 'checkpoints/rows.json').read_text()))}
        assert set(prior) == {r['patient_id_text'] for r in rows}
        for row in rows:
            old = prior[row['patient_id_text']]
            row['source_ipfs'] = old['source_ipfs']
            row['prior_policy_id'] = old['status']['policy_id']
            row['prior_remaining_budget'] = old['dp']['remaining_epsilon_budget_scaled']
            if not b.RESUME_RUN:
                assert read(b.contract.functions.getRemainingEpsilonBudget(row['patient_id'])) == row['prior_remaining_budget']
    last = 'preflight'
    if b.RESUME_RUN:
        rows, last = b.load_phase_boundary_checkpoint(rows, manifest, allow_dynamic_readonly=True)
        manifest['diagnostic_recovery'] = True
        cpu_file = b.OUT_DIR / 'cpu_usage.csv'
        if cpu_file.exists():
            with cpu_file.open(newline='') as fh:
                b.resource_monitor.samples = list(csv.DictReader(fh))
    else:
        b.checkpoint_complete_stage('preflight', rows, manifest)
    started = time.time()
    b._RUN_STARTED_MONO = time.perf_counter()
    b.resource_monitor.start()
    def stage(name, job):
        nonlocal last
        if b.stage_already_completed(name, last):
            print(f'SKIP completed checkpoint phase: {name}', flush=True)
            return
        b.checkpoint_mark_active(name, last)
        b.resource_monitor.set_phase(name)
        job()
        b.checkpoint_complete_stage(name, rows, manifest)
        last = name
    def parallel(name, fn):
        stage(name, lambda: b.parallel_phase(name, rows, fn))
    def update(row):
        b.send_tx(b.contract.functions.updateConsentParameters(row['patient_id'], b.PURPOSE[row['purpose']],
            b.ACCESS[row['access']], row['duration_seconds'], True, b.SENSITIVITY[row['sensitivity']]),
            'patient', 'update_consent', 'Atomic consent parameter update', row['patient_id_text'])
    def verify_policy(row):
        patient = read(b.contract.functions.patientRecords(row['patient_id']))
        assert int(patient[3]) == b.SENSITIVITY[row['sensitivity']]
        policy = read(b.contract.functions.policies(patient[8]))
        assert policy[2] == b.PURPOSE[row['purpose']] and policy[3] == b.ACCESS[row['access']]
        assert policy[4] and policy[5] and not policy[6]
        assert policy[9] - policy[8] == row['duration_seconds']
        if PREVIOUS:
            old = read(b.contract.functions.policies(row['prior_policy_id']))
            assert old[6] and not old[4] and not old[5], 'Previous policy not invalidated'
            assert read(b.contract.functions.getRemainingEpsilonBudget(row['patient_id'])) == row['prior_remaining_budget'], 'Update changed privacy budget'
        row['policy_update_verified'] = True
    def authorize(row):
        row['status'] = b.status_for_row(row)
        assert row['status']['status'] in ('Granted', 'PartialGranted')
        assert row['status']['ipfs_hash_verified'] is True
        assert row['status']['policy_id'] == read(b.contract.functions.patientRecords(row['patient_id']))[8]
    def prepare(row):
        b.prepare_dp_job(row)
        policy = read(b.contract.functions.policies(row['status']['policy_id']))
        timestamp = b._read_only_rpc_with_retry('dynamic epsilon timestamp', lambda: b.w3.eth.get_block('latest').timestamp)
        remaining = int(policy[9]) - timestamp
        validity = 100_000 if remaining < 180*86400 else 500_000 if remaining < 365*86400 else 1_000_000
        expected = model.derive_epsilon_scaled(model.PURPOSE[row['purpose']], model.ACCESS[row['access']], validity, model.SENSITIVITY[row['sensitivity']])[2]
        assert row['dp']['epsilon_scaled'] == expected, 'On-chain epsilon differs from independent equation'
        assert math.isfinite(row['dp']['sampled_noise'])
        if b.DP_MECHANISM != 'Exponential':
            assert abs(row['dp']['sampled_noise']) > 1e-12
        else:
            row['dp']['zero_noise_valid_discrete_selection'] = abs(row['dp']['sampled_noise']) <= 1e-12
        row['epsilon_equation_verified'] = True
    try:
        if STAGE == 'E1':
            stage('setup_requesters', b.ensure_requesters)
            parallel('prepare_source_ipfs', lambda r: r.update({'source_ipfs': b.prepare_source_ipfs(r)}))
            parallel('register_patients', b.register_job)
            parallel('create_policies', b.policy_job)
        else:
            parallel('update_consent', update)
        parallel('verify_policy_updates', verify_policy)
        parallel('submit_requests', b.request_job)
        parallel('evaluate_requests', b.evaluate_job)
        parallel('verify_authorization', authorize)
        parallel('prepare_dp_release', prepare)
        parallel('dp_releases', b.release_job)
        parallel('verify_dp_readback', b.verify_release_job)
    finally:
        b.resource_monitor.stop()
    finished = time.time()
    cpu = b.resource_monitor.summary()
    assert not b.metrics.failures
    assert not cpu['sampling_errors'], cpu['sampling_errors']
    expected_containers = {f'pbac-validator{i}' for i in range(1, 5)} | {'pbac-rpcnode', 'pbac-ipfs'}
    assert expected_containers <= set(cpu['by_container']), 'Incomplete CPU coverage'
    b.write_outputs(rows, manifest, started, finished, 5 if STAGE == 'E1' else 4)
    b.checkpoint_complete_stage('complete', rows, manifest)
    print(f'DYNAMIC STAGE {STAGE} PASSED', flush=True)

if __name__ == '__main__':
    main()
