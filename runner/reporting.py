"""Produce complete reports from verified evidence."""
import itertools
import statistics
from .contracts import FEATURES,METRICS,comparison_reasons,validate_plan
from .evidence import read,write,verified_trials
from .statistics import absolute_interval,neutral_statistics,paired_interval

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
    rows,problems=verified_trials(directory,plan,manifest)
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
