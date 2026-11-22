#!/usr/bin/env python3
"""
Fair, phase-oriented PBAC + dynamic-consent + differential-privacy benchmark
for Hyperledger Besu (Clique / IBFT2 / QBFT).

Design goals:
  * identical transaction mix on every consensus network;
  * fresh contract state is mandatory by default;
  * real DP value handoff: epsilon is derived on-chain, noise is generated
    off-chain, the perturbed value is committed to the smart contract, and
    the stored value/hash/CID are read back and verified;
  * high enough concurrency to avoid a client-side serial bottleneck;
  * phase-level TPS, latency, gas, block utilization, and full audit output;
  * no original healthcare value or raw sampled noise is published to IPFS
    or blockchain. Raw/noise are retained only in local experimental output.

This is benchmarking code. In deterministic DP mode the random stream is
repeatable so all consensus mechanisms receive the exact same DP workload.
Do NOT use deterministic mode for production privacy releases.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import platform
import random
import subprocess
import sys
import statistics
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from web3.exceptions import Web3RPCError, BlockNotFound

import dp_engine


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
ABI_DIR = ROOT / "abi"
RESULTS_ROOT = Path(os.getenv("RESULTS_ROOT", str(ROOT / "results")))

BESU_RPC_URL = os.getenv("BESU_RPC_URL", "http://127.0.0.1:8545")
IPFS_API_URL = os.getenv("IPFS_API_URL", "http://127.0.0.1:5001/api/v0")
CONTRACT_ADDRESS = os.getenv("PBAC_CONTRACT_ADDRESS", "").strip()
ABI_FILE = Path(os.getenv(
    "PBAC_ABI_FILE",
    str(ABI_DIR / "HealthcareConsentDP_Final.minimal_abi.json"),
))
CHAIN_ID = int(os.getenv("BESU_CHAIN_ID", "1337"))
GAS_PRICE_GWEI = float(os.getenv("BESU_GAS_PRICE_GWEI", "0"))
EXPECTED_BESU_VERSION = os.getenv("BESU_EXPECTED_VERSION", "25.10.0").strip()
VALIDATOR_FILE = ROOT / "network" / "validator_addresses.json"

CONSENSUS = os.getenv("CONSENSUS", "QBFT").upper()
if CONSENSUS not in {"CLIQUE", "IBFT2", "QBFT"}:
    raise SystemExit("CONSENSUS must be CLIQUE, IBFT2, or QBFT")

N_RECORDS = int(os.getenv("N_RECORDS", "50"))
START_ROW = int(os.getenv("START_ROW", "0"))
WORKERS = max(1, int(os.getenv("WORKERS", "128")))
MAX_PENDING_PER_SENDER = max(1, int(os.getenv("MAX_PENDING_PER_SENDER", "512")))
TX_TIMEOUT_SECONDS = int(os.getenv("TX_TIMEOUT_SECONDS", "300"))
REQUIRE_FRESH_CONTRACT = os.getenv("REQUIRE_FRESH_CONTRACT", "true").lower() in {"1", "true", "yes", "y"}
VERIFY_IPFS = os.getenv("VERIFY_IPFS", "true").lower() in {"1", "true", "yes", "y"}
RESUME_RUN = os.getenv("RESUME_RUN", "false").lower() in {"1", "true", "yes", "y"}
CPU_SAMPLE_INTERVAL_SECONDS = max(1.0, float(os.getenv("CPU_SAMPLE_INTERVAL_SECONDS", "2")))

DP_MECHANISM = os.getenv("DP_MECHANISM", "Laplace").title()
if DP_MECHANISM not in {"Laplace", "Gaussian", "Exponential"}:
    raise SystemExit("DP_MECHANISM must be Laplace, Gaussian, or Exponential")
DP_RANDOM_MODE = os.getenv("DP_RANDOM_MODE", "deterministic").lower()
if DP_RANDOM_MODE not in {"deterministic", "secure"}:
    raise SystemExit("DP_RANDOM_MODE must be deterministic or secure")
DP_BENCHMARK_SEED = os.getenv("DP_BENCHMARK_SEED", "2026-final-benchmark")
DP_VALUE_SCALE = int(os.getenv("DP_VALUE_SCALE", "1000000"))
EPSILON_SCALE = 1_000_000
GLUCOSE_LO = float(os.getenv("DP_GLUCOSE_LO", "0"))
GLUCOSE_HI = float(os.getenv("DP_GLUCOSE_HI", "200"))
GLUCOSE_SENSITIVITY = float(os.getenv("DP_SENSITIVITY_GLUCOSE", "20"))
GAUSSIAN_DELTA = float(os.getenv("DP_GAUSSIAN_DELTA", "1e-5"))
EXP_CANDIDATES = max(3, int(os.getenv("DP_EXPONENTIAL_CANDIDATES", "1001")))
DP_QUERY_MODE = os.getenv("DP_QUERY_MODE", "bounded_mean").strip().lower()
if DP_QUERY_MODE not in {"bounded_mean", "scalar"}:
    raise SystemExit("DP_QUERY_MODE must be bounded_mean or scalar")
DP_COHORT_SIZE = max(2, int(os.getenv("DP_COHORT_SIZE", "25")))

BENCHMARK_REFERENCE_TIME = pd.Timestamp(os.getenv("BENCHMARK_REFERENCE_TIME", "2026-09-14T00:00:00Z"))
if BENCHMARK_REFERENCE_TIME.tzinfo is None:
    BENCHMARK_REFERENCE_TIME = BENCHMARK_REFERENCE_TIME.tz_localize("UTC")
else:
    BENCHMARK_REFERENCE_TIME = BENCHMARK_REFERENCE_TIME.tz_convert("UTC")
DETERMINISTIC_SOURCE_ARTIFACTS = os.getenv("DETERMINISTIC_SOURCE_ARTIFACTS", "true").lower() in {"1", "true", "yes", "y"}
SOURCE_ARTIFACT_SEED = os.getenv("SOURCE_ARTIFACT_SEED", "2026-final-source-artifacts")

PATIENT_DATA_FILE = Path(os.getenv("PATIENT_DATA_FILE", str(DATA_DIR / "diabetes.csv")))
CONSENT_DATA_FILE = Path(os.getenv("CONSENT_DATA_FILE", str(DATA_DIR / "consent1000.csv")))

RUN_LABEL = os.getenv("RUN_LABEL", "").strip()
CAMPAIGN_ID = os.getenv("CAMPAIGN_ID", "").strip() or None
RUN_ID = RUN_LABEL or f"{CONSENSUS.lower()}_{DP_MECHANISM.lower()}_n{N_RECORDS}_w{WORKERS}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
OUT_DIR = RESULTS_ROOT / RUN_ID
KEY_DIR = OUT_DIR / "keys"
CHECKPOINT_DIR = OUT_DIR / "checkpoints"
PROGRESS_FILE = CHECKPOINT_DIR / "progress.json"
ROWS_CHECKPOINT_FILE = CHECKPOINT_DIR / "rows.json"
METRICS_CHECKPOINT_FILE = CHECKPOINT_DIR / "metrics.json"
MANIFEST_CHECKPOINT_FILE = CHECKPOINT_DIR / "manifest.json"
LIVE_PROGRESS_FILE = CHECKPOINT_DIR / "live_progress.json"
if OUT_DIR.exists() and any(OUT_DIR.iterdir()) and not RESUME_RUN:
    raise SystemExit(
        f"Refusing to reuse non-empty result directory: {OUT_DIR}. "
        "Use a new RUN_LABEL/CAMPAIGN_ID, or set RESUME_RUN=true only for a valid phase-boundary checkpoint."
    )
if RESUME_RUN and not PROGRESS_FILE.exists():
    raise SystemExit(f"RESUME_RUN=true but checkpoint is missing: {PROGRESS_FILE}")
OUT_DIR.mkdir(parents=True, exist_ok=True)
KEY_DIR.mkdir(parents=True, exist_ok=True)
CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)

# Contract enums
ACCESS = {"None": 0, "Minor": 1, "Partial": 2, "Full": 3}
PURPOSE = {"Research": 0, "Analysis": 1, "Treatment": 2, "Both": 3, "Commercial": 4}
SENSITIVITY = {"Low": 0, "Medium": 1, "High": 2}
REQUESTER_ROLE = {
    "Unregistered": 0,
    "Clinician": 1,
    "Researcher": 2,
    "PublicHealthAnalyst": 3,
    "CommercialPartner": 4,
}
STATUS = {0: "Pending", 1: "Granted", 2: "PartialGranted", 3: "Denied"}
DENIAL_REASON = {
    0: "None", 1: "PatientDoesNotExist", 2: "NoActivePolicy",
    3: "PolicyInactive", 4: "PolicyRevoked", 5: "PolicyExpired",
    6: "PurposeMismatch", 7: "AccessLevelTooHigh", 8: "ConsentNotGranted",
    9: "RequesterNotRegistered", 10: "RoleNotPermittedForPurpose",
}
MECHANISM_CODE = {"Laplace": 0, "Gaussian": 1, "Exponential": 2}

# Development keys supplied by the user's existing Besu package. Override all
# of these by environment variable outside a local test network.
ACCOUNTS = {
    "controller": {
        "address": os.getenv("PBAC_CONTROLLER_ADDRESS", "0xed9d02e382b34818e88b88a309c7fe71e65f419d"),
        "private_key": os.getenv("PBAC_CONTROLLER_PRIVATE_KEY", "0xe6181caaffff94a09d7e332fc8da9884d99902c7874eb74354bdcadf411929f1"),
    },
    "patient": {
        "address": os.getenv("PBAC_PATIENT_ADDRESS", "0xf48de4a0c2939e62891f3c6aca68982975477e45"),
        "private_key": os.getenv("PBAC_PATIENT_PRIVATE_KEY", "0x7f012b2a11fc651c9a73ac13f0a298d89186c23c2c9a0e83206ad6e274ba3fc7"),
    },
    "researcher": {
        "address": os.getenv("PBAC_RESEARCHER_ADDRESS", "0x0886328869e4e1f401e1052a5f4aae8b45f42610"),
        "private_key": os.getenv("PBAC_RESEARCHER_PRIVATE_KEY", "0xf23f92ed543046498d7616807b18a8f304855cb644df25bc7d0b0b37d8a66019"),
    },
    "clinician": {
        "address": os.getenv("PBAC_CLINICIAN_ADDRESS", "0xf17f52151EbEF6C7334FAD080c5704D77216b732"),
        "private_key": os.getenv("PBAC_CLINICIAN_PRIVATE_KEY", "0xae6ae8e5ccbfb04590405997ee2d52d2b330726137b875053c36d94e974d162f"),
    },
    "commercial": {
        "address": os.getenv("PBAC_COMMERCIAL_ADDRESS", "0x627306090abaB3A6e1400e9345bC60c78a8BEf57"),
        "private_key": os.getenv("PBAC_COMMERCIAL_PRIVATE_KEY", "0xc87509a1c067bbde78beb793e6fa76530b6382a4c0241e5e4a9ec0a0f44dc0d3"),
    },
}

PURPOSE_TO_SIGNER = {
    "Research": ("researcher", "Researcher"),
    "Analysis": ("researcher", "Researcher"),
    "Both": ("researcher", "Researcher"),
    "Treatment": ("clinician", "Clinician"),
    "Commercial": ("commercial", "CommercialPartner"),
}


# ---------------------------------------------------------------------------
# Web3 / HTTP setup
# ---------------------------------------------------------------------------
session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=max(100, WORKERS * 4), pool_maxsize=max(100, WORKERS * 4))
session.mount("http://", adapter)
session.mount("https://", adapter)
w3 = Web3(Web3.HTTPProvider(BESU_RPC_URL, session=session, request_kwargs={"timeout": 300}))
w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

if not CONTRACT_ADDRESS:
    raise SystemExit("PBAC_CONTRACT_ADDRESS is required. Deploy HealthcareConsentDP_Final.sol first.")
if not ABI_FILE.exists():
    raise SystemExit(f"ABI file not found: {ABI_FILE}")
with ABI_FILE.open("r", encoding="utf-8") as fh:
    ABI = json.load(fh)
contract = w3.eth.contract(address=Web3.to_checksum_address(CONTRACT_ADDRESS), abi=ABI)


def checksum(addr: str) -> str:
    return Web3.to_checksum_address(addr)


# ---------------------------------------------------------------------------
# Consensus/network runtime verification
# ---------------------------------------------------------------------------
def rpc_result(method: str, params: list[Any] | None = None) -> Any:
    response = w3.provider.make_request(method, params or [])
    if isinstance(response, dict) and response.get("error"):
        raise RuntimeError(f"{method} RPC error: {response['error']}")
    if not isinstance(response, dict) or "result" not in response:
        raise RuntimeError(f"invalid RPC response for {method}: {response}")
    return response["result"]


def expected_validators() -> list[str]:
    if not VALIDATOR_FILE.exists():
        raise RuntimeError(f"validator declaration missing: {VALIDATOR_FILE}")
    obj = json.loads(VALIDATOR_FILE.read_text(encoding="utf-8"))
    vals = [str(v).lower() for v in obj.get("validators", [])]
    if len(vals) != 4 or len(set(vals)) != 4:
        raise RuntimeError(f"expected exactly four unique validator addresses, got {vals}")
    return vals


def consensus_validators() -> tuple[str, list[str]]:
    methods = {
        "CLIQUE": ("clique_getSigners", ["latest"]),
        "IBFT2": ("ibft_getValidatorsByBlockNumber", ["latest"]),
        "QBFT": ("qbft_getValidatorsByBlockNumber", ["latest"]),
    }
    method, params = methods[CONSENSUS]
    vals = [str(v).lower() for v in rpc_result(method, params)]
    return method, vals


def local_network_config() -> dict[str, Any]:
    genesis_path = ROOT / "network" / "config" / f"genesis-{CONSENSUS.lower()}.json"
    cfg_path = ROOT / "network" / "config" / "config.toml"
    d = json.loads(genesis_path.read_text(encoding="utf-8"))
    return {
        "genesis_path": str(genesis_path.relative_to(ROOT)),
        "genesis_sha256": hashlib.sha256(genesis_path.read_bytes()).hexdigest(),
        "runtime_config_sha256": hashlib.sha256(cfg_path.read_bytes()).hexdigest(),
        "block_period_seconds": int(d["config"][CONSENSUS.lower()]["blockperiodseconds"]),
        "declared_gas_limit": int(str(d["gasLimit"]), 16),
    }


def block_observability(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Observed block timing/utilization over blocks that contain workload txs."""
    if not records:
        return {}
    by_block: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        by_block[int(r["block_number"])].append(r)
    used = sorted(by_block)
    block_data = {}
    for n in used:
        b = w3.eth.get_block(n)
        block_data[n] = {
            "timestamp": int(b.timestamp),
            "gas_limit": int(b.gasLimit),
            "gas_used_chain": int(b.gasUsed),
            "workload_gas": sum(int(r["gas_used"]) for r in by_block[n]),
            "workload_tx": len(by_block[n]),
        }
    intervals = [block_data[b]["timestamp"] - block_data[a]["timestamp"] for a, b in zip(used, used[1:])]
    utilization = [block_data[n]["gas_used_chain"] / block_data[n]["gas_limit"] for n in used if block_data[n]["gas_limit"]]
    workload_util = [block_data[n]["workload_gas"] / block_data[n]["gas_limit"] for n in used if block_data[n]["gas_limit"]]
    return {
        "used_block_count": len(used),
        "first_used_block": used[0],
        "last_used_block": used[-1],
        "observed_used_block_interval_seconds": {
            "mean": statistics.mean(intervals) if intervals else 0.0,
            "median": statistics.median(intervals) if intervals else 0.0,
            "p95": pct(intervals, 0.95) if intervals else 0.0,
        },
        "chain_gas_utilization_used_blocks": {
            "mean": statistics.mean(utilization) if utilization else 0.0,
            "peak": max(utilization) if utilization else 0.0,
        },
        "workload_gas_utilization_used_blocks": {
            "mean": statistics.mean(workload_util) if workload_util else 0.0,
            "peak": max(workload_util) if workload_util else 0.0,
        },
    }


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def _json_safe_for_journal(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    if isinstance(value, dict):
        return {str(k): _json_safe_for_journal(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_for_journal(v) for v in value]
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return value


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


class Metrics:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.records: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []

    def add(self, rec: dict[str, Any]) -> None:
        with self.lock:
            self.records.append(rec)
            with (OUT_DIR / "transactions_journal.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(_json_safe_for_journal(rec), default=str) + "\n")

    def fail(self, rec: dict[str, Any]) -> None:
        with self.lock:
            self.failures.append(rec)
            with (OUT_DIR / "failures_journal.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(_json_safe_for_journal(rec), default=str) + "\n")

    @staticmethod
    def _group_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
        if not records:
            return {"count": 0, "window_seconds": 0, "tps": 0, "latency": {}, "gas": {}}
        submitted = [r["submitted_at"] for r in records]
        confirmed = [r["confirmed_at"] for r in records]
        lat = [r["latency_seconds"] for r in records]
        gas = [r["gas_used"] for r in records]
        window = max(confirmed) - min(submitted)
        submission_window = max(submitted) - min(submitted)
        confirmation_window = max(confirmed) - min(confirmed)
        queue_drain = max(confirmed) - max(submitted)
        blocks = [r["block_number"] for r in records]
        unique_blocks = sorted(set(blocks))
        counts = defaultdict(int)
        for b in blocks:
            counts[b] += 1
        return {
            "count": len(records),
            "window_seconds": window,
            "tps": len(records) / window if window > 0 else 0.0,
            "submission_window_seconds": submission_window,
            "submission_rate_tps": len(records) / submission_window if submission_window > 0 else 0.0,
            "confirmation_window_seconds": confirmation_window,
            "queue_drain_seconds": queue_drain,
            "latency": {
                "mean_seconds": statistics.mean(lat),
                "median_seconds": statistics.median(lat),
                "p95_seconds": pct(lat, 0.95),
                "p99_seconds": pct(lat, 0.99),
                "min_seconds": min(lat),
                "max_seconds": max(lat),
            },
            "gas": {
                "total": sum(gas),
                "mean": statistics.mean(gas),
                "min": min(gas),
                "max": max(gas),
            },
            "blocks": {
                "first": min(blocks),
                "last": max(blocks),
                "unique_count": len(unique_blocks),
                "mean_transactions_per_used_block": statistics.mean(counts.values()),
                "max_transactions_in_used_block": max(counts.values()),
            },
        }

    def summary(self) -> dict[str, Any]:
        setup = [r for r in self.records if r["phase"] == "setup"]
        workload = [r for r in self.records if r["phase"] != "setup"]
        by_phase: dict[str, Any] = {}
        for phase in sorted({r["phase"] for r in workload}):
            by_phase[phase] = self._group_summary([r for r in workload if r["phase"] == phase])
        by_type: dict[str, Any] = {}
        for tx_type in sorted({r["tx_type"] for r in workload}):
            by_type[tx_type] = self._group_summary([r for r in workload if r["tx_type"] == tx_type])
        active_window = sum(v["window_seconds"] for v in by_phase.values())
        first_last_window = 0.0
        if workload:
            first_last_window = max(r["confirmed_at"] for r in workload) - min(r["submitted_at"] for r in workload)
        return {
            "setup": self._group_summary(setup),
            "workload": self._group_summary(workload),
            "phase_summary": by_phase,
            "transaction_type_summary": by_type,
            "active_blockchain_window_seconds": active_window,
            "active_blockchain_tps": len(workload) / active_window if active_window > 0 else 0.0,
            "first_to_last_workload_window_seconds": first_last_window,
            "first_to_last_workload_tps": len(workload) / first_last_window if first_last_window > 0 else 0.0,
            "failures": list(self.failures),
        }


metrics = Metrics()


# ---------------------------------------------------------------------------
# Docker CPU/resource sampling
# ---------------------------------------------------------------------------
class DockerResourceMonitor:
    """Sample every PBAC container without affecting transaction accounting."""

    def __init__(self, interval_seconds: float) -> None:
        self.interval_seconds = interval_seconds
        self.samples: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.started_mono = 0.0
        self.phase = "preflight"
        self.lock = threading.Lock()
        self.csv_path = OUT_DIR / "cpu_usage.csv"

    @staticmethod
    def _percent(value: Any) -> float:
        try:
            return float(str(value).strip().removesuffix("%"))
        except (TypeError, ValueError):
            return 0.0

    def set_phase(self, phase: str) -> None:
        with self.lock:
            self.phase = phase

    def start(self) -> None:
        # Diagnostic resumes must sample new phases as well as preserve
        # historical rows. RESUME_RUN still excludes timing from publication.
        self.started_mono = time.perf_counter()
        self.thread = threading.Thread(target=self._run, name="docker-resource-monitor", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=50)

    def _append_csv(self, rows: list[dict[str, Any]]) -> None:
        fields = ["timestamp_utc", "elapsed_seconds", "phase", "container", "container_id", "cpu_percent", "memory_percent", "memory_usage", "pids"]
        exists = self.csv_path.exists()
        with self.csv_path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            if not exists:
                writer.writeheader()
            writer.writerows(rows)

    def _sample(self) -> None:
        proc = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=45,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "docker stats failed").strip())
        timestamp = datetime.now(timezone.utc).isoformat()
        elapsed = time.perf_counter() - self.started_mono
        with self.lock:
            phase = self.phase
        rows = []
        for line in proc.stdout.splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            name = str(item.get("Name", ""))
            if not name.startswith("pbac-"):
                continue
            rows.append({
                "timestamp_utc": timestamp,
                "elapsed_seconds": elapsed,
                "phase": phase,
                "container": name,
                "container_id": item.get("ID"),
                "cpu_percent": self._percent(item.get("CPUPerc")),
                "memory_percent": self._percent(item.get("MemPerc")),
                "memory_usage": item.get("MemUsage"),
                "pids": item.get("PIDs"),
            })
        if rows:
            self.samples.extend(rows)
            self._append_csv(rows)

    def _run(self) -> None:
        next_sample = time.perf_counter()
        while not self.stop_event.is_set():
            try:
                self._sample()
            except Exception as exc:
                self.errors.append(f"{type(exc).__name__}: {exc}")
            next_sample += self.interval_seconds
            now = time.perf_counter()
            if next_sample < now:
                next_sample = now
            self.stop_event.wait(max(0.0, next_sample - now))

    @staticmethod
    def _stats(values: list[float]) -> dict[str, float | int]:
        return {
            "samples": len(values),
            "mean_percent": statistics.mean(values) if values else 0.0,
            "p95_percent": pct(values, 0.95) if values else 0.0,
            "max_percent": max(values) if values else 0.0,
        }

    def summary(self) -> dict[str, Any]:
        by_container: dict[str, list[float]] = defaultdict(list)
        aggregate_by_timestamp: dict[str, float] = defaultdict(float)
        phase_all_by_timestamp: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        phase_validator_by_timestamp: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for row in self.samples:
            cpu = float(row["cpu_percent"])
            by_container[row["container"]].append(cpu)
            aggregate_by_timestamp[row["timestamp_utc"]] += cpu
            phase_all_by_timestamp[row["phase"]][row["timestamp_utc"]] += cpu
            if str(row["container"]).startswith("pbac-validator"):
                phase_validator_by_timestamp[row["phase"]][row["timestamp_utc"]] += cpu
        validators = [r for r in self.samples if str(r["container"]).startswith("pbac-validator")]
        validator_by_timestamp: dict[str, float] = defaultdict(float)
        for row in validators:
            validator_by_timestamp[row["timestamp_utc"]] += float(row["cpu_percent"])
        return {
            "sample_interval_seconds": self.interval_seconds,
            "docker_cpu_percent_definition": "100 percent equals one fully utilized logical CPU; totals may exceed 100 percent on multicore hosts.",
            "all_pbac_containers_aggregate": self._stats(list(aggregate_by_timestamp.values())),
            "validators_aggregate": self._stats(list(validator_by_timestamp.values())),
            "by_container": {name: self._stats(values) for name, values in sorted(by_container.items())},
            "by_phase": {
                phase: {
                    "all_pbac_containers_aggregate": self._stats(list(values.values())),
                    "validators_aggregate": self._stats(list(phase_validator_by_timestamp[phase].values())),
                }
                for phase, values in sorted(phase_all_by_timestamp.items())
            },
            "sampling_errors": self.errors,
            "raw_csv": "cpu_usage.csv",
        }


resource_monitor = DockerResourceMonitor(CPU_SAMPLE_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Crash-safe phase checkpoints
# ---------------------------------------------------------------------------
# A benchmark run is resumable only from a *completed phase boundary*.  This is
# deliberate: continuing a performance measurement after a crash in the middle
# of a transaction-producing phase would mix failure downtime into latency/TPS
# and can leave partially applied on-chain state.  The campaign wrappers keep
# all completed independent runs and restart only the interrupted run when a
# crash occurred mid-phase.
STAGE_ORDER = [
    "preflight",
    "setup_requesters",
    "prepare_source_ipfs",
    "register_patients",
    "create_policies",
    "submit_requests",
    "evaluate_requests",
    "verify_authorization",
    "prepare_dp_release",
    "dp_releases",
    "verify_dp_readback",
    "write_outputs",
    "complete",
]


def _json_safe(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return {"__bytes_hex__": bytes(value).hex()}
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, pd.Timestamp):
        return {"__timestamp__": value.isoformat()}
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return value


def _json_restore(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"__bytes_hex__"}:
            return bytes.fromhex(value["__bytes_hex__"])
        if set(value) == {"__timestamp__"}:
            return pd.Timestamp(value["__timestamp__"])
        return {k: _json_restore(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_restore(v) for v in value]
    return value


def _atomic_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(_json_safe(obj), indent=2, default=str), encoding="utf-8")
    # Windows readers/scanners can briefly deny rename/delete sharing. Keep
    # both the old checkpoint and fully written temporary file intact while
    # retrying; never fall back to truncating the destination.
    for attempt in range(21):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:
            if os.name != 'nt' or getattr(exc, 'winerror', None) not in (5, 32, 33) or attempt == 20:
                raise
            time.sleep(min(0.05 * (attempt + 1), 0.5))


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return "--:--:--"
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"


def _overall_fraction(stage: str, phase_fraction: float) -> float:
    # Approximate overall run progress. Each phase receives one equal progress
    # slice; phase ETA is the more reliable local estimate.
    try:
        idx = STAGE_ORDER.index(stage)
    except ValueError:
        return 0.0
    denom = max(1, len(STAGE_ORDER) - 1)
    return min(1.0, max(0.0, (idx + min(1.0, max(0.0, phase_fraction))) / denom))


def write_live_progress(stage: str, completed: int, total: int, phase_started: float, run_started: float, *, status: str = "running") -> dict[str, Any]:
    now = time.perf_counter()
    total = max(1, int(total))
    completed = min(total, max(0, int(completed)))
    frac = completed / total
    phase_elapsed = max(0.0, now - phase_started)
    rate = completed / phase_elapsed if phase_elapsed > 0 and completed > 0 else 0.0
    phase_eta = (total - completed) / rate if rate > 0 else None
    overall_frac = _overall_fraction(stage, frac)
    run_elapsed = max(0.0, now - run_started)
    overall_eta = (run_elapsed / overall_frac - run_elapsed) if overall_frac > 0.01 else None
    obj = {
        "run_id": RUN_ID,
        "campaign_id": CAMPAIGN_ID,
        "status": status,
        "stage": stage,
        "completed_items": completed,
        "total_items": total,
        "phase_percent": round(frac * 100.0, 2),
        "approx_overall_percent": round(overall_frac * 100.0, 2),
        "items_per_second": rate,
        "phase_elapsed_seconds": phase_elapsed,
        "phase_eta_seconds": phase_eta,
        "run_elapsed_seconds": run_elapsed,
        "approx_run_eta_seconds": overall_eta,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "eta_note": "Phase ETA is based on observed completion rate in the current phase. Overall ETA is approximate because phases have different costs.",
    }
    _atomic_json(LIVE_PROGRESS_FILE, obj)
    return obj


def _print_progress(obj: dict[str, Any]) -> None:
    print(
        f"{obj['stage']}: {obj['completed_items']}/{obj['total_items']} "
        f"({obj['phase_percent']:.1f}%) | {obj['items_per_second']:.2f} items/s | "
        f"elapsed {_fmt_duration(obj['phase_elapsed_seconds'])} | "
        f"phase ETA {_fmt_duration(obj['phase_eta_seconds'])} | "
        f"overall ~{obj['approx_overall_percent']:.1f}% | "
        f"run ETA ~{_fmt_duration(obj['approx_run_eta_seconds'])}",
        flush=True,
    )


def _write_partial_transactions() -> None:
    path = OUT_DIR / "transactions_partial.csv"
    fields = ["phase", "tx_type", "label", "sender_role", "sender", "tx_hash", "block_number", "gas_used", "submitted_at", "confirmed_at", "latency_seconds"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for rec in metrics.records:
            writer.writerow({k: rec.get(k) for k in fields})


def checkpoint_mark_active(stage: str, last_completed_stage: str | None) -> None:
    _atomic_json(PROGRESS_FILE, {
        "run_id": RUN_ID,
        "campaign_id": CAMPAIGN_ID,
        "status": "running",
        "last_completed_stage": last_completed_stage,
        "active_stage": stage,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "resume_policy": "phase_boundary_only",
        "publication_note": "If a crash occurs while active_stage is non-null, restart only this independent run from a fresh chain for valid performance metrics.",
    })


def checkpoint_complete_stage(stage: str, rows: list[dict[str, Any]], manifest: dict[str, Any]) -> None:
    _atomic_json(ROWS_CHECKPOINT_FILE, rows)
    _atomic_json(METRICS_CHECKPOINT_FILE, {"records": metrics.records, "failures": metrics.failures})
    _atomic_json(MANIFEST_CHECKPOINT_FILE, manifest)
    _write_partial_transactions()
    _atomic_json(PROGRESS_FILE, {
        "run_id": RUN_ID,
        "campaign_id": CAMPAIGN_ID,
        "status": "complete" if stage == "complete" else "checkpointed",
        "last_completed_stage": stage,
        "active_stage": None,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "resume_policy": "phase_boundary_only",
    })


def _stable_resume_fields() -> list[str]:
    return [
        "run_id", "campaign_id", "consensus_label", "chain_id", "genesis_hash",
        "contract_address", "contract_code_sha256", "n_records", "start_row", "workers",
        "max_pending_per_sender", "dp_mechanism", "dp_random_mode", "dp_benchmark_seed_sha256",
        "benchmark_reference_time", "dp_query_mode", "dp_cohort_size", "dp_glucose_lower_bound",
        "dp_glucose_upper_bound", "dp_scalar_sensitivity_glucose", "dp_value_scale", "epsilon_scale",
        "gaussian_delta", "exponential_candidates", "patient_data_sha256", "consent_data_sha256",
        "workload_sha256", "cpu_sample_interval_seconds",
    ]


def load_phase_boundary_checkpoint(base_rows: list[dict[str, Any]], current_manifest: dict[str, Any], allow_dynamic_readonly: bool = False) -> tuple[list[dict[str, Any]], str]:
    progress = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
    readback_only = progress.get("active_stage") == "verify_dp_readback" and progress.get("last_completed_stage") == "dp_releases"
    readback_only = readback_only or (allow_dynamic_readonly and progress.get('active_stage') in {'verify_authorization', 'verify_policy_updates'})
    # Preparation reads state and adds deterministic content-addressed IPFS
    # payloads, but submits no transactions or budget deductions. Restore
    # the prior authorization checkpoint and regenerate only preparation.
    readback_only = readback_only or (allow_dynamic_readonly and progress.get('active_stage') == 'prepare_dp_release' and progress.get('last_completed_stage') == 'verify_authorization')
    if progress.get("active_stage") and not readback_only:
        raise RuntimeError(
            "The previous process stopped during active stage "
            f"{progress['active_stage']!r}. Mid-phase continuation is intentionally refused because it can "
            "invalidate TPS/latency and leave partial on-chain state. Restart only this independent run "
            "from a fresh chain; completed campaign runs remain preserved."
        )
    last = str(progress.get("last_completed_stage") or "")
    if last not in STAGE_ORDER:
        raise RuntimeError(f"invalid checkpoint stage: {last!r}")
    saved_manifest = json.loads(MANIFEST_CHECKPOINT_FILE.read_text(encoding="utf-8"))
    for field in _stable_resume_fields():
        if saved_manifest.get(field) != current_manifest.get(field):
            raise RuntimeError(
                f"resume safety check failed for {field}: saved={saved_manifest.get(field)!r}, "
                f"current={current_manifest.get(field)!r}"
            )
    saved_rows = _json_restore(json.loads(ROWS_CHECKPOINT_FILE.read_text(encoding="utf-8")))
    if [r["patient_id_text"] for r in saved_rows] != [r["patient_id_text"] for r in base_rows]:
        raise RuntimeError("resume workload patient ordering differs from the checkpoint")
    saved_metrics = _json_restore(json.loads(METRICS_CHECKPOINT_FILE.read_text(encoding="utf-8")))
    metrics.records = list(saved_metrics.get("records", []))
    metrics.failures = list(saved_metrics.get("failures", []))
    print(f"Resuming run {RUN_ID} from completed phase: {last}", flush=True)
    return saved_rows, last


def stage_already_completed(stage: str, last_completed: str | None) -> bool:
    if not last_completed:
        return False
    return STAGE_ORDER.index(stage) <= STAGE_ORDER.index(last_completed)


# ---------------------------------------------------------------------------
# Thread-safe nonce manager
# ---------------------------------------------------------------------------
class NonceManager:
    def __init__(self) -> None:
        self.global_lock = threading.Lock()
        self.submit_locks: dict[str, threading.Lock] = {}
        self.semaphores: dict[str, threading.BoundedSemaphore] = {}
        self.next_nonce: dict[str, int] = {}

    def _ensure(self, address: str) -> None:
        with self.global_lock:
            if address not in self.submit_locks:
                self.submit_locks[address] = threading.Lock()
                self.semaphores[address] = threading.BoundedSemaphore(MAX_PENDING_PER_SENDER)

    def acquire(self, address: str) -> None:
        self._ensure(address)
        self.semaphores[address].acquire()

    def release(self, address: str) -> None:
        self.semaphores[address].release()

    def lock(self, address: str) -> threading.Lock:
        self._ensure(address)
        return self.submit_locks[address]

    def get_and_increment(self, address: str) -> int:
        if address not in self.next_nonce:
            self.next_nonce[address] = w3.eth.get_transaction_count(address, "pending")
        n = self.next_nonce[address]
        self.next_nonce[address] = n + 1
        return n

    def resync(self, address: str) -> None:
        self.next_nonce[address] = w3.eth.get_transaction_count(address, "pending")


nonce_mgr = NonceManager()


def raw_signed(signed: Any) -> bytes:
    return getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")


def send_tx(fn: Any, signer_key: str, phase: str, tx_type: str, label: str) -> Any:
    acct = ACCOUNTS[signer_key]
    addr = checksum(acct["address"])
    acquired = False
    submitted_at = None
    tx_hash = None
    try:
        try:
            gas_est = int(fn.estimate_gas({"from": addr}))
            gas_limit = gas_est + max(50_000, gas_est // 5)
        except Exception:
            gas_limit = 2_500_000

        nonce_mgr.acquire(addr)
        acquired = True
        with nonce_mgr.lock(addr):
            nonce = nonce_mgr.get_and_increment(addr)
            tx = fn.build_transaction({
                "chainId": CHAIN_ID,
                "from": addr,
                "gas": gas_limit,
                "gasPrice": w3.to_wei(GAS_PRICE_GWEI, "gwei"),
                "nonce": nonce,
            })
            signed = w3.eth.account.sign_transaction(tx, acct["private_key"])
            submitted_at = time.time()
            try:
                tx_hash = w3.eth.send_raw_transaction(raw_signed(signed))
            except Exception:
                nonce_mgr.resync(addr)
                raise
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=TX_TIMEOUT_SECONDS)
        confirmed_at = time.time()
        if int(receipt.status) != 1:
            raise RuntimeError(f"transaction reverted: {tx_hash.hex()}")
        metrics.add({
            "phase": phase,
            "tx_type": tx_type,
            "label": label,
            "sender_role": signer_key,
            "sender": addr,
            "tx_hash": tx_hash.hex(),
            "block_number": int(receipt.blockNumber),
            "gas_used": int(receipt.gasUsed),
            "submitted_at": submitted_at,
            "confirmed_at": confirmed_at,
            "latency_seconds": confirmed_at - submitted_at,
        })
        return receipt
    except Exception as exc:
        metrics.fail({"phase": phase, "tx_type": tx_type, "label": label, "error": f"{type(exc).__name__}: {exc}"})
        raise
    finally:
        if acquired:
            nonce_mgr.release(addr)


# ---------------------------------------------------------------------------
# Input and consent helpers
# ---------------------------------------------------------------------------
def patient_uint(patient_id: str) -> int:
    digits = "".join(ch for ch in str(patient_id) if ch.isdigit())
    if not digits:
        digest = hashlib.sha256(str(patient_id).encode()).digest()
        return int.from_bytes(digest[:8], "big") or 1
    return int(digits)


def parse_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"1", "true", "yes", "y"}


def policy_duration_seconds(valid_until: Any) -> int:
    """Return a deterministic benchmark duration from a fixed study reference time.

    Using wall-clock ``now`` makes the contract calldata differ across Clique,
    IBFT2, and QBFT runs that start minutes apart. A fixed reference makes the
    same consent row generate the exact same duration on every network.
    """
    dt = pd.to_datetime(valid_until, dayfirst=True, utc=True)
    seconds = int((dt - BENCHMARK_REFERENCE_TIME).total_seconds())
    if seconds <= 0:
        raise ValueError(
            f"consent is not valid relative to BENCHMARK_REFERENCE_TIME={BENCHMARK_REFERENCE_TIME}: "
            f"valid_until={valid_until}"
        )
    return seconds


def load_workload() -> list[dict[str, Any]]:
    patients = pd.read_csv(PATIENT_DATA_FILE)
    consent = pd.read_csv(CONSENT_DATA_FILE)
    required_patient = {"patient_id", "Glucose"}
    required_consent = {"patient_id", "consent_granted", "purpose", "access_level", "valid_until", "revoked", "sensitivity"}
    if missing := required_patient.difference(patients.columns):
        raise ValueError(f"patient dataset missing columns: {sorted(missing)}")
    if missing := required_consent.difference(consent.columns):
        raise ValueError(f"consent dataset missing columns: {sorted(missing)}")
    if patients["patient_id"].duplicated().any() or consent["patient_id"].duplicated().any():
        raise ValueError("patient_id must be unique in both datasets")

    merged = patients.merge(consent, on="patient_id", how="left", validate="one_to_one", suffixes=("", "_consent"))
    if merged[list(required_consent - {"patient_id"})].isna().any().any():
        raise ValueError("one or more patient records do not have a complete consent row")
    if START_ROW < 0 or N_RECORDS <= 0 or START_ROW + N_RECORDS > len(merged):
        raise ValueError(f"requested rows [{START_ROW}, {START_ROW + N_RECORDS}) exceed dataset size {len(merged)}")
    selected = merged.iloc[START_ROW:START_ROW + N_RECORDS].copy()
    all_glucose = [max(GLUCOSE_LO, min(GLUCOSE_HI, float(v))) for v in patients["Glucose"].tolist()]
    if DP_QUERY_MODE == "bounded_mean" and len(all_glucose) % DP_COHORT_SIZE != 0:
        raise ValueError(
            f"For deterministic bounded-mean buckets, dataset size {len(all_glucose)} must be divisible "
            f"by DP_COHORT_SIZE={DP_COHORT_SIZE}"
        )

    rows = []
    for idx, row in selected.iterrows():
        purpose = str(row["purpose"]).strip().title()
        access = str(row["access_level"]).strip().title()
        sensitivity = str(row["sensitivity"]).strip().title()
        if purpose not in PURPOSE:
            raise ValueError(f"unsupported purpose {purpose}")
        if access not in ACCESS or access == "None":
            raise ValueError(f"unsupported access {access}")
        if sensitivity not in SENSITIVITY:
            raise ValueError(f"unsupported sensitivity {sensitivity}")
        consent_granted = parse_bool(row["consent_granted"])
        revoked = parse_bool(row["revoked"])
        if not consent_granted or revoked:
            raise ValueError(
                "Main performance workload requires identical successful transaction mix. "
                f"Patient {row['patient_id']} has consent_granted={consent_granted}, revoked={revoked}. "
                "Use functional_tests.py for negative PBAC/consent cases."
            )
        glucose = float(row["Glucose"])
        if not (GLUCOSE_LO <= glucose <= GLUCOSE_HI):
            raise ValueError(f"Glucose {glucose} outside declared bounds [{GLUCOSE_LO}, {GLUCOSE_HI}]")
        if DP_QUERY_MODE == "bounded_mean":
            bucket = int(idx) // DP_COHORT_SIZE
            start = bucket * DP_COHORT_SIZE
            cohort = all_glucose[start:start + DP_COHORT_SIZE]
            query_true_value = statistics.mean(cohort)
            query_sensitivity = (GLUCOSE_HI - GLUCOSE_LO) / DP_COHORT_SIZE
            query_meta = {"type": "bounded_mean", "cohort_size": DP_COHORT_SIZE, "cohort_bucket": bucket}
        else:
            query_true_value = glucose
            query_sensitivity = GLUCOSE_SENSITIVITY
            query_meta = {"type": "scalar", "cohort_size": 1, "cohort_bucket": int(idx)}
        pid_text = str(row["patient_id"])
        pid = patient_uint(pid_text)
        signer_key, role_name = PURPOSE_TO_SIGNER[purpose]
        rows.append({
            "row_index": int(idx),
            "patient_id_text": pid_text,
            "patient_id": pid,
            "record": {k: (None if pd.isna(v) else v.item() if hasattr(v, "item") else v) for k, v in row.items() if k not in required_consent - {"patient_id"}},
            "glucose": glucose,
            "query_true_value": query_true_value,
            "query_sensitivity": query_sensitivity,
            "query_meta": query_meta,
            "purpose": purpose,
            "access": access,
            "sensitivity": sensitivity,
            "duration_seconds": policy_duration_seconds(row["valid_until"]),
            "requester_key": signer_key,
            "requester_role": role_name,
            "request_id": 1_000_000 + pid,
        })
    return rows


def workload_hash(rows: list[dict[str, Any]]) -> str:
    canonical = [{
        "patient_id": r["patient_id_text"], "purpose": r["purpose"], "access": r["access"],
        "sensitivity": r["sensitivity"], "duration_seconds": r["duration_seconds"], "glucose": r["glucose"],
        "query_true_value": r["query_true_value"], "query_sensitivity": r["query_sensitivity"], "query_meta": r["query_meta"],
        "requester_role": r["requester_role"], "request_id": r["request_id"],
    } for r in rows]
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _command_version(cmd: list[str]) -> str | None:
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=15)
        text = (proc.stdout or "").strip().replace("\r", " ").replace("\n", " | ")
        return text[:500] if proc.returncode == 0 and text else None
    except Exception:
        return None


def host_environment() -> dict[str, Any]:
    """Capture the execution conditions needed to explain/reproduce paper results."""
    env = {
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python_version": sys.version.split()[0],
        "logical_cpu_count": os.cpu_count(),
        "docker_client_version": _command_version(["docker", "--version"]),
        "docker_compose_version": _command_version(["docker", "compose", "version"]),
        "host_label": os.getenv("BENCHMARK_HOST_LABEL", "").strip() or None,
    }
    canonical = json.dumps(env, sort_keys=True, separators=(",", ":"), default=str).encode()
    env["fingerprint_sha256"] = hashlib.sha256(canonical).hexdigest()
    return env


# ---------------------------------------------------------------------------
# IPFS helpers
# ---------------------------------------------------------------------------
def ipfs_add(name: str, payload: bytes) -> tuple[str, float]:
    t0 = time.perf_counter()
    response = session.post(f"{IPFS_API_URL}/add", files={"file": (name, payload)}, timeout=120)
    response.raise_for_status()
    elapsed = time.perf_counter() - t0
    data = response.json()
    return data["Hash"], elapsed


def ipfs_cat(cid: str) -> bytes:
    response = session.post(f"{IPFS_API_URL}/cat", params={"arg": cid}, timeout=120)
    response.raise_for_status()
    return response.content


def sha256_bytes(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def canonical_json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _source_key_nonce(row: dict[str, Any], label: str) -> tuple[bytes, bytes]:
    """Derive a unique AES-GCM key/nonce pair for reproducible benchmark artifacts.

    This mode exists so the exact same encrypted bytes, hashes and CIDs are used
    across all consensus mechanisms. Each patient+label receives a distinct key,
    so nonce reuse under the same key is avoided. In a production deployment use
    a proper KMS/HSM and randomized key lifecycle instead of a benchmark seed.
    """
    material = f"{SOURCE_ARTIFACT_SEED}|{row['patient_id_text']}|{label}".encode()
    key = hashlib.sha256(b"key|" + material).digest()
    nonce = hashlib.sha256(b"nonce|" + material).digest()[:12]
    return key, nonce


def _encrypt_source(row: dict[str, Any], label: str, plaintext: bytes) -> tuple[bytes, bytes]:
    if DETERMINISTIC_SOURCE_ARTIFACTS:
        key, nonce = _source_key_nonce(row, label)
    else:
        key = os.urandom(32)
        nonce = os.urandom(12)
    aad = f"PBAC-DP|{row['patient_id_text']}|{label}".encode()
    ciphertext = nonce + AESGCM(key).encrypt(nonce, plaintext, aad)
    # Key stays local only. It is never uploaded to IPFS or blockchain.
    (KEY_DIR / f"{row['patient_id_text']}_{label}.key").write_bytes(key)
    return ciphertext, nonce


def prepare_source_ipfs(row: dict[str, Any]) -> dict[str, Any]:
    full_obj = row["record"]
    partial_fields = {k: full_obj.get(k) for k in ["patient_id", "Age", "BMI", "BloodPressure", "Glucose"] if k in full_obj}
    full_enc, _ = _encrypt_source(row, "full", canonical_json_bytes(full_obj))
    partial_enc, _ = _encrypt_source(row, "partial", canonical_json_bytes(partial_fields))
    partial_cid, partial_latency = ipfs_add(f"{row['patient_id_text']}_partial.enc", partial_enc)
    full_cid, full_latency = ipfs_add(f"{row['patient_id_text']}_full.enc", full_enc)
    return {
        "partial_cid": partial_cid, "partial_hash": sha256_bytes(partial_enc), "partial_bytes": len(partial_enc),
        "full_cid": full_cid, "full_hash": sha256_bytes(full_enc), "full_bytes": len(full_enc),
        "ipfs_upload_seconds": partial_latency + full_latency,
    }


# ---------------------------------------------------------------------------
# DP mechanisms
# ---------------------------------------------------------------------------
def stable_rng(row: dict[str, Any]) -> random.Random:
    if DP_RANDOM_MODE == "secure":
        return random.SystemRandom()  # type: ignore[return-value]
    material = f"{DP_BENCHMARK_SEED}|{row['patient_id']}|{row['request_id']}|{DP_MECHANISM}".encode()
    seed = int.from_bytes(hashlib.sha256(material).digest()[:8], "big")
    return random.Random(seed)


def generate_dp_release(row: dict[str, Any], epsilon: float) -> dict[str, Any]:
    return dp_engine.privatize(
        true_value=row["query_true_value"],
        epsilon=epsilon,
        sensitivity=row["query_sensitivity"],
        mechanism=DP_MECHANISM,
        rng=stable_rng(row),
        lo=GLUCOSE_LO,
        hi=GLUCOSE_HI,
        gaussian_delta=GAUSSIAN_DELTA,
        exponential_candidates=EXP_CANDIDATES,
    )


# ---------------------------------------------------------------------------
# Contract state / phase jobs
# ---------------------------------------------------------------------------
def patient_exists(pid: int) -> bool:
    rec = contract.functions.patientRecords(pid).call()
    return bool(rec[2])


def requester_profile(address: str) -> tuple[int, bool, str]:
    p = contract.functions.getRequesterProfile(checksum(address)).call()
    return int(p[0]), bool(p[1]), str(p[2])


def ensure_requesters() -> None:
    for key, role_name, org in [
        ("researcher", "Researcher", "University Research Lab"),
        ("clinician", "Clinician", "Clinical Care Unit"),
        ("commercial", "CommercialPartner", "Approved Industry Partner"),
    ]:
        role, registered, _ = requester_profile(ACCOUNTS[key]["address"])
        target = REQUESTER_ROLE[role_name]
        if registered and role == target:
            continue
        send_tx(
            contract.functions.registerRequester(checksum(ACCOUNTS[key]["address"]), target, org),
            "controller", "setup", "Requester registration", f"register {role_name}",
        )


def register_job(row: dict[str, Any]) -> None:
    ipfs = row["source_ipfs"]
    send_tx(
        contract.functions.registerPatientRecord(
            row["patient_id"], checksum(ACCOUNTS["patient"]["address"]), SENSITIVITY[row["sensitivity"]],
            ipfs["partial_cid"], ipfs["partial_hash"], ipfs["full_cid"], ipfs["full_hash"],
        ),
        "controller", "register_patients", "Patient registration", row["patient_id_text"],
    )


def policy_job(row: dict[str, Any]) -> None:
    # No post-receipt RPC read here: keeping the transaction-producing phase
    # free of validation calls avoids throttling the offered load. The following
    # submitAccessRequest transaction requires an active policy and therefore
    # provides the dependency check.
    send_tx(
        contract.functions.createPolicy(
            row["patient_id"], PURPOSE[row["purpose"]], ACCESS[row["access"]], row["duration_seconds"], True
        ),
        "patient", "create_policies", "Policy creation", row["patient_id_text"],
    )


def request_job(row: dict[str, Any]) -> None:
    send_tx(
        contract.functions.submitAccessRequest(
            row["request_id"], row["patient_id"], PURPOSE[row["purpose"]], ACCESS[row["access"]]
        ),
        row["requester_key"], "submit_requests", "Access request submission", row["patient_id_text"],
    )


def evaluate_job(row: dict[str, Any]) -> None:
    send_tx(
        contract.functions.evaluateRequest(row["request_id"]),
        "controller", "evaluate_requests", "PBAC evaluation", row["patient_id_text"],
    )


def status_for_row(row: dict[str, Any]) -> dict[str, Any]:
    result = _read_only_rpc_with_retry(
        f"verify authorization for {row['patient_id_text']}",
        lambda: contract.functions.statusByRequester(row["request_id"]).call(
            {"from": checksum(ACCOUNTS[row["requester_key"]]["address"])}
        ),
    )
    out = {
        "status_code": int(result[0]), "status": STATUS.get(int(result[0]), str(result[0])),
        "reason_code": int(result[1]), "reason": DENIAL_REASON.get(int(result[1]), str(result[1])),
        "policy_id": int(result[2]), "cid": str(result[3]), "record_hash": bytes(result[4]),
    }
    if VERIFY_IPFS and out["status"] in {"Granted", "PartialGranted"}:
        payload = ipfs_cat(out["cid"])
        out["ipfs_hash_verified"] = sha256_bytes(payload) == out["record_hash"]
        if not out["ipfs_hash_verified"]:
            raise RuntimeError(f"IPFS hash mismatch for {row['patient_id_text']}")
    else:
        out["ipfs_hash_verified"] = None
    return out


def _read_only_rpc_with_retry(description: str, fn: Callable[[], Any]) -> Any:
    """Bounded retries for transient read failures, never transactions."""
    for attempt in range(3):
        try:
            return fn()
        except (Web3RPCError, BlockNotFound) as exc:
            transient = (isinstance(exc, Web3RPCError) and '-32603' in str(exc)) or (isinstance(exc, BlockNotFound) and "'latest'" in str(exc))
            if not transient or attempt == 2:
                raise
            delay = 2 * (attempt + 1)
            print(f"Retrying read-only RPC {description} after {delay}s: {exc}", flush=True)
            time.sleep(delay)


def prepare_dp_job(row: dict[str, Any]) -> None:
    epsilon_scaled = int(_read_only_rpc_with_retry(
        f"derive epsilon for {row['patient_id_text']}",
        lambda: contract.functions.deriveEpsilonForRequest(row["request_id"]).call(),
    ))
    if epsilon_scaled <= 0:
        raise RuntimeError("non-positive epsilon")
    epsilon_budget_before_scaled = int(_read_only_rpc_with_retry(
        f"read epsilon budget for {row['patient_id_text']}",
        lambda: contract.functions.getRemainingEpsilonBudget(row["patient_id"]).call(),
    ))
    if epsilon_budget_before_scaled < epsilon_scaled:
        raise RuntimeError("insufficient epsilon budget before DP release")
    epsilon = epsilon_scaled / EPSILON_SCALE
    release = generate_dp_release(row, epsilon)
    released_scaled = int(round(release["released_value"] * DP_VALUE_SCALE))
    sensitivity_scaled = max(1, int(round(row["query_sensitivity"] * DP_VALUE_SCALE)))
    # Public payload deliberately excludes original aggregate/value and raw noise.
    public_payload = {
        "schema": "HealthcareConsentDP-Final-release-v1",
        "patient_id": row["patient_id"],
        "request_id": row["request_id"],
        "attribute": "Glucose",
        "query_type": row["query_meta"]["type"],
        "cohort_size": row["query_meta"]["cohort_size"],
        "mechanism": DP_MECHANISM,
        "released_value_scaled": released_scaled,
        "value_scale": DP_VALUE_SCALE,
        "epsilon_scaled": epsilon_scaled,
        "epsilon_scale": EPSILON_SCALE,
        "query_sensitivity_scaled": sensitivity_scaled,
        "delta": GAUSSIAN_DELTA if DP_MECHANISM == "Gaussian" else None,
    }
    payload = canonical_json_bytes(public_payload)
    cid, latency = ipfs_add(f"dp_{row['request_id']}_{DP_MECHANISM.lower()}.json", payload)
    row["dp"] = {
        **release,
        "epsilon": epsilon,
        "epsilon_scaled": epsilon_scaled,
        "epsilon_budget_before_scaled": epsilon_budget_before_scaled,
        "released_scaled": released_scaled,
        "sensitivity_scaled": sensitivity_scaled,
        "release_hash": sha256_bytes(payload),
        "release_hash_hex": "0x" + sha256_bytes(payload).hex(),
        "cid": cid,
        "ipfs_upload_seconds": latency,
    }


def release_job(row: dict[str, Any]) -> None:
    d = row["dp"]
    send_tx(
        contract.functions.releasePrivateAggregate(
            row["request_id"], d["released_scaled"], d["sensitivity_scaled"],
            MECHANISM_CODE[DP_MECHANISM], d["release_hash"], d["cid"],
        ),
        row["requester_key"], "dp_releases", f"DP release ({DP_MECHANISM})", row["patient_id_text"],
    )


def verify_release_job(row: dict[str, Any]) -> None:
    # Read-only RPC retries never replay a transaction or alter workload timing.
    for attempt in range(3):
        try:
            return _verify_release_once(row)
        except Web3RPCError as exc:
            if '-32603' not in str(exc) or attempt == 2:
                raise
            print(f"Retrying read-only DP verification for {row['patient_id_text']}: {exc}", flush=True)
            time.sleep(2 * (attempt + 1))


def _verify_release_once(row: dict[str, Any]) -> None:
    """Read-back is deliberately separated from the transaction-producing phase.

    This proves the DP handoff without letting verification RPCs throttle the
    number of release transactions offered to Besu.
    """
    d = row["dp"]
    count = int(contract.functions.getReleaseCount(row["request_id"]).call())
    if count != 1:
        raise RuntimeError(f"expected exactly one release, found {count}")
    stored = contract.functions.getRelease(row["request_id"], 0).call()
    epsilon_budget_after_scaled = int(contract.functions.getRemainingEpsilonBudget(row["patient_id"]).call())
    checks = {
        "patient": int(stored[0]) == row["patient_id"],
        "released_value": int(stored[1]) == d["released_scaled"],
        "epsilon": int(stored[2]) == d["epsilon_scaled"],
        "sensitivity": int(stored[3]) == d["sensitivity_scaled"],
        "mechanism": int(stored[4]) == MECHANISM_CODE[DP_MECHANISM],
        "release_hash": bytes(stored[5]) == d["release_hash"],
        "cid": str(stored[6]) == d["cid"],
        "request_replay_guard": bool(contract.functions.requestReleased(row["request_id"]).call()),
        "epsilon_budget_deduction": d["epsilon_budget_before_scaled"] - epsilon_budget_after_scaled == d["epsilon_scaled"],
    }
    if not all(checks.values()):
        raise RuntimeError(f"DP on-chain read-back mismatch: {checks}")
    row["dp"]["on_chain_verified"] = True
    row["dp"]["on_chain_checks"] = checks
    row["dp"]["remaining_epsilon_budget_scaled"] = epsilon_budget_after_scaled


# ---------------------------------------------------------------------------
# Execution helpers
# ---------------------------------------------------------------------------
def parallel_phase(name: str, rows: list[dict[str, Any]], fn: Callable[[dict[str, Any]], None], workers: int = WORKERS) -> None:
    print(f"\n=== {name}: {len(rows)} items, workers={workers} ===", flush=True)
    errors = []
    phase_started = time.perf_counter()
    run_started = globals().get("_RUN_STARTED_MONO", phase_started)
    _print_progress(write_live_progress(name, 0, len(rows), phase_started, run_started))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=name[:12]) as executor:
        futures = {executor.submit(fn, row): row for row in rows}
        completed = 0
        report_every = max(1, min(25, len(rows) // 20 or 1))
        last_report = phase_started
        for future in as_completed(futures):
            row = futures[future]
            try:
                future.result()
            except Exception as exc:
                errors.append((row["patient_id_text"], f"{type(exc).__name__}: {exc}"))
            completed += 1
            now = time.perf_counter()
            if completed % report_every == 0 or completed == len(rows) or now - last_report >= 5.0:
                _print_progress(write_live_progress(name, completed, len(rows), phase_started, run_started))
                last_report = now
    if errors:
        preview = "; ".join(f"{pid}: {err}" for pid, err in errors[:10])
        raise RuntimeError(f"{name} failed for {len(errors)} items. First errors: {preview}")


def preflight(rows: list[dict[str, Any]]) -> dict[str, Any]:
    print("Running preflight checks...")
    if not w3.is_connected():
        raise RuntimeError(f"cannot connect to Besu at {BESU_RPC_URL}")
    if int(w3.eth.chain_id) != CHAIN_ID:
        raise RuntimeError(f"chainId mismatch: expected {CHAIN_ID}, got {w3.eth.chain_id}")

    # Fail before the experiment if a configured signer key does not derive the
    # declared address or is not funded in the selected fresh genesis.
    account_balances: dict[str, int] = {}
    for role, acct_cfg in ACCOUNTS.items():
        derived = w3.eth.account.from_key(acct_cfg["private_key"]).address
        declared = checksum(acct_cfg["address"])
        if derived.lower() != declared.lower():
            raise RuntimeError(
                f"{role} private-key/address mismatch: derived={derived}, declared={declared}"
            )
        balance = int(w3.eth.get_balance(declared))
        if balance <= 0:
            raise RuntimeError(f"{role} account is not funded in genesis: {declared}")
        account_balances[role] = balance

    client_version = w3.client_version
    if EXPECTED_BESU_VERSION and EXPECTED_BESU_VERSION not in client_version:
        raise RuntimeError(f"Besu version mismatch: expected {EXPECTED_BESU_VERSION}, got {client_version}")
    peer_count = int(rpc_result("net_peerCount"), 16)
    peer_deadline = time.monotonic() + 60
    while peer_count < 4 and time.monotonic() < peer_deadline:
        print(f"Waiting for peers: {peer_count}/4", flush=True)
        time.sleep(2)
        peer_count = int(rpc_result("net_peerCount"), 16)
    if peer_count < 4:
        raise RuntimeError(f"insufficient peers: {peer_count}; expected at least 4")
    consensus_method, live_validators = consensus_validators()
    declared_validators = expected_validators()
    if set(live_validators) != set(declared_validators):
        raise RuntimeError(
            f"{CONSENSUS} validator mismatch via {consensus_method}: "
            f"live={live_validators}, expected={declared_validators}"
        )
    local_net = local_network_config()
    if local_net["block_period_seconds"] != 2:
        raise RuntimeError(f"local block period is not 2s: {local_net}")
    code = w3.eth.get_code(checksum(CONTRACT_ADDRESS))
    if not code or code == b"\x00":
        raise RuntimeError("no contract bytecode at PBAC_CONTRACT_ADDRESS")
    onchain_controller = checksum(contract.functions.controller().call())
    if onchain_controller != checksum(ACCOUNTS["controller"]["address"]):
        raise RuntimeError(f"controller mismatch: contract={onchain_controller}, configured={ACCOUNTS['controller']['address']}")

    required = {
        "registerPatientRecord", "createPolicy", "registerRequester", "submitAccessRequest",
        "evaluateRequest", "statusByRequester", "deriveEpsilonForRequest", "releasePrivateAggregate",
        "getRelease", "getReleaseCount", "requestReleased", "getRemainingEpsilonBudget",
    }
    present = {entry.get("name") for entry in ABI if entry.get("type") == "function"}
    missing = sorted(required - present)
    if missing:
        raise RuntimeError(f"ABI missing required functions: {missing}")

    if REQUIRE_FRESH_CONTRACT and not RESUME_RUN:
        if int(contract.functions.lastPolicyId().call()) != 0:
            raise RuntimeError("contract is not fresh: lastPolicyId != 0. Deploy a new contract or reset/redeploy.")
        dirty = [r["patient_id_text"] for r in rows[: min(25, len(rows))] if patient_exists(r["patient_id"])]
        if dirty:
            raise RuntimeError(f"contract is not fresh: selected patients already exist: {dirty[:10]}")

    response = session.post(f"{IPFS_API_URL}/version", timeout=30)
    response.raise_for_status()
    ipfs_version = response.json().get("Version")

    genesis = w3.eth.get_block(0)
    latest = w3.eth.get_block("latest")
    if int(genesis.gasLimit) != local_net["declared_gas_limit"]:
        raise RuntimeError(
            f"genesis gas limit mismatch: chain={int(genesis.gasLimit)}, local={local_net['declared_gas_limit']}"
        )
    manifest = {
        "run_id": RUN_ID,
        "campaign_id": CAMPAIGN_ID,
        "consensus_label": CONSENSUS,
        "client_version": client_version,
        "expected_besu_version": EXPECTED_BESU_VERSION,
        "chain_id": int(w3.eth.chain_id),
        "peer_count": peer_count,
        "consensus_rpc_method": consensus_method,
        "consensus_validators": live_validators,
        "expected_validators": declared_validators,
        "local_genesis_sha256": local_net["genesis_sha256"],
        "runtime_config_sha256": local_net["runtime_config_sha256"],
        "block_period_seconds": local_net["block_period_seconds"],
        "genesis_hash": genesis.hash.hex(),
        "genesis_gas_limit": int(genesis.gasLimit),
        "start_block": int(latest.number),
        "contract_address": checksum(CONTRACT_ADDRESS),
        "contract_code_sha256": hashlib.sha256(bytes(code)).hexdigest(),
        "signer_addresses": {role: checksum(cfg["address"]) for role, cfg in ACCOUNTS.items()},
        "signer_start_balances_wei": account_balances,
        "ipfs_version": ipfs_version,
        "n_records": N_RECORDS,
        "start_row": START_ROW,
        "workers": WORKERS,
        "max_pending_per_sender": MAX_PENDING_PER_SENDER,
        "dp_mechanism": DP_MECHANISM,
        "dp_random_mode": DP_RANDOM_MODE,
        "dp_benchmark_seed_sha256": hashlib.sha256(DP_BENCHMARK_SEED.encode()).hexdigest(),
        "benchmark_reference_time": BENCHMARK_REFERENCE_TIME.isoformat(),
        "deterministic_source_artifacts": DETERMINISTIC_SOURCE_ARTIFACTS,
        "source_artifact_seed_sha256": hashlib.sha256(SOURCE_ARTIFACT_SEED.encode()).hexdigest(),
        "dp_query_mode": DP_QUERY_MODE,
        "dp_cohort_size": DP_COHORT_SIZE if DP_QUERY_MODE == "bounded_mean" else 1,
        "dp_glucose_lower_bound": GLUCOSE_LO,
        "dp_glucose_upper_bound": GLUCOSE_HI,
        "dp_scalar_sensitivity_glucose": GLUCOSE_SENSITIVITY,
        "dp_value_scale": DP_VALUE_SCALE,
        "epsilon_scale": EPSILON_SCALE,
        "gaussian_delta": GAUSSIAN_DELTA,
        "exponential_candidates": EXP_CANDIDATES,
        "gas_price_gwei": GAS_PRICE_GWEI,
        "tx_timeout_seconds": TX_TIMEOUT_SECONDS,
        "verify_ipfs": VERIFY_IPFS,
        "cpu_sample_interval_seconds": CPU_SAMPLE_INTERVAL_SECONDS,
        "resume_run": RESUME_RUN,
        "patient_data_file": str(PATIENT_DATA_FILE),
        "patient_data_sha256": file_sha256(PATIENT_DATA_FILE),
        "consent_data_file": str(CONSENT_DATA_FILE),
        "consent_data_sha256": file_sha256(CONSENT_DATA_FILE),
        "host_environment": host_environment(),
        "workload_sha256": workload_hash(rows),
    }
    print(json.dumps(manifest, indent=2))
    return manifest


def write_outputs(rows: list[dict[str, Any]], manifest: dict[str, Any], started: float, finished: float, transactions_per_patient: int = 5) -> None:
    # Verify that the noise pipeline was actually active.
    noises = [float(r["dp"]["sampled_noise"]) for r in rows]
    released = [float(r["dp"]["released_value"]) for r in rows]
    nonzero_noise = sum(abs(v) > 1e-12 for v in noises)
    unique_releases = len({round(v, 9) for v in released})
    if nonzero_noise == 0:
        raise RuntimeError("DP generator produced zero noise for every record")
    if len(rows) >= 3 and unique_releases < 2:
        raise RuntimeError("DP generator produced no meaningful variation in released values")
    if not all(r["dp"].get("on_chain_verified") for r in rows):
        raise RuntimeError("one or more DP releases were not verified on-chain")

    summary = metrics.summary()
    summary.update({
        "manifest": manifest,
        "wall_clock_seconds": None if RESUME_RUN else finished - started,
        "resumed_from_checkpoint": RESUME_RUN,
        "publication_eligible": not RESUME_RUN,
        "selected_patients": len(rows),
        "expected_workload_transactions": transactions_per_patient * len(rows),
        "actual_workload_transactions": summary["workload"]["count"],
        "transaction_mix_valid": summary["workload"]["count"] == transactions_per_patient * len(rows),
        "dp_handoff": {
            "verified_count": sum(1 for r in rows if r["dp"].get("on_chain_verified")),
            "nonzero_noise_count": nonzero_noise,
            "unique_released_values": unique_releases,
            "mean_absolute_sampled_noise": statistics.mean(abs(v) for v in noises),
            "clipping_rate": sum(bool(r["dp"]["was_clipped"]) for r in rows) / len(rows),
        },
        "epsilon_budget": {
            "deduction_verified_count": sum(1 for r in rows if r["dp"].get("on_chain_checks", {}).get("epsilon_budget_deduction")),
            "mean_epsilon_used": statistics.mean(float(r["dp"]["epsilon"]) for r in rows),
            "min_remaining_epsilon_budget": min(float(r["dp"]["remaining_epsilon_budget_scaled"]) / EPSILON_SCALE for r in rows),
            "max_remaining_epsilon_budget": max(float(r["dp"]["remaining_epsilon_budget_scaled"]) / EPSILON_SCALE for r in rows),
        },
        "cpu_usage": resource_monitor.summary(),
    })
    if not summary["transaction_mix_valid"]:
        raise RuntimeError(
            f"transaction mix invalid: expected {transactions_per_patient * len(rows)}, got {summary['workload']['count']}"
        )
    workload_records = [r for r in metrics.records if r["phase"] != "setup"]
    summary["block_observability"] = block_observability(workload_records)

    with (OUT_DIR / "summary.json").open("w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, default=str)
    with (OUT_DIR / "transactions.csv").open("w", newline="", encoding="utf-8") as fh:
        fields = ["phase", "tx_type", "label", "sender_role", "sender", "tx_hash", "block_number", "gas_used", "submitted_at", "confirmed_at", "latency_seconds"]
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for rec in metrics.records:
            writer.writerow({k: rec.get(k) for k in fields})

    local_rows = []
    for r in rows:
        local_rows.append({
            "patient_id": r["patient_id_text"], "request_id": r["request_id"], "purpose": r["purpose"],
            "access": r["access"], "sensitivity": r["sensitivity"], "requester_role": r["requester_role"],
            "status": r["status"]["status"], "reason": r["status"]["reason"],
            "epsilon": r["dp"]["epsilon"], "query_type": r["query_meta"]["type"],
            "query_cohort_size": r["query_meta"]["cohort_size"], "true_query_value_LOCAL_ONLY": r["dp"]["true_value"],
            "query_sensitivity": r["query_sensitivity"],
            "epsilon_budget_before": r["dp"]["epsilon_budget_before_scaled"] / EPSILON_SCALE,
            "remaining_epsilon_budget": r["dp"]["remaining_epsilon_budget_scaled"] / EPSILON_SCALE,
            "sampled_noise_LOCAL_ONLY": r["dp"]["sampled_noise"], "released_value": r["dp"]["released_value"],
            "release_cid": r["dp"]["cid"], "release_hash": r["dp"]["release_hash_hex"],
            "on_chain_verified": r["dp"]["on_chain_verified"],
        })
    with (OUT_DIR / "patient_dp_audit_LOCAL_ONLY.json").open("w", encoding="utf-8") as fh:
        json.dump(local_rows, fh, indent=2)

    print("\n=== FINAL RESULT ===")
    print(f"Output: {OUT_DIR}")
    print(f"Workload hash: {manifest['workload_sha256']}")
    print(f"Transactions: {summary['actual_workload_transactions']} / expected {summary['expected_workload_transactions']}")
    print(f"Active blockchain TPS: {summary['active_blockchain_tps']:.4f}")
    print(f"First-to-last workload TPS: {summary['first_to_last_workload_tps']:.4f}")
    print(f"Workload submission rate: {summary['workload']['submission_rate_tps']:.4f} tx/s")
    print(f"Queue drain after last submission: {summary['workload']['queue_drain_seconds']:.4f}s")
    print(f"DP handoffs verified: {summary['dp_handoff']['verified_count']}/{len(rows)}")
    print(f"Non-zero noise releases: {summary['dp_handoff']['nonzero_noise_count']}/{len(rows)}")
    print(f"Epsilon-budget deductions verified: {summary['epsilon_budget']['deduction_verified_count']}/{len(rows)}")
    cpu = summary["cpu_usage"]["validators_aggregate"]
    print(f"Validator CPU: mean={cpu['mean_percent']:.2f}% p95={cpu['p95_percent']:.2f}% max={cpu['max_percent']:.2f}%")
    print("Phase TPS:")
    for phase, data in summary["phase_summary"].items():
        print(f"  {phase:20s} {data['tps']:.4f} TPS | p95={data['latency']['p95_seconds']:.4f}s")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    base_rows = load_workload()
    manifest = preflight(base_rows)
    last_completed: str | None = None
    if RESUME_RUN:
        rows, last_completed = load_phase_boundary_checkpoint(base_rows, manifest)
    else:
        rows = base_rows
        checkpoint_complete_stage("preflight", rows, manifest)
        last_completed = "preflight"

    started = time.time()
    global _RUN_STARTED_MONO
    _RUN_STARTED_MONO = time.perf_counter()
    resource_monitor.start()

    def run_stage(stage: str, action: Callable[[], None]) -> None:
        nonlocal last_completed
        if stage_already_completed(stage, last_completed):
            print(f"Skipping checkpointed stage: {stage}", flush=True)
            return
        checkpoint_mark_active(stage, last_completed)
        resource_monitor.set_phase(stage)
        stage_started = time.perf_counter()
        if stage not in {"prepare_source_ipfs", "register_patients", "create_policies", "submit_requests", "evaluate_requests", "verify_authorization", "prepare_dp_release", "dp_releases", "verify_dp_readback"}:
            _print_progress(write_live_progress(stage, 0, 1, stage_started, _RUN_STARTED_MONO))
        action()
        if stage not in {"prepare_source_ipfs", "register_patients", "create_policies", "submit_requests", "evaluate_requests", "verify_authorization", "prepare_dp_release", "dp_releases", "verify_dp_readback"}:
            _print_progress(write_live_progress(stage, 1, 1, stage_started, _RUN_STARTED_MONO))
        checkpoint_complete_stage(stage, rows, manifest)
        last_completed = stage

    # System setup: excluded from per-patient workload metrics.
    run_stage("setup_requesters", ensure_requesters)

    # Off-chain preparation is deliberately completed before the blockchain
    # phases so IPFS/encryption does not starve the transaction generator.
    run_stage(
        "prepare_source_ipfs",
        lambda: parallel_phase("prepare_source_ipfs", rows, lambda r: r.update({"source_ipfs": prepare_source_ipfs(r)})),
    )

    # Five identical transaction phases per authorized patient.  Dependencies
    # between phases are sequential, while patient operations *within* each
    # phase are parallel when WORKERS > 1.
    run_stage("register_patients", lambda: parallel_phase("register_patients", rows, register_job))
    run_stage("create_policies", lambda: parallel_phase("create_policies", rows, policy_job))
    run_stage("submit_requests", lambda: parallel_phase("submit_requests", rows, request_job))
    run_stage("evaluate_requests", lambda: parallel_phase("evaluate_requests", rows, evaluate_job))

    def authorization_stage() -> None:
        parallel_phase("verify_authorization", rows, lambda r: r.update({"status": status_for_row(r)}))
        denied = [r for r in rows if r["status"]["status"] not in {"Granted", "PartialGranted"}]
        if denied:
            raise RuntimeError(
                "Main performance run requires all selected requests to authorize. "
                f"Denied={[(r['patient_id_text'], r['status']['reason']) for r in denied[:10]]}"
            )

    run_stage("verify_authorization", authorization_stage)

    # Critical fixed pipeline: request-specific on-chain epsilon -> DP noise ->
    # privacy-safe IPFS payload -> releasePrivateAggregate -> on-chain readback.
    run_stage("prepare_dp_release", lambda: parallel_phase("prepare_dp_release", rows, prepare_dp_job))
    run_stage("dp_releases", lambda: parallel_phase("dp_releases", rows, release_job))
    run_stage("verify_dp_readback", lambda: parallel_phase("verify_dp_readback", rows, verify_release_job))

    finished = time.time()
    resource_monitor.stop()
    if not stage_already_completed("write_outputs", last_completed):
        checkpoint_mark_active("write_outputs", last_completed)
        write_outputs(rows, manifest, started, finished)
        checkpoint_complete_stage("write_outputs", rows, manifest)
        last_completed = "write_outputs"
    checkpoint_complete_stage("complete", rows, manifest)
    complete_started = time.perf_counter()
    final_progress = write_live_progress("complete", 1, 1, complete_started, _RUN_STARTED_MONO, status="complete")
    final_progress["approx_overall_percent"] = 100.0
    final_progress["approx_run_eta_seconds"] = 0.0
    _atomic_json(LIVE_PROGRESS_FILE, final_progress)
    _print_progress(final_progress)


if __name__ == "__main__":
    main()
