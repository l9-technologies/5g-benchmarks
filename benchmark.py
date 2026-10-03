#!/usr/bin/env python3
"""Run and verify paired 5G comparisons. Python 3.11+, standard library only."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import platform
import random
import signal
import shutil
import statistics
import subprocess
import sys
import time

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


def read(path):
    return json.loads(Path(path).read_text(), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")


def digest(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def object_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


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


def check_inputs(plan):
    if not Path(plan["cwd"]).is_dir():
        raise ValueError("working directory does not exist")
    for path, expected in plan["inputs"].items():
        if not Path(path).is_file() or digest(path) != expected:
            raise ValueError(f"input hash changed: {path}")


def host_identity():
    result = {"system": platform.system(), "kernel": platform.release(), "machine": platform.machine(),
              "node": platform.node(), "cpu_count": os.cpu_count(), "python": platform.python_version()}
    for name, path in (("cpu", "/proc/cpuinfo"), ("memory", "/proc/meminfo")):
        if Path(path).exists():
            lines = Path(path).read_text().splitlines()
            keys = ("model name", "CPU implementer", "CPU architecture", "CPU part", "MemTotal")
            result[name] = sorted(set(line for line in lines if line.startswith(keys)))
    if hasattr(os, "sched_getaffinity"):
        result["affinity"] = sorted(os.sched_getaffinity(0))
    result["governors"] = sorted(set(p.read_text().strip() for p in Path("/sys/devices/system/cpu").glob("cpu*/cpufreq/scaling_governor")))
    for name, path in (("clocksource", "/sys/devices/system/clocksource/clocksource0/current_clocksource"),
                       ("boot_id", "/proc/sys/kernel/random/boot_id")):
        if Path(path).exists():
            result[name] = Path(path).read_text().strip()
    return result


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


def stop_group(process, grace=3):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass


def invoke(argv, cwd, env, timeout, log):
    with Path(log).open("wb") as handle:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124
        except KeyboardInterrupt:
            return 130
        finally:
            stop_group(proc, grace=30)


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


def evidence_hashes(directory, names):
    hashes = {}
    for name in names:
        if not isinstance(name, str):
            raise ValueError("evidence paths must be strings")
        file = directory / name
        if Path(name).is_absolute() or not file.resolve().is_relative_to(directory.resolve()) or file.is_symlink() or not file.is_file():
            raise ValueError(f"invalid evidence path: {name}")
        hashes[name] = digest(file)
    return hashes


def percentile(values, p):
    values = sorted(values)
    return values[max(0, math.ceil(p * len(values)) - 1)]


def paired_interval(a, b, seed):
    """Paired bootstrap CI for mean relative change, in percent. No zero division."""
    if any(v == 0 for v in b):
        return [0.0, 0.0] if a == b else None
    deltas = [100 * (x - y) / y for x, y in zip(a, b, strict=True)]
    return bootstrap_interval(deltas, seed)


def absolute_interval(a, b, seed):
    return bootstrap_interval([x - y for x, y in zip(a, b, strict=True)], seed)


def bootstrap_interval(deltas, seed):
    if min(deltas) == max(deltas):
        return [deltas[0], deltas[0]]
    rng = random.Random(seed)
    means = [statistics.mean(rng.choices(deltas, k=len(deltas))) for _ in range(4000)]
    return [percentile(means, .025), percentile(means, .975)]


def balanced_repetitions(candidate_count,minimum=6):
    return math.ceil(max(6,minimum)/candidate_count)*candidate_count


def neutral_statistics(a,b,seed,equivalence_percent,direction):
    # Linear quantiles preserve interval sign when candidate A and B are exchanged.
    def interval(deltas):
        rng=random.Random(seed)
        means=sorted(statistics.mean(rng.choices(deltas,k=len(deltas))) for _ in range(4000))
        def quantile(q):
            x=q*(len(means)-1);lo=math.floor(x);hi=math.ceil(x)
            return means[lo]*(hi-x)+means[hi]*(x-lo) if hi!=lo else means[lo]
        return [quantile(.025),quantile(.975)]
    absolute=interval([x-y for x,y in zip(a,b,strict=True)])
    relative=interval([200*(x-y)/(abs(x)+abs(y)) if x or y else 0 for x,y in zip(a,b,strict=True)])
    margin=(abs(statistics.median(a))+abs(statistics.median(b)))/2*equivalence_percent/100
    decision='no_clear_difference'
    if max(abs(v) for v in absolute)<=margin:decision='equivalent'
    elif absolute[0]>margin:decision='b_better' if direction=='lower' else 'a_better'
    elif absolute[1]<-margin:decision='a_better' if direction=='lower' else 'b_better'
    return {'absolute_interval':absolute,'symmetric_percent_interval':relative,'margin':margin,'decision':decision}


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


def neutral_comparisons(plan,groups):
    comparisons=[];checks=[];counts=plan['protocol']['ue_counts']
    for count in counts:
        for a,b in itertools.combinations(plan['candidates'],2):
            same=a['identity']==b['identity']
            scope='self' if same else 'core' if a['identity']['radio']==b['identity']['radio'] else 'radio_pair' if a['identity']['core']==b['identity']['core'] else None
            if scope is None:continue
            aa,bb=[sorted(groups.get((count,c['id']),[]),key=lambda r:r['trial']['repetition']) for c in (a,b)]
            for metric,(unit,direction) in METRICS.items():
                reasons=[]
                if len(aa)!=plan['repetitions'] or len(bb)!=plan['repetitions']:reasons.append('missing_trial')
                if any(r['status']!='passed' for r in aa+bb):reasons.append('failed_trial')
                if any(r['measurement'].get('features',{}).get(f)!='passed' for r in aa+bb for f in plan['required_features']):reasons.append('required_feature_failed')
                av,bv=[[r['measurement'].get('metrics',{}).get(metric) for r in rows] for rows in (aa,bb)]
                if not av or not bv or any(v is None for v in av+bv):reasons.append('missing_metric')
                stats=neutral_statistics(av,bv,plan['seed'],plan['equivalence_percent'],direction) if not reasons else None
                if same:
                    checks.append({'ue_count':count,'metric':metric,'a':a['id'],'b':b['id'],
                        'identity':a['identity'],'equivalent':bool(stats and stats['decision']=='equivalent'),
                        'absolute_interval':stats['absolute_interval'] if stats else None,
                        'interval_percent':stats['symmetric_percent_interval'] if stats else None,'reasons':reasons})
                    continue
                reasons+=comparison_reasons(metric,aa,bb,scope)
                comparisons.append({'scope':scope,'ue_count':count,'metric':metric,'unit':unit,'a':a['id'],'b':b['id'],
                    'a_median':statistics.median([v for v in av if v is not None]) if any(v is not None for v in av) else None,
                    'b_median':statistics.median([v for v in bv if v is not None]) if any(v is not None for v in bv) else None,
                    'a_minus_b_95_ci':stats['absolute_interval'] if stats else None,
                    'a_change_percent_95_ci':None,'symmetric_change_percent_95_ci':stats['symmetric_percent_interval'] if stats else None,
                    'equivalence_margin':stats['margin'] if stats else None,'statistical_decision':stats['decision'] if stats else None,
                    'reasons':sorted(set(reasons)),'decision':'comparison_blocked'})
    candidates={c['id']:c for c in plan['candidates']}
    for c in comparisons:
        relevant=[s for s in checks if s['ue_count']==c['ue_count'] and s['metric']==c['metric'] and s['identity'] in (candidates[c['a']]['identity'],candidates[c['b']]['identity'])]
        if len(relevant)!=2 or not all(s['equivalent'] for s in relevant):c['reasons'].append('unstable_baseline')
        c['reasons']=sorted(set(c['reasons']))
        if 'required_feature_failed' in c['reasons']:c['decision']='required_feature_failed'
        elif c['reasons']:c['decision']='comparison_blocked'
        elif plan['mode']!='live' or plan['protocol']['duration_seconds']<30:c['decision']='verification_only'
        else:c['decision']=c['statistical_decision']
    return comparisons,checks


def display(value):
    return 'missing' if value is None else f'{value:.4g}'


def display_interval(value):
    return 'unavailable' if value is None else ' to '.join(display(v) for v in value)


def make_report(directory):
    plan = validate_plan(read(directory / "plan.json"))
    manifest = read(directory / "manifest.json")
    if digest(directory / "report-tool.py") != manifest["report_tool_sha256"]:
        raise ValueError("report tool hash changed")
    if object_hash(plan) != manifest["plan_sha256"] or selected_schedule(plan, manifest) != manifest["schedule"]:
        raise ValueError("plan hash or schedule changed")
    rows, problems = [], []
    expected = {r["id"] for r in manifest["schedule"]}
    if {p.name for p in (directory / "trials").iterdir()} - expected:
        raise ValueError("unexpected trial directory")
    for entry in manifest["schedule"]:
        trial = directory / "trials" / entry["id"]
        if not (trial / "result.json").exists():
            problems.append(f"missing trial: {entry['id']}")
            continue
        result = read(trial / "result.json")
        if digest(trial / "result.json") != manifest.get("result_hashes", {}).get(entry["id"]):
            raise ValueError(f"result hash changed: {entry['id']}")
        if result["trial"] != entry or result["plan_sha256"] != manifest["plan_sha256"]:
            raise ValueError(f"trial identity changed: {entry['id']}")
        for name, expected_hash in result["hashes"].items():
            if evidence_hashes(trial, [name])[name] != expected_hash:
                raise ValueError(f"evidence hash changed: {entry['id']}/{name}")
        m = result["measurement"]
        if m:
            validate_measurement(m)
            if read(trial / "measurement.json") != m:
                raise ValueError("measurement changed")
        if result["status"] != "passed":
            problems.append(f"{entry['id']}: {result['error']}")
        rows.append(result)
    groups = {}
    for row in rows:
        t = row["trial"]
        groups.setdefault((t["ue_count"], t["candidate"]), []).append(row)
    summaries, comparisons, self_checks = [], [], []
    for (count, candidate), trials in sorted(groups.items()):
        measurements = [r["measurement"] for r in trials if r["status"] == "passed"]
        summaries.append({"ue_count": count, "candidate": candidate, "attempts": len(trials),
            "passed": len(measurements), "failed": sum(r["status"] != "passed" for r in trials),
            "features": {f: {s: sum(r["measurement"].get("features", {}).get(f, "untested") == s for r in trials)
                             for s in ("passed", "failed", "unsupported", "untested")} for f in FEATURES},
            "metrics": {metric: {"unit": METRICS[metric][0], "median": statistics.median(values),
                         "minimum": min(values), "maximum": max(values), "samples": values}
                        for metric in METRICS if (values := [m["metrics"][metric] for m in measurements if metric in m["metrics"]])}})
    if plan.get('method_version',1)==2:
        comparisons,self_checks=neutral_comparisons(plan,groups)
    else:
        for count in plan["protocol"]["ue_counts"]:
            for a, b in itertools.combinations(plan["candidates"], 2):
                same = a["identity"] == b["identity"]
                scope = "self" if same else "core" if a["identity"]["radio"] == b["identity"]["radio"] else "radio_pair" if a["identity"]["core"] == b["identity"]["core"] else None
                if scope is None:
                    continue
                aa, bb = [sorted(groups.get((count, c["id"]), []), key=lambda r: r["trial"]["repetition"]) for c in (a, b)]
                good = len(aa) == len(bb) == plan["repetitions"] and all(r["status"] == "passed" for r in aa + bb)
                for metric, (unit, direction) in METRICS.items():
                    if not good or any(metric not in r["measurement"]["metrics"] for r in aa + bb):
                        continue
                    av, bv = [[r["measurement"]["metrics"][metric] for r in rr] for rr in (aa, bb)]
                    ci = paired_interval(av, bv, plan["seed"])
                    absolute_ci = absolute_interval(av, bv, plan["seed"])
                    margin = statistics.median(bv) * plan["equivalence_percent"] / 100
                    if same:
                        self_checks.append({"ue_count": count, "metric": metric, "a": a["id"], "b": b["id"],
                            "interval_percent": ci, "absolute_interval": absolute_ci,
                            "equivalent": max(abs(x) for x in absolute_ci) <= margin})
                    else:
                        decision = "no_clear_difference"
                        if absolute_ci[0] > margin:
                            decision = "b_better" if direction == "lower" else "a_better"
                        elif absolute_ci[1] < -margin:
                            decision = "a_better" if direction == "lower" else "b_better"
                        comparisons.append({"scope": scope, "ue_count": count, "metric": metric, "unit": unit,
                            "a": a["id"], "b": b["id"], "a_median": statistics.median(av), "b_median": statistics.median(bv),
                            "a_change_percent_95_ci": ci, "a_minus_b_95_ci": absolute_ci, "decision": decision})
        for comparison in comparisons:
            checks = [s for s in self_checks if s["ue_count"] == comparison["ue_count"] and s["metric"] == comparison["metric"]]
            if not checks or not all(s["equivalent"] for s in checks):
                comparison["decision"] = "unstable_baseline"
            elif plan["mode"] != "live" or plan["protocol"]["duration_seconds"] < 30:
                comparison["decision"] = "verification_only"
    stable = bool(self_checks) and all(s["equivalent"] for s in self_checks)
    status = "incomplete" if problems else "verification_only" if plan["mode"] != "live" or plan["protocol"]["duration_seconds"] < 30 else "measured" if stable else "unstable_baseline"
    if manifest.get("shard", {}).get("count", 1) > 1:
        status = "shard_complete" if not problems else "incomplete"
        for comparison in comparisons:
            comparison["decision"] = "shard_only"
    metric_failures = [{"trial": r["trial"]["id"], "metric": k, "error": v}
                       for r in rows for k, v in r["measurement"].get("metric_errors", {}).items()]
    feature_failures = [{"trial": r["trial"]["id"], "feature": k}
                        for r in rows for k, v in r["measurement"].get("features", {}).items() if v == "failed"]
    if status == "measured" and (metric_failures or feature_failures):
        status = "measured_with_failures"
    required_failures=[{'trial':r['trial']['id'],'feature':f,'observed':r['measurement'].get('features',{}).get(f,'untested')} for r in rows for f in plan.get('required_features',[]) if r['measurement'].get('features',{}).get(f)!='passed']
    report = {"schema_version": 1,"method_version":plan.get('method_version',1),'required_feature_failures':required_failures, "status": status, "publishable": False,
        "plan_sha256": manifest["plan_sha256"], "host": manifest["host"], "protocol": plan["protocol"],
        "trial_count": len(rows), "hosts": manifest.get("hosts", [manifest["host"]]), "problems": problems, "metric_failures": metric_failures,
        "feature_failures": feature_failures, "summaries": summaries,
        "self_checks": self_checks, "comparisons": comparisons,
        "method": "Paired bootstrap of mean difference and relative change; 4000 resamples; seed in plan; 95% intervals per metric. Decisions use the absolute interval and the declared margin times the reference median. Intervals are exploratory, not simultaneous guarantees. No overall rank.",
        "scope": "Radio pair comparisons measure UE and gNB together. UE and gNB resource values are separate. No physical radio claim."}
    if plan.get('method_version',1)==2:
        report['method']='Method 2: every candidate has a repeated baseline; complete order rotations; paired bootstrap, 4000 resamples; symmetric mean-median equivalence margin and symmetric relative differences. Each interval is exploratory and per metric. Required feature failures block comparisons. All planned comparisons are shown. No overall rank or independent certification.'
        report['independent_verification']='not_performed'
        report['comparison_claims_ready']=bool(comparisons) and not required_failures and not problems and all(c['decision'] in ('a_better','b_better','equivalent','no_clear_difference') for c in comparisons)
    write(directory / "report.json", report)
    lines = ["# 5G comparison", "", f"Status: {status}. Trials: {len(rows)}.", "", report["scope"], "", report["method"], "",
             "| Scope | UEs | Metric | A | B | A median | B median | A minus B, 95% interval | Result |",
             "|---|---:|---|---|---|---:|---:|---|---|"]
    lines += [f"| {c['scope']} | {c['ue_count']} | {c['metric']} ({c['unit']}) | {c['a']} | {c['b']} | {display(c['a_median'])} | {display(c['b_median'])} | {display_interval(c['a_minus_b_95_ci'])} | {c['decision']} |" for c in comparisons]
    lines += ["", "## Measurements", "", "Values from trials that passed measurement checks. Failed metrics have no estimate.", "",
              "| Candidate | UEs | Metric | Samples | Median | Minimum | Maximum |",
              "|---|---:|---|---:|---:|---:|---:|"]
    lines += [f"| {s['candidate']} | {s['ue_count']} | {name} ({m['unit']}) | {len(m['samples'])} | {m['median']:.4g} | {m['minimum']:.4g} | {m['maximum']:.4g} |"
              for s in summaries for name, m in s["metrics"].items()]
    lines += ["", "## Feature evidence", "", "Counts include failed trials. A feature result applies only to this workload.", "",
              "| Candidate | UEs | Feature | Passed | Failed | Unsupported | Untested |",
              "|---|---:|---|---:|---:|---:|---:|"]
    lines += [f"| {s['candidate']} | {s['ue_count']} | {f} | {v['passed']} | {v['failed']} | {v['unsupported']} | {v['untested']} |"
              for s in summaries for f, v in s["features"].items()]
    lines += ["", "## Missing or failed evidence", ""] + ([f"- {p}" for p in problems] or ["None."])
    lines += ["", "## Required feature failures", ""] + ([f"- {p['trial']}: {p['feature']}: {p['observed']}" for p in required_failures] or ["None."])
    lines += ["", "## Failed measurements", ""] + ([f"- {p['trial']}: {p['metric']}: {p['error']}" for p in metric_failures] or ["None."])
    (directory / "report.md").write_text("\n".join(lines) + "\n")
    return report


def run(plan_path, output, shard_index=0, shard_count=1):
    plan = validate_plan(read(plan_path))
    if not 1 <= shard_count <= plan["repetitions"] or not 0 <= shard_index < shard_count:
        raise ValueError("invalid shard index or count")
    check_inputs(plan)
    output.mkdir(parents=True, exist_ok=False)
    (output / "trials").mkdir()
    shutil.copyfile(Path(__file__), output / "report-tool.py")
    write(output / "plan.json", plan)
    manifest = {"plan_sha256": object_hash(plan), "schedule": schedule(plan), "host": host_identity(), "result_hashes": {},
                "report_tool_sha256": digest(output / "report-tool.py")}
    manifest["shard"] = {"index": shard_index, "count": shard_count}
    if shard_count > 1:
        manifest["schedule"] = [r for r in manifest["schedule"] if (r["repetition"] - 1) % shard_count == shard_index]
    write(output / "manifest.json", manifest)
    candidates = {c["id"]: c for c in plan["candidates"]}
    # All plans for this prepared lab use one lock, regardless of output path.
    lock_path = Path("/var/lock/5g-benchmark.lock") if plan["mode"] == "live" else Path(plan["cwd"]) / ".benchmark.lock"
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for entry in manifest["schedule"]:
            directory = output / "trials" / entry["id"]
            directory.mkdir()
            result = {"trial": entry, "plan_sha256": manifest["plan_sha256"], "status": "failed", "error": "", "measurement": {}, "hashes": {}}
            code = None
            try:
                check_inputs(plan)
                if host_identity() != manifest["host"]:
                    raise ValueError("host identity changed")
                env = dict(os.environ, BENCH_RESULT_DIR=str(directory), BENCH_PLAN=str(output / "plan.json"),
                           BENCH_CANDIDATE=json.dumps(candidates[entry["candidate"]]), BENCH_UE_COUNT=str(entry["ue_count"]),
                           BENCH_REPETITION=str(entry["repetition"]))
                code = invoke(plan["driver"], plan["cwd"], env, plan["timeout_seconds"], directory / "driver.log")
                if (directory / "measurement.json").exists():
                    m = read(directory / "measurement.json")
                    validate_measurement(m)
                    if plan.get('method_version',1)==2:
                        missing=[f for f in plan['required_features'] if m.get('features',{}).get(f)!='passed']
                        if missing:
                            shutil.copyfile(directory/'measurement.json',directory/'driver-measurement.json')
                            m['evidence'].append('driver-measurement.json')
                            m['quality_errors'].append('required feature checks failed: '+', '.join(missing))
                            for f in missing:m['features'][f]='failed'
                            write(directory/'measurement.json',m)
                    result["measurement"] = m
                    result["hashes"] = evidence_hashes(directory, m["evidence"] + ["measurement.json", "driver.log"])
                    if m["kind"] != plan["mode"] or not m["cleanup_ok"] or m["quality_errors"]:
                        raise ValueError(f"measurement quality failed: {m['quality_errors']}; cleanup={m['cleanup_ok']}")
                if code != 0 or not result["measurement"]:
                    raise ValueError(f"driver failed: exit {code}")
                check_inputs(plan)
                if host_identity() != manifest["host"]:
                    raise ValueError("host identity changed during the trial")
                result["status"] = "passed"
            except (OSError, ValueError, KeyError, TypeError) as error:
                result["error"] = str(error)
            except KeyboardInterrupt:
                code = 130
                result["error"] = "run interrupted"
            write(directory / "result.json", result)
            manifest["result_hashes"][entry["id"]] = digest(directory / "result.json")
            write(output / "manifest.json", manifest)
            label = "measurement saved" if result["status"] == "passed" else "failed"
            print(f"{entry['id']}: {label}; {len(result['measurement'].get('metric_errors', {}))} failed metrics", flush=True)
            # Absence of a cleanup receipt is unsafe. Preserve this failure and
            # all planned missing trials, then stop before starting another lab.
            if code == 130 or not result["measurement"].get("cleanup_ok", False):
                break
    return make_report(output)


def selected_schedule(plan, manifest):
    shard = manifest.get("shard", {"index": 0, "count": 1})
    if type(shard.get("count")) is not int or type(shard.get("index")) is not int or not 1 <= shard["count"] <= plan["repetitions"] or not 0 <= shard["index"] < shard["count"]:
        raise ValueError("invalid shard metadata")
    return [r for r in schedule(plan) if (r["repetition"] - 1) % shard["count"] == shard["index"]]


def host_class(host):
    result = {k: v for k, v in host.items() if k not in ("node", "boot_id")}
    # Boot reservations can change MemTotal by a few KiB on equal hosts.
    if "memory" in result:
        result["memory"] = [f"MemTotal: {round(int(line.split()[1]) / 1024)} MiB"
                            if line.startswith("MemTotal:") else line for line in result["memory"]]
    return result


def merge(directories, output):
    if output.exists():
        raise ValueError("output directory already exists")
    plans = [validate_plan(read(d / "plan.json")) for d in directories]
    manifests = [read(d / "manifest.json") for d in directories]
    if not plans or any(object_hash(p) != object_hash(plans[0]) for p in plans):
        raise ValueError("shards use different plans")
    if any(host_class(m["host"]) != host_class(manifests[0]["host"]) for m in manifests):
        raise ValueError("shards use different host classes")
    expected = {r["id"] for r in schedule(plans[0])}
    seen = set()
    for d, m in zip(directories, manifests, strict=True):
        make_report(d)  # Verify every result and evidence hash before copying.
        for r in m["schedule"]:
            if r["id"] in seen:
                raise ValueError("duplicate trial in shards")
            if not (d / "trials" / r["id"] / "result.json").is_file():
                raise ValueError("missing trial in shard")
            seen.add(r["id"])
    if seen != expected:
        raise ValueError("missing shard trials")
    pairs = {}
    for m in manifests:
        for r in m["schedule"]:
            key = (r["ue_count"], r["repetition"])
            if key in pairs and pairs[key] != m["host"]:
                raise ValueError("paired candidates use different hosts")
            pairs[key] = m["host"]
    output.mkdir()
    (output / "trials").mkdir()
    shutil.copyfile(directories[0] / "report-tool.py", output / "report-tool.py")
    write(output / "plan.json", plans[0])
    combined = {**manifests[0], "shard": {"index": 0, "count": 1},
        "schedule": schedule(plans[0]), "result_hashes": {}, "trial_hosts": {}, "hosts": []}
    for d, m in zip(directories, manifests, strict=True):
        combined["hosts"].append(m["host"])
        for r in m["schedule"]:
            shutil.copytree(d / "trials" / r["id"], output / "trials" / r["id"])
            combined["result_hashes"][r["id"]] = m["result_hashes"][r["id"]]
            combined["trial_hosts"][r["id"]] = m["host"]
    write(output / "manifest.json", combined)
    return make_report(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("run", "validate"):
        p = sub.add_parser(name)
        p.add_argument("plan", type=Path)
        if name == "run":
            p.add_argument("--output", required=True, type=Path)
            p.add_argument("--shard-index", type=int, default=0)
            p.add_argument("--shard-count", type=int, default=1)
    sub.add_parser("report").add_argument("directory", type=Path)
    p = sub.add_parser("merge")
    p.add_argument("directories", nargs="+", type=Path)
    p.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.action == "validate":
            plan = validate_plan(read(args.plan))
            check_inputs(plan)
            print(json.dumps({"status": "ready", "trials": len(schedule(plan))}))
            return 0
        if args.action == "run":
            report = run(args.plan.resolve(), args.output.resolve(), args.shard_index, args.shard_count)
        elif args.action == "merge":
            report = merge([d.resolve() for d in args.directories], args.output.resolve())
        else:
            report = make_report(args.directory.resolve())
        print(json.dumps({"status": report["status"], "trial_count": report["trial_count"]}))
        return 2 if report["status"] == "incomplete" else 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"benchmark: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
