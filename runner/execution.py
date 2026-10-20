"""Run, schedule, and merge complete paired measurements."""
import fcntl
import json
import os
from pathlib import Path
import platform
import signal
import shutil
import subprocess
import time
from .contracts import schedule,validate_measurement,validate_plan,measurement_failures
from .evidence import check_inputs,digest,evidence_hashes,object_hash,read,write,verified_trials
from .reporting import make_report
from .snapshot import save_report_tool
from . import host_control

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


def run(plan_path, output, shard_index=0, shard_count=1, *, resume=False, max_trials=None, report_on_finish=True, max_total_trials=None, recover_only=False):
    plan = validate_plan(read(plan_path))
    if not 1 <= shard_count <= plan["repetitions"] or not 0 <= shard_index < shard_count:
        raise ValueError("invalid shard index or count")
    check_inputs(plan)
    if max_trials is not None and (type(max_trials) is not int or max_trials < 1):
        raise ValueError("max trials must be a positive integer")
    if max_total_trials is not None and (type(max_total_trials) is not int or not 1<=max_total_trials<=len(schedule(plan))):
        raise ValueError('total trial limit must select a nonempty schedule prefix')
    candidates = {c["id"]: c for c in plan["candidates"]}
    # All plans for this prepared lab use one lock, regardless of output path.
    lock_path = Path("/var/lock/5g-benchmark.lock") if plan["mode"] == "live" else Path(plan["cwd"]) / ".benchmark.lock"
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if resume:
            manifest = read(output / "manifest.json")
            if read(output / "plan.json") != plan or manifest.get("shard") != {"index": shard_index, "count": shard_count}:
                raise ValueError("resume plan or shard changed")
            if host_identity() != manifest["host"]:
                raise ValueError("host identity changed; use a new cohort")
            # A complete result can survive a crash just before its manifest
            # checkpoint. Verify all raw evidence and cleanup before adoption.
            pending={entry['id']:digest(output/'trials'/entry['id']/'result.json') for entry in manifest['schedule']
                     if entry['id'] not in manifest['result_hashes'] and (output/'trials'/entry['id']/'result.json').is_file()}
            candidate={**manifest,'result_hashes':{**manifest['result_hashes'],**pending}}
            rows, _ = verified_trials(output, plan, candidate)
            if pending:
                if any(not row['measurement'].get('cleanup_ok') for row in rows if row['trial']['id'] in pending):
                    raise ValueError('incomplete cleanup in an uncommitted result; review required')
                folder=output/'recoveries';folder.mkdir(exist_ok=True)
                write(folder/(str(time.time_ns())+'.json'),{'reason':'complete results recovered before manifest checkpoint','adopted_result_hashes':pending})
                manifest=candidate;write(output/'manifest.json',manifest)
            if any(not r["measurement"].get("cleanup_ok", False) for r in rows):
                raise ValueError("resume refused: a previous trial has no successful cleanup receipt")
            for entry in manifest["schedule"]:
                directory = output / "trials" / entry["id"]
                if directory.exists() and not (directory / "result.json").exists():
                    raise ValueError("resume refused: interrupted trial needs cleanup review: " + entry["id"])
        else:
            output.mkdir(parents=True, exist_ok=False)
            (output / "trials").mkdir()
            save_report_tool(output / "report-tool.py")
            write(output / "plan.json", plan)
            manifest = {"plan_sha256": object_hash(plan), "schedule": schedule(plan), "host": host_identity(), "result_hashes": {},
                        "report_tool_sha256": digest(output / "report-tool.py"), "shard": {"index": shard_index, "count": shard_count}}
            if shard_count > 1:
                manifest["schedule"] = [r for r in manifest["schedule"] if (r["repetition"] - 1) % shard_count == shard_index]
            write(output / "manifest.json", manifest)
        collected = 0
        for entry in manifest["schedule"]:
            if recover_only:break
            if max_total_trials is not None and len(manifest['result_hashes'])>=max_total_trials:break
            directory = output / "trials" / entry["id"]
            if entry["id"] in manifest["result_hashes"]:
                continue
            if max_trials is not None and collected >= max_trials:
                break
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
                if 'host_control' in plan:
                    (directory/'driver.log').touch()
                    code=host_control.invoke(plan['host_control'],plan['driver'],plan['cwd'],env,plan['timeout_seconds'],directory/'driver.log',directory/'host')
                else:
                    code = invoke(plan["driver"], plan["cwd"], env, plan["timeout_seconds"], directory / "driver.log")
                if 'host_control' in plan and not (directory/'measurement.json').exists():
                    lease=read(directory/'host/lease.json')
                    write(directory/'measurement.json',{'kind':plan['mode'],'cleanup_ok':bool(lease.get('restored')),
                        'quality_errors':lease.get('quality_errors',[])+['host-controlled driver did not produce a measurement'],
                        'features':{},'metrics':{},'evidence':['driver.log']})
                if (directory / "measurement.json").exists():
                    m = read(directory / "measurement.json")
                    validate_measurement(m)
                    if 'host_control' in plan:host_control.annotate(directory,m)
                    failures=measurement_failures(plan,m,entry['ue_count'])
                    if failures:
                        shutil.copyfile(directory/'measurement.json',directory/'driver-measurement.json')
                        m['evidence'].append('driver-measurement.json')
                        m['quality_errors'].extend(failures)
                        if plan.get('method_version',1)==2:
                            for feature in plan['required_features']:
                                if m.get('features',{}).get(feature)!='passed':m['features'][feature]='failed'
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
            result["driver_return_code"]=code
            write(directory / "result.json", result)
            manifest["result_hashes"][entry["id"]] = digest(directory / "result.json")
            write(output / "manifest.json", manifest)
            collected += 1
            label = "measurement saved" if result["status"] == "passed" else "failed"
            print(f"{entry['id']}: {label}; {len(result['measurement'].get('metric_errors', {}))} failed metrics", flush=True)
            # Absence of a cleanup receipt is unsafe. Preserve this failure and
            # all planned missing trials, then stop before starting another lab.
            if code == 130 or not result["measurement"].get("cleanup_ok", False):
                break
    return make_report(output) if report_on_finish else {"trial_count": len(manifest["result_hashes"]),'new_trials':collected}


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
