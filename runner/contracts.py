"""Shared plan, measurement, and comparison requirements."""
import itertools
import math
import random

# Units and event boundaries are shared by every live adapter.
METRICS = {
    **{f"{mode}_host_cpu_seconds_per_gbit": ("host CPU seconds/Gbit", "lower") for mode in ("tcp_ul", "tcp_dl", "udp_ul", "udp_dl")},
    "host_idle_memory_used_bytes": ("host bytes", "lower"),
    "host_memory_used_peak_bytes": ("host bytes", "lower"),
    "attach_ready_ms": ("ms", "lower"),
    "stack_traffic_ready_ms": ("ms", "lower"),
    **{f"{part}_p{p}_ms": ("ms", "lower") for part in ("registration", "session") for p in (50, 95, 99)},
    **{f"{part}_per_second": ("procedures/s", "higher") for part in ("registration", "session")},
    **{f"{mode}_rtt_p{p}_ms": ("ms", "lower") for mode in ("tcp_ul", "tcp_dl", "udp_ul", "udp_dl") for p in (50, 95, 99)},
    **{f"{mode}_ping_loss_percent": ("percent", "lower") for mode in ("tcp_ul", "tcp_dl", "udp_ul", "udp_dl")},
    **{f"{mode}_core_cpu_seconds_per_gbit": ("CPU seconds/Gbit", "lower") for mode in ("tcp_ul", "tcp_dl", "udp_ul", "udp_dl")},
    **{f"udp_{d}_jitter_ms": ("ms", "lower") for d in ("ul", "dl")},
    **{f"tcp_{d}_retransmits": ("retransmits", "lower") for d in ("ul", "dl")},
    "attach_success_percent": ("percent", "higher"),
    "rtt_p50_ms": ("ms", "lower"), "rtt_p95_ms": ("ms", "lower"),
    "rtt_p99_ms": ("ms", "lower"), "ping_loss_percent": ("percent", "lower"),
    "tcp_ul_mbps": ("Mbit/s", "higher"), "tcp_dl_mbps": ("Mbit/s", "higher"),
    "udp_ul_mbps": ("Mbit/s", "higher"), "udp_dl_mbps": ("Mbit/s", "higher"),
    "udp_ul_loss_percent": ("percent", "lower"), "udp_dl_loss_percent": ("percent", "lower"),
    **{f"{part}_{metric}": spec for part in ("core", "ue", "gnb") for metric, spec in {
        "cpu_seconds": ("CPU seconds", "lower"), "rss_peak_bytes": ("bytes", "lower"),
        "pss_peak_bytes": ("bytes", "lower"),
        "idle_rss_bytes": ("bytes", "lower"), "idle_pss_bytes": ("bytes", "lower"),
        "binary_bytes": ("bytes", "lower"),
    }.items()},
}
FEATURES = ("registration", "pdu_session", "n2_ngap", "n3_gtpu", "external_n6",
            "tcp", "udp", "distinct_ue_addresses", "multi_ue_isolation", "cleanup", "paging", "handover",
            "session_release", "deregistration", "restart_recovery", "ipv6",
            "multiple_sessions", "slice_isolation", "malformed_messages", "physical_radio")


def number(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be a finite number from {low} through {high}")


def validate_plan(plan):
    if plan.get("mode")=="live" and plan.get("method_version")!=2:
        raise ValueError("new live runs require method 2; preserve historical reports with their saved tool")
    if plan.get("schema_version") != 1 or plan.get("mode") not in ("live", "fixture"):
        raise ValueError("invalid plan version or mode")
    for key, low, high in (("repetitions", 6, 100), ("timeout_seconds", 1, 86400),
                           ("seed", 0, 2**32 - 1), ("equivalence_percent", 0.1, 50)):
        number(plan.get(key), key, low, high)
    if any(type(plan[key]) is not int for key in ("repetitions", "seed")):
        raise ValueError("repetitions and seed must be integers")
    protocol = plan["protocol"]
    counts = protocol["ue_counts"]
    if not counts or len(set(counts)) != len(counts) or any(type(n) is not int or not 1 <= n <= 512 for n in counts):
        raise ValueError("UE counts must be distinct integers from 1 through 512")
    for key, low, high in (("duration_seconds", 2, 3600), ("warmup_seconds", 1, 300),
                           ("packet_bytes", 64, 1400), ("offered_mbps_per_ue", 0.01, 10000)):
        number(protocol.get(key), key, low, high)
    if not isinstance(plan["driver"], list) or not plan["driver"] or not all(isinstance(x, str) and x for x in plan["driver"]):
        raise ValueError("driver must be a command array")
    if not isinstance(plan.get("cwd"), str) or not plan["cwd"] or not plan.get("inputs"):
        raise ValueError("a working directory and pinned inputs are required")
    candidates = plan["candidates"]
    if not 2 <= len(candidates) <= 20:
        raise ValueError("supply candidates and one repeated baseline (2 to 20 entries)")
    ids = set()
    for c in candidates:
        if not isinstance(c["id"], str) or not c["id"].replace("-", "").isalnum() or c["id"] in ids:
            raise ValueError("candidate IDs must be unique letters, digits, and hyphens")
        ids.add(c["id"])
        if set(c["identity"]) != {"core", "radio"} or not all(isinstance(v, str) and v for v in c["identity"].values()):
            raise ValueError("each candidate needs exact core and radio identities")
    if not any(a["identity"] == b["identity"] for a, b in itertools.combinations(candidates, 2)):
        raise ValueError("a repeated baseline with identical binaries is required")
    if plan.get("method_version",1)==2:
        identities=[tuple(sorted(c['identity'].items())) for c in candidates]
        if any(identities.count(identity)!=2 for identity in set(identities)):
            raise ValueError("method 2 requires exactly one repeat of every core/radio candidate")
        if plan['repetitions']%len(candidates):
            raise ValueError("method 2 requires complete candidate-order rotations")
        if plan['mode']=='live':
            registry=plan.get('requirements',{}).get('requirements',[])
            if not registry or [r.get('feature') for r in registry]!=plan.get('required_features') or any(any(not r.get(k) for k in ('release','specification','clause','source','test')) for r in registry):
                raise ValueError('live plans need the required feature registry and 3GPP references')
        required=plan.get('required_features')
        if not isinstance(required,list) or not required or len(set(required))!=len(required) or set(required)-set(FEATURES):
            raise ValueError("method 2 needs declared required feature checks")
    elif plan.get("method_version",1)!=1:
        raise ValueError("unknown method version")
    return plan


def schedule(plan):
    rng = random.Random(plan["seed"])
    rows = []
    for count in plan["protocol"]["ue_counts"]:
        order = sorted(plan["candidates"],key=lambda c:(c['identity']['core'],c['identity']['radio'],c['id'].endswith('-repeat'))) if plan.get('method_version',1)==2 else list(plan['candidates'])
        rng.shuffle(order)
        for repetition in range(plan["repetitions"]):
            # A randomized starting order, then a balanced rotation.
            shift = repetition % len(order)
            for c in order[shift:] + order[:shift]:
                rows.append({"id": f"u{count}-r{repetition + 1}-{c['id']}",
                             "candidate": c["id"], "ue_count": count, "repetition": repetition + 1})
    return rows


def validate_measurement(m):
    if not isinstance(m, dict):
        raise ValueError("measurement must be an object")
    if m.get("kind") not in ("live", "fixture") or type(m.get("cleanup_ok")) is not bool:
        raise ValueError("measurement needs kind and cleanup_ok")
    if not isinstance(m.get("quality_errors"), list) or not all(isinstance(x, str) for x in m["quality_errors"]):
        raise ValueError("invalid quality errors")
    if not isinstance(m.get("metrics"), dict) or set(m["metrics"]) - METRICS.keys():
        raise ValueError("unknown metric")
    for name, value in m["metrics"].items():
        number(value, name, 0, 100 if name.endswith("percent") else 1e18)
    if not isinstance(m.get("features"), dict) or set(m["features"]) - set(FEATURES):
        raise ValueError("unknown feature")
    if any(value not in ("passed", "failed", "unsupported", "untested") for value in m["features"].values()):
        raise ValueError("invalid feature result")
    if not isinstance(m.get("evidence"), list) or not m["evidence"]:
        raise ValueError("measurement needs evidence")
    errors = m.get("metric_errors", {})
    if not isinstance(errors, dict) or set(errors) - METRICS.keys() or any(not isinstance(v, str) or not v for v in errors.values()):
        raise ValueError("invalid metric errors")
    if set(errors) & m["metrics"].keys():
        raise ValueError("a metric cannot have both a value and an error")
    if m["kind"] == "live" and not m["quality_errors"] and set(m["metrics"]) | set(errors) != set(METRICS):
        raise ValueError("live measurement is missing required metric outcomes")


def balanced_repetitions(candidate_count,minimum=6):
    return math.ceil(max(6,minimum)/candidate_count)*candidate_count


def comparison_reasons(metric,aa,bb,scope):
    keys=['service_profile']
    if metric.startswith(('registration_','attach_','stack_')):keys.append('authentication_profile')
    if metric.startswith(('core_','host_')) or '_host_cpu_' in metric:keys.append('durability_profile')
    if metric.startswith(('core_','host_','ue_','gnb_')) or '_host_cpu_' in metric:keys.append('resource_scope')
    if scope=='radio_pair':keys.append('radio_capability_profile')
    reasons=[]
    if metric.endswith('binary_bytes') or metric.startswith('core_') or '_core_cpu_' in metric:
        reasons.append('incomplete_resource_boundary')
    for key in keys:
        values=[r['measurement'].get('comparison_state',{}).get(key) for r in aa+bb]
        if not values or any(v is None or v=='' for v in values):reasons.append('incomplete_comparison_state')
        elif any(v!=values[0] for v in values):reasons.append('comparison_state_mismatch')
    return sorted(set(reasons))


def selected_schedule(plan, manifest):
    shard = manifest.get("shard", {"index": 0, "count": 1})
    if type(shard.get("count")) is not int or type(shard.get("index")) is not int or not 1 <= shard["count"] <= plan["repetitions"] or not 0 <= shard["index"] < shard["count"]:
        raise ValueError("invalid shard metadata")
    return [r for r in schedule(plan) if (r["repetition"] - 1) % shard["count"] == shard["index"]]
