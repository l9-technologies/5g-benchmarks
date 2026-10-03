"""Read, hash, and verify immutable benchmark evidence."""
import hashlib
import json
from pathlib import Path
from .contracts import selected_schedule,validate_measurement

def read(path):
    return json.loads(Path(path).read_text(), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")


def digest(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def object_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def check_inputs(plan):
    if not Path(plan["cwd"]).is_dir():
        raise ValueError("working directory does not exist")
    for path, expected in plan["inputs"].items():
        if not Path(path).is_file() or digest(path) != expected:
            raise ValueError(f"input hash changed: {path}")


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


def verified_trials(directory,plan,manifest):
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
    return rows,problems
