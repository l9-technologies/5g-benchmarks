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
from .contracts import schedule,validate_measurement,validate_plan
from .evidence import check_inputs,digest,evidence_hashes,object_hash,read,write
from .reporting import make_report
from .snapshot import save_report_tool

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


def run(plan_path, output, shard_index=0, shard_count=1):
    plan = validate_plan(read(plan_path))
    if not 1 <= shard_count <= plan["repetitions"] or not 0 <= shard_index < shard_count:
        raise ValueError("invalid shard index or count")
    check_inputs(plan)
    output.mkdir(parents=True, exist_ok=False)
    (output / "trials").mkdir()
    save_report_tool(output / "report-tool.py")
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
