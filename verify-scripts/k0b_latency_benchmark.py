#!/usr/bin/env python3
"""
sdk/verify-scripts/k0b_latency_benchmark.py
=============================================
K0B authoritative governance latency benchmark.

Measures SDK-side wall-clock duration, backend-reported Server-Timing
metrics (when the backend's dual opt-in is satisfied — see below), derived
network/client overhead, retry counts, and SDK-client created/reused
classification for the existing authoritative `POST /v1/chains`,
`POST /v1/chains/{id}/events`, and `POST /v1/chains/{id}/complete` requests.

MEASUREMENT SEMANTICS AND LIMITATIONS (read before interpreting output)
-------------------------------------------------------------------------
- "client_created" / "client_reused" describe only whether the SDK
  constructed a new `httpx.AsyncClient` object for the current event loop or
  reused an already-cached one. This does NOT prove whether the underlying
  TCP/TLS connection was reused — httpx/httpcore manage connection pooling
  independently of client-object lifetime, and this script cannot observe
  that layer. Never read "client_reused" as "connection reused", "TLS
  reused", or "keepalive confirmed".
- "network_client_overhead_ms" (= total SDK wall time minus server-reported
  "app" application duration) is a residual, not a pure network measurement.
  It may include network transit, DNS/TCP/TLS handshake time, httpx-internal
  processing, local asyncio scheduling delay, retry backoff sleep time (if
  the request retried), and response parsing. It is never labeled or
  reported as "network RTT".
- The backend Server-Timing header (when both server and request opt-ins are
  satisfied — see backend/app/main.py) reports total application processing
  time as "app", plus — since backend commit dc513f1 (K2A1) — up to six
  additional narrower metrics on successful requests to the three governed
  handlers: "auth", "dbacq", "lookup", "verify", "lastused", "handler". See
  _K2A_NESTING_NOTE below (and backend/app/benchmark_timing.py) for exactly
  what each one measures and how they nest inside "app" — they are NOT
  independent, additive quantities; do not sum them or subtract them all
  from "app". Detailed metrics are absent (by design, not error) on any
  non-2xx response, any route other than the three governed ones, or an
  older/unpatched deployment that only ever sends "app" — this script
  tolerates all of those as normal, not as failures.
- Requests that internally retried have inflated total_duration_ms (backoff
  sleep is real elapsed wall time for that sample) — see retries_total.

WARNING
-------
This script must be run only against a controlled DEVELOPMENT organization
and backend. It refuses to target a production-looking backend URL unless
ARCLASP_K0B_ALLOW_PRODUCTION=1 is explicitly set, and even then this
script does not run itself automatically against production — a human
operator must choose to set that variable and invoke it deliberately.

REQUIRED ENVIRONMENT VARIABLES
-------------------------------
    ARCLASP_K0B_BACKEND_URL   Backend base URL to benchmark against.
    ARCLASP_K0B_API_KEY       API key for a controlled development
                                 organization. NEVER printed or written to
                                 any output file by this script.

OPTIONAL ENVIRONMENT VARIABLES
-------------------------------
    ARCLASP_K0B_REGION_LABEL      Operator-supplied benchmark region label
                                     (e.g. "india", "us-east"). Recorded
                                     verbatim in output as low-cardinality
                                     metadata only — no other free text is
                                     ever recorded.
    ARCLASP_K0B_ALLOW_PRODUCTION  Must be "1" to target a production-
                                     looking backend URL. Absent by default.

WHAT THIS SCRIPT NEVER RECORDS OR PRINTS
------------------------------------------
API keys, Authorization headers, chain IDs, organization IDs, user IDs,
agent/action names, action payloads, prompts, responses, or raw exception
text (which could embed the above). Only low-cardinality metadata is
recorded: operation category, durations, retry count, response status
class, cold/warm classification, client created/reused, exception class
name, event count requested, and the operator-supplied region label.

USAGE
-----
    # Validate configuration only — makes no network calls.
    python k0b_latency_benchmark.py --dry-run

    # Minimal safe run (default): 1-event chain, 1 repetition.
    python k0b_latency_benchmark.py

    # Broader run.
    python k0b_latency_benchmark.py --event-counts 1,20,50 --runs 3
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import pathlib
import re
import statistics
import sys
import time
import uuid
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Make the local (editable) SDK importable regardless of CWD.
# ---------------------------------------------------------------------------
_SDK_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(_SDK_ROOT))

import arclasp  # noqa: E402
from arclasp import client as _pc  # noqa: E402
from arclasp.chain import Chain  # noqa: E402
from arclasp.exceptions import (  # noqa: E402
    ActionDeniedError,
    BackendUnavailableError,
    ChainAutoPausedError,
    ChainTimeoutError,
    ArclaspKillSwitchError,
)
from arclasp.models import PolicyDecision  # noqa: E402
from arclasp.sanitization import sanitize_payload  # noqa: E402

_ALLOWED_EVENT_COUNTS = (1, 20, 50)
_DEFAULT_MAX_REQUESTS = 30
_HARD_REQUEST_CEILING = 500  # this script refuses to plan more than this, ever
_PRODUCTION_URL_MARKERS = ("arclasp.com", "api.arclasp", "proofrail.dev", "api.proofrail")

# Request-side half of the backend's dual opt-in (see
# backend/app/main.py::_k0b_benchmark_timing_header). Not a secret — sent
# unconditionally by this script; harmless no-op if the server setting is off.
_BENCHMARK_TIMING_REQUEST_HEADER = "X-Arclasp-Benchmark-Timing"
_BENCHMARK_TIMING_REQUEST_VALUE = "1"

# Decisions that a benchmark run is allowed to see and continue past.
# Anything else (deny, require_approval, kill-switch, auto-pause, or any
# unrecognized value) stops the run immediately — never waits/polls.
_ALLOWED_POLICY_DECISIONS = ("allow", "allow_with_flag")


class _AbortBenchmark(Exception):
    """Raised internally to stop the run immediately on a disallowed outcome."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="K0B authoritative governance latency benchmark")
    parser.add_argument(
        "--event-counts",
        default="1",
        help="Comma-separated subset of {1,20,50}. Default: 1 (very small safe default).",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="Repetitions per event-count. Default: 1 (very small safe default).",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=_DEFAULT_MAX_REQUESTS,
        help=f"Refuse to run if the planned request count exceeds this cap "
        f"(hard ceiling {_HARD_REQUEST_CEILING}). Default: {_DEFAULT_MAX_REQUESTS}.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate configuration and print the run plan; make no network calls.",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Output directory for sanitized JSON/summary. "
        "Default: <repo_root>/verification-artifacts/k0b",
    )
    return parser.parse_args()


def _parse_event_counts(raw: str) -> list[int]:
    counts: list[int] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            value = int(token)
        except ValueError:
            raise SystemExit(f"Invalid --event-counts value: {token!r}")
        if value not in _ALLOWED_EVENT_COUNTS:
            raise SystemExit(
                f"--event-counts values must be a subset of {_ALLOWED_EVENT_COUNTS}, got {value}"
            )
        counts.append(value)
    if not counts:
        raise SystemExit("--event-counts must specify at least one value")
    return counts


def _planned_request_count(event_counts: list[int], runs: int) -> int:
    # Each run of N events issues: 1 chain_create + N event_record + 1 chain_complete.
    return sum(count + 2 for count in event_counts) * runs


def _default_out_dir() -> pathlib.Path:
    repo_root = _SDK_ROOT.parent
    return repo_root / "verification-artifacts" / "k0b"


# ---------------------------------------------------------------------------
# Sample recording
# ---------------------------------------------------------------------------


def _status_class(status_code: int | None) -> str | None:
    if status_code is None:
        return None
    return f"{status_code // 100}xx"


# K2A1 (backend commit dc513f1) extended the backend's dual-opt-in
# Server-Timing header from a single "app" metric to a fixed set of seven:
# app, auth, dbacq, lookup, verify, lastused, handler. See
# backend/app/benchmark_timing.py for their exact meaning and nesting —
# summarized again in _K2A_NESTING_NOTE below, which is surfaced in both this
# script's JSON and text output so a reader of either never has to go find
# that module to avoid double-counting nested spans.
#
# _K2A_METRIC_FIELD_MAP is the fixed allowlist: only these seven metric
# names are ever recognized. Every other metric name (e.g. a CDN/proxy/load
# balancer prepending its own "cdn;dur=5" entry) is ignored — never stored
# under any key, never surfaced in output. The dict values are the sample
# field names this script writes into (see _timed_call) — "app" keeps the
# pre-existing "server_duration_ms" name for backward compatibility; nothing
# that existed before this extension was renamed.
_K2A_METRIC_FIELD_MAP: dict[str, str] = {
    "app": "server_duration_ms",
    "auth": "auth_duration_ms",
    "dbacq": "db_connection_acquire_ms",
    "lookup": "api_key_lookup_ms",
    "verify": "api_key_hash_verify_ms",
    "lastused": "last_used_update_ms",
    "handler": "handler_duration_ms",
}

# Short display label (matches the wire metric name) for each sample field,
# used only for compact text-summary output — never for parsing.
_K2A_FIELD_TO_LABEL: dict[str, str] = {v: k for k, v in _K2A_METRIC_FIELD_MAP.items()}

_K2A_NESTING_NOTE = (
    "Nesting: lookup, verify, and lastused are nested inside auth. dbacq is "
    "normally nested inside lookup (the first query on the request's "
    "session). handler is separate from auth, not a child of it. app is the "
    "outermost measurement and contains all other metrics. Do not sum these "
    "metrics together, and do not derive a residual by subtracting all of "
    "them from app -- dbacq is already counted inside lookup, and "
    "lookup/verify/lastused are already counted inside auth, so naive "
    "addition or subtraction double-counts nested spans."
)

# Matches one Server-Timing "term": a metric name, optionally followed by
# ";param=value" attributes. Applied to each comma-separated segment of the
# header individually (Server-Timing entries never legally contain a comma
# inside a value here — dur is always a bare number). Metric names are
# matched case-insensitively (re.IGNORECASE) per K2A2 spec; group(1) is
# lower-cased again explicitly at the call site as defense in depth.
_TERM_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_-]*)\s*(?:;(.*))?$")

# Extracts a `dur` parameter's raw value from a term's ";"-prefixed
# parameter string. Tolerates optional whitespace around '=', and tolerates
# `dur` appearing anywhere among other attributes (e.g.
# ";desc=\"Application\";dur=12.34" or ";dur=12.34;desc=\"Application\"").
_DUR_VALUE_RE = re.compile(r"(?:^|;)\s*dur\s*=\s*(?P<value>[^;]+)", re.IGNORECASE)


def _parse_dur_value(raw: str) -> float | None:
    """
    Parse one dur= value. Returns None (never raises) for anything that
    isn't a plain finite non-negative number: non-numeric text, NaN,
    +/-Infinity, and negative values are all rejected here rather than
    silently coerced into a plausible-looking duration.
    """
    candidate = raw.strip().strip('"')
    try:
        value = float(candidate)
    except (ValueError, TypeError):
        return None
    if not math.isfinite(value):
        return None
    if value < 0:
        return None
    return value


def _parse_server_timing_metrics(server_timing_header: str | None) -> dict[str, float | None]:
    """
    Parse a Server-Timing header into the fixed K2A metric allowlist.

    Returns a dict with exactly the seven keys in _K2A_METRIC_FIELD_MAP's
    values (server_duration_ms, auth_duration_ms, db_connection_acquire_ms,
    api_key_lookup_ms, api_key_hash_verify_ms, last_used_update_ms,
    handler_duration_ms), each either a float (milliseconds, unrounded here
    — callers round when building output) or None if that metric was never
    validly present.

    Rules (documented, deterministic):
      - Metric names are matched case-insensitively.
      - Whitespace around names, ';', and '=' is tolerated.
      - Metrics may appear in any order.
      - Any metric name outside the fixed allowlist is ignored entirely —
        it is never stored under any key and never appears in the result,
        by construction (the result dict's keys are fixed up front and
        never grown from input).
      - A term with no parseable, finite, non-negative `dur` value is
        skipped silently — it can never raise, and never produces a
        fabricated 0.0 or a stale/previous value.
      - Duplicate recognized metrics: first VALID occurrence wins. A later
        duplicate (whether valid or malformed) never overwrites an already-
        recorded value. This matches the pre-existing "app;dur" duplicate
        rule this function replaces (see historical
        test_server_duration_ms_parsing case "app;dur=12.3, app;dur=45.6").
      - The raw header string itself is never retained in the return value
        or as a side effect of this function.
    """
    result: dict[str, float | None] = {field: None for field in _K2A_METRIC_FIELD_MAP.values()}
    if not server_timing_header:
        return result

    for raw_term in server_timing_header.split(","):
        term_match = _TERM_RE.match(raw_term)
        if term_match is None:
            continue

        name = term_match.group(1).lower()
        field = _K2A_METRIC_FIELD_MAP.get(name)
        if field is None:
            continue  # unrelated/unknown metric -- ignored, never stored

        if result[field] is not None:
            continue  # duplicate -- first valid occurrence already won

        params = term_match.group(2) or ""
        if not params:
            continue  # e.g. bare "app" with no ";dur=..." at all
        dur_match = _DUR_VALUE_RE.search(";" + params)
        if dur_match is None:
            continue
        value = _parse_dur_value(dur_match.group("value"))
        if value is None:
            continue
        result[field] = value

    return result


def _server_duration_ms(server_timing_header: str | None) -> float | None:
    """Backward-compatible accessor: the "app" metric only, unrounded. Kept
    for any existing caller/test that only ever wanted this one value —
    behaves identically to before this function was reimplemented on top of
    _parse_server_timing_metrics, including the case-insensitive metric-name
    matching newly required by K2A2 (see the module's test suite for the
    documented change from the prior case-sensitive behavior)."""
    return _parse_server_timing_metrics(server_timing_header)["server_duration_ms"]


async def _timed_call(coro_factory, operation: str, event_count: int, region_label: str | None) -> dict:
    """Run one authoritative request under the benchmark hook, returning a
    sanitized sample dict. Never records payload/identity/secret data.

    sdk_client_state ("client_created" / "client_reused") is derived
    directly from the SDK hook's authoritative client_created classification
    for this exact request — never guessed. It describes only SDK-level
    httpx.AsyncClient object lifetime, not TCP/TLS connection reuse — see
    the module docstring's MEASUREMENT SEMANTICS section.
    """
    start = time.perf_counter()
    exception_category: str | None = None
    with _pc._benchmark_sample() as sample:
        try:
            await coro_factory()
        except _AbortBenchmark as exc:
            exception_category = exc.category
            raise
        except (BackendUnavailableError, ActionDeniedError, ChainAutoPausedError,
                ChainTimeoutError, ArclaspKillSwitchError):
            exception_category = "governance_stop"
            raise
        except Exception as exc:
            exception_category = type(exc).__name__
            raise
    total_ms = (time.perf_counter() - start) * 1000

    # K2A2: parse the full fixed metric allowlist in one pass rather than
    # just "app". Every value here is independently optional — a non-2xx
    # response, a route other than the three governed ones, or an older
    # deployment that only ever sends "app" all produce None for the six
    # detailed fields without that being treated as any kind of failure
    # (see the module docstring's "Successful governed request expectation"
    # note and this script's tests for the tolerance this requires).
    metrics = _parse_server_timing_metrics(sample.server_timing_header)
    server_ms = metrics["server_duration_ms"]
    overhead_ms: float | None = None
    overhead_negative = False
    if server_ms is not None:
        overhead_ms = round(total_ms - server_ms, 3)
        if overhead_ms < 0:
            # Honest representation, not clamped: a negative residual means
            # the SDK-measured wall clock and the backend-reported duration
            # disagree (clock skew, rounding, or measurement granularity) —
            # it is flagged rather than silently floored to zero, which
            # would hide a real anomaly, or left unflagged, which would
            # misleadingly read as "negative network time".
            overhead_negative = True

    return {
        "operation": operation,
        "event_count_requested": event_count,
        "sdk_client_state": "client_created" if sample.client_created else "client_reused",
        "client_created": sample.client_created,
        "retries": sample.retries,
        "response_status_class": _status_class(sample.status_code),
        "total_duration_ms": round(total_ms, 3),
        "server_duration_ms": round(server_ms, 3) if server_ms is not None else None,
        "auth_duration_ms": round(metrics["auth_duration_ms"], 3) if metrics["auth_duration_ms"] is not None else None,
        "db_connection_acquire_ms": round(metrics["db_connection_acquire_ms"], 3) if metrics["db_connection_acquire_ms"] is not None else None,
        "api_key_lookup_ms": round(metrics["api_key_lookup_ms"], 3) if metrics["api_key_lookup_ms"] is not None else None,
        "api_key_hash_verify_ms": round(metrics["api_key_hash_verify_ms"], 3) if metrics["api_key_hash_verify_ms"] is not None else None,
        "last_used_update_ms": round(metrics["last_used_update_ms"], 3) if metrics["last_used_update_ms"] is not None else None,
        "handler_duration_ms": round(metrics["handler_duration_ms"], 3) if metrics["handler_duration_ms"] is not None else None,
        "network_client_overhead_ms": overhead_ms,
        "network_client_overhead_ms_negative": overhead_negative,
        "exception_category": exception_category or sample.exception_category,
        "benchmark_region_label": region_label,
    }


# ---------------------------------------------------------------------------
# Low-level event recording — bypasses Chain.record_agent_action()'s
# built-in human-approval polling loop.
# ---------------------------------------------------------------------------


async def _record_event_no_poll(chain: Chain, action_type: str, action_name: str) -> None:
    """
    Record one event via the low-level authoritative API directly.

    A benchmark run must stop immediately on deny, require_approval,
    kill-switch, auto-pause, or any unrecognized decision — never wait.
    Chain.record_agent_action() would otherwise block for up to hours inside
    its internal approval-polling loop on a require_approval decision, which
    is incompatible with a bounded, deterministic benchmark run. This helper
    inspects the raw decision and raises _AbortBenchmark immediately instead
    of ever entering that loop.
    """
    config = _pc.get_config()
    sanitized = sanitize_payload({}, config)
    event_body = {
        "idempotency_key": uuid.uuid4().hex,
        "agent_name": "k0b-benchmark-agent",
        "parent_agent_name": None,
        "action_type": action_type,
        "action_name": action_name,
        "action_payload": sanitized,
        "executed_at": datetime.now(timezone.utc).isoformat(),
    }

    response = await _pc._post(
        f"/v1/chains/{chain._chain_id}/events",
        event_body,
        action_type=action_type,
    )
    decision = PolicyDecision.model_validate(response)

    if decision.auto_paused:
        raise _AbortBenchmark("chain_auto_paused")
    if decision.policy_decision == "deny":
        raise _AbortBenchmark("kill_switch" if decision.kill_switch_active else "policy_deny")
    if decision.policy_decision == "require_approval":
        raise _AbortBenchmark("requires_approval")
    if decision.policy_decision not in _ALLOWED_POLICY_DECISIONS:
        raise _AbortBenchmark("unexpected_policy_decision")


# ---------------------------------------------------------------------------
# One benchmark run: create a chain, record N events, complete it.
# ---------------------------------------------------------------------------


async def _run_one(event_count: int, region_label: str | None) -> list[dict]:
    samples: list[dict] = []
    chain_name = f"k0b-benchmark-{uuid.uuid4().hex[:12]}"

    chain = Chain(chain_name)

    async def _enter():
        # Prime the client (attributing client_created/reused to THIS
        # sample) and set the request-side benchmark-timing opt-in header
        # before the first request goes out, so every request in this run —
        # including chain_start — carries it. Harmless no-op unless the
        # backend's server-side setting is also enabled.
        client = _pc._get_client()
        client.headers[_BENCHMARK_TIMING_REQUEST_HEADER] = _BENCHMARK_TIMING_REQUEST_VALUE
        await chain._start()

    samples.append(
        await _timed_call(_enter, "chain_start", event_count, region_label)
    )

    try:
        for _i in range(event_count):

            async def _record():
                await _record_event_no_poll(chain, "tool_call", "benchmark_noop")

            samples.append(
                await _timed_call(_record, "event_record", event_count, region_label)
            )
    finally:
        # Every created chain is completed where safe: this runs even when
        # the loop above stopped early on a governance/error condition.
        # Chain._complete() is best-effort — it logs and swallows its own
        # failures internally rather than raising — so this never masks an
        # in-flight exception from the try block above.
        async def _complete():
            await chain._complete()

        samples.append(
            await _timed_call(_complete, "chain_complete", event_count, region_label)
        )

    return samples


# ---------------------------------------------------------------------------
# Percentiles
# ---------------------------------------------------------------------------


def _percentiles(values: list[float]) -> dict:
    if not values:
        return {"p50": None, "p95": None, "p99": None, "n": 0}
    if len(values) == 1:
        v = round(values[0], 3)
        return {"p50": v, "p95": v, "p99": v, "n": 1}
    sorted_vals = sorted(values)
    quantiles = statistics.quantiles(sorted_vals, n=100, method="inclusive")
    return {
        "p50": round(quantiles[49], 3),
        "p95": round(quantiles[94], 3),
        "p99": round(quantiles[98], 3),
        "n": len(values),
    }


# The seven K2A sample fields to summarize, in the fixed display/nesting
# order used throughout this script (app, then its sub-spans, then the
# sibling handler metric). Percentile structures are produced only for
# fields that have at least one non-None sample in a given group — a metric
# never observed for that group (non-2xx responses, a non-governed route, or
# an older deployment reporting only "app") is omitted entirely rather than
# reported as a fabricated all-zero percentile block.
_K2A_SUMMARY_FIELDS: tuple[str, ...] = (
    "server_duration_ms",
    "auth_duration_ms",
    "db_connection_acquire_ms",
    "api_key_lookup_ms",
    "api_key_hash_verify_ms",
    "last_used_update_ms",
    "handler_duration_ms",
)


def _summarize(samples: list[dict]) -> dict:
    groups: dict[tuple, list[dict]] = {}
    for s in samples:
        key = (s["operation"], s["event_count_requested"], s["sdk_client_state"])
        groups.setdefault(key, []).append(s)

    summary = []
    for (operation, event_count, sdk_client_state), group in sorted(groups.items()):
        total_durations = [g["total_duration_ms"] for g in group]
        network_overheads = [g["network_client_overhead_ms"] for g in group if g["network_client_overhead_ms"] is not None]
        retries_total = sum(g["retries"] for g in group)
        negative_overhead_count = sum(1 for g in group if g["network_client_overhead_ms_negative"])

        entry = {
            "operation": operation,
            "event_count_requested": event_count,
            "sdk_client_state": sdk_client_state,
            "sample_count": len(group),
            "total_duration_ms": _percentiles(total_durations),
        }
        for field in _K2A_SUMMARY_FIELDS:
            values = [g[field] for g in group if g[field] is not None]
            entry[field] = _percentiles(values) if values else None
        entry["network_client_overhead_ms"] = (
            _percentiles(network_overheads) if network_overheads else None
        )
        entry["network_client_overhead_ms_negative_count"] = negative_overhead_count
        entry["retries_total"] = retries_total
        summary.append(entry)

    return {"groups": summary, "k2a_nesting_note": _K2A_NESTING_NOTE}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    args = _parse_args()
    event_counts = _parse_event_counts(args.event_counts)
    runs = args.runs
    region_label = os.environ.get("ARCLASP_K0B_REGION_LABEL")

    if runs < 1:
        raise SystemExit("--runs must be >= 1")

    if args.max_requests > _HARD_REQUEST_CEILING:
        raise SystemExit(
            f"--max-requests ({args.max_requests}) exceeds the hard ceiling "
            f"({_HARD_REQUEST_CEILING}) this script will never plan beyond."
        )

    planned = _planned_request_count(event_counts, runs)

    print("=" * 70)
    print("K0B latency benchmark")
    print("WARNING: run only against a controlled DEVELOPMENT organization.")
    print("=" * 70)
    print(f"event_counts={event_counts} runs={runs} planned_requests={planned} "
          f"cap={args.max_requests} region_label={region_label!r}")

    if planned > args.max_requests:
        raise SystemExit(
            f"Refusing to run: planned request count ({planned}) exceeds "
            f"--max-requests ({args.max_requests}). Lower --event-counts/--runs "
            f"or raise the cap explicitly (still bounded by the hard ceiling "
            f"{_HARD_REQUEST_CEILING})."
        )

    backend_url = os.environ.get("ARCLASP_K0B_BACKEND_URL")
    api_key = os.environ.get("ARCLASP_K0B_API_KEY")

    if args.dry_run:
        print("--dry-run: configuration validated, no network calls made.")
        print(f"ARCLASP_K0B_BACKEND_URL set: {bool(backend_url)}")
        print(f"ARCLASP_K0B_API_KEY set: {bool(api_key)}")
        return 0

    if not backend_url or not api_key:
        raise SystemExit(
            "Refusing to run: ARCLASP_K0B_BACKEND_URL and ARCLASP_K0B_API_KEY "
            "must both be set. Use --dry-run to validate configuration without them."
        )

    if any(marker in backend_url for marker in _PRODUCTION_URL_MARKERS):
        if os.environ.get("ARCLASP_K0B_ALLOW_PRODUCTION") != "1":
            raise SystemExit(
                "Refusing to run: backend URL looks production-like and "
                "ARCLASP_K0B_ALLOW_PRODUCTION=1 was not set. This script must "
                "default to a controlled development organization."
            )

    out_dir = pathlib.Path(args.out_dir) if args.out_dir else _default_out_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    async def _run_all() -> list[dict]:
        all_samples: list[dict] = []
        for event_count in event_counts:
            for _run_index in range(runs):
                # Re-init before every run: init() clears the SDK's cached
                # httpx.AsyncClient, so the first request of each run is an
                # honest cold-client sample and the rest of that run's
                # requests are honest warm-client samples.
                arclasp.init(
                    api_key=api_key,
                    backend_url=backend_url,
                    environment="development",
                    backend_timeout_seconds=30,
                )
                all_samples.extend(await _run_one(event_count, region_label))
        return all_samples

    try:
        # A fresh event loop per invocation of this script is intentional —
        # it gives an honest "cold" first-request classification for the
        # SDK's per-loop httpx.AsyncClient cache.
        samples = asyncio.run(_run_all())
    except _AbortBenchmark as exc:
        print(f"ABORTED: {exc.category}")
        return 1
    except (BackendUnavailableError, ActionDeniedError, ChainAutoPausedError,
            ChainTimeoutError, ArclaspKillSwitchError) as exc:
        print(f"ABORTED on governance stop condition: {type(exc).__name__}")
        return 1
    except Exception as exc:
        print(f"ABORTED on unexpected error: {type(exc).__name__}")
        return 1

    summary = _summarize(samples)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    json_path = out_dir / f"{timestamp}_k0b_latency.json"
    txt_path = out_dir / f"{timestamp}_k0b_latency_summary.txt"

    with json_path.open("w", encoding="utf-8") as f:
        json.dump(
            {
                "generated_at": timestamp,
                "event_counts": event_counts,
                "runs": runs,
                "region_label": region_label,
                "samples": samples,
                "summary": summary,
            },
            f,
            indent=2,
        )

    with txt_path.open("w", encoding="utf-8") as f:
        f.write(f"K0B latency benchmark — {timestamp}\n")
        f.write(f"event_counts={event_counts} runs={runs} region_label={region_label!r}\n\n")
        f.write(
            "NOTE: sdk_client_state describes only SDK httpx.AsyncClient "
            "object lifetime (client_created/client_reused), not TCP/TLS "
            "connection reuse. network_client_overhead_ms is a residual "
            "(total minus server-reported duration) that may include "
            "network transit, DNS/TCP/TLS, httpx processing, local "
            "scheduling, retry backoff, and response parsing — not pure "
            "network RTT.\n\n"
        )
        f.write("K2A " + _K2A_NESTING_NOTE + "\n\n")
        f.write(
            "K2A detailed metrics (auth/dbacq/lookup/verify/lastused/handler) "
            "are only ever populated for successful (2xx) requests to the "
            "three governed handlers (chain_start, event_record, "
            "chain_complete); absent detailed metrics on any other request "
            "(non-2xx response, an older deployment, or a route other than "
            "those three) are expected and are not reported as a failure.\n\n"
        )
        for group in summary["groups"]:
            f.write(
                f"{group['operation']:14s} events={group['event_count_requested']:>2} "
                f"{group['sdk_client_state']:14s} n={group['sample_count']:>3}\n"
            )
            f.write(
                f"  total p50/p95/p99={group['total_duration_ms']['p50']}/"
                f"{group['total_duration_ms']['p95']}/{group['total_duration_ms']['p99']} "
                f"retries_total={group['retries_total']} "
                f"negative_overhead_count={group['network_client_overhead_ms_negative_count']}\n"
            )
            for field in _K2A_SUMMARY_FIELDS:
                pct = group[field]
                if pct is None:
                    continue
                label = _K2A_FIELD_TO_LABEL[field]
                f.write(
                    f"  {label} p50={pct['p50']} p95={pct['p95']} "
                    f"p99={pct['p99']} n={pct['n']}\n"
                )

    print(f"Wrote {json_path}")
    print(f"Wrote {txt_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
