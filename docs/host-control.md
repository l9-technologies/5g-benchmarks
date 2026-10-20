# Shared lab control

Use one lab file for artifact locations, CPU roles, host admission, and restoration. Test profiles continue to select workloads and measurements. Keep the lab file and host receipts outside the public repository. Do not put secrets in command arguments or output.

## Check the installation

```sh
python3 benchmark.py lab doctor profiles/mini-weekly.json --lab /opt/benchmark/lab.json --output /opt/benchmark/admission-001
```

The doctor checks the profile, installed artifacts, pinned command inputs, available disk and memory, and the host snapshot. It compiles all declared cases without starting traffic. It saves a `ready-lab.json` that reuses the prepared artifacts. It does not pause CI, build a core, or certify generator capacity. A failed command keeps its diagnostic output.

On Linux, memory admission uses `MemAvailable`. On macOS, it uses free, inactive, and speculative pages. These are admission estimates, not core memory measurements. Apply the declared memory accounting contract separately.

```sh
sudo python3 benchmark.py study collect profiles/mini-weekly.json --lab /opt/benchmark/admission-001/ready-lab.json --output /opt/benchmark/results/week-001
sudo python3 benchmark.py study resume /opt/benchmark/results/week-001
python3 benchmark.py study status /opt/benchmark/results/week-001
```

The collect command continues through the declared study. Resume continues saved work after an interruption. Use the existing `--max-trials` option only when an outer scheduler needs a smaller block. Each new trial receives a bounded reservation. This first controller uses one reservation per trial so restoration failures affect that trial before its result is committed. It can require more admission time than a batch reservation. It does not change candidate ordering or permit concurrent timed trials.

An unbounded collect or resume writes reports and verifies the archive after all declared checks and trials finish. The existing executor still runs all declared candidates, repeats, and confirmation trials. It does not retry failed trials to obtain a pass. Status includes counts and `next_action`.

## Host configuration

Add `host_control` beside `native` or `prepared_plan` in the existing lab file. Commands are argument arrays, not shell text. The following example assumes that the machine already has an installed host adapter:

```json
{
  "schema_version": 1,
  "locks": ["/opt/benchmark/maintenance.lock", "/opt/benchmark/timing.lock"],
  "max_seconds": 900,
  "restore_seconds": 90,
  "sample_seconds": 5,
  "minimum_free_bytes": 3221225472,
  "minimum_available_memory_bytes": 2147483648,
  "inputs": {"/opt/benchmark/host-adapter.py": "REPLACE_WITH_SHA256"},
  "commands": {
    "snapshot": ["/usr/bin/python3", "/opt/benchmark/host-adapter.py", "snapshot", "{lease}"],
    "acquire": ["/usr/bin/python3", "/opt/benchmark/host-adapter.py", "acquire", "{lease}"],
    "sample": ["/usr/bin/python3", "/opt/benchmark/host-adapter.py", "sample", "{lease}"],
    "restore": ["/usr/bin/python3", "/opt/benchmark/host-adapter.py", "restore", "{lease}"],
    "verify_restored": ["/usr/bin/python3", "/opt/benchmark/host-adapter.py", "verify_restored", "{lease}"]
  }
}
```

The schema is shared across machines. Host-specific operations remain in an installed adapter. Reuse that adapter each week; do not generate another campaign controller. The public controller owns locks, process identity, deadlines, monitoring, evidence, interruption handling, and the restoration guard. It does not contain local VM names or CI credentials. Existing labs without `host_control` still work and retain their existing external admission requirement.

## Adapter contract

Each command receives the absolute lease directory in place of `{lease}`. It returns one JSON object with `passed: true` only when all its checks pass, and exits zero. A missing result, false result, timeout, or nonzero exit fails admission or the trial. Include measurements, limits, ownership, and reasons in the JSON. The controller saves stdout, stderr, arguments, exit status, and results.

- `snapshot` is read-only. Save the original CI, VM, process, and resource state inside the lease directory. Fail if a foreign owner is active. Check the generator calibration and measurement boundary required by the selected lab. A successful snapshot alone is not a performance comparison.
- `acquire` reserves only authorized resources. Honor the saved snapshot and the declared policy. Drain active CI jobs before any idle-runner or VM pause. Do not cancel a job, change scheduling policy, or pause another owner. Save each mutation before applying it. Be safe if the controller stops halfway through this operation.
- `sample` checks the declared noise, memory pressure, paging, ownership, and resource limits throughout the trial. Keep the original limits. Include enough raw data to replay each decision. A failed sample stops the owned driver and fails the trial.
- `restore` is idempotent, including after partial acquisition. Restore only this lease's changes to the saved state. Clean up the owned benchmark's guest unit or remote process too; termination of a local SSH client is not proof of remote cleanup.
- `verify_restored` independently checks the restored CI/VM state and absence of owned benchmark processes. Fail if restoration is not verified. Never resume an unrelated service that was stopped before acquisition.

Commands have a timeout of half the restoration budget. Reserve enough time for the two restoration commands and owned-process termination. The full reservation cannot exceed 1,200 seconds. The independent guard inherits the admission locks. It restores when the controller exits or its work deadline expires. It checks process start identity before it signals the owned process group. A controller or host power failure can still need external recovery; no local process can restore a powered-off host.

Each trial includes `host/lease.json`, resource admission, command receipts, guard output, and its original driver measurement. A failed host check is a measurement quality failure. A failed restore sets `cleanup_ok` to false and stops further trials. Hashes bind this evidence to the existing result and archive. Failed attempts remain visible. No chat messages, automatic retries, new paid hosts, or CI policy changes are added.

## Limits and verification

Command E2E checks cover successful admission, bounded resume, changed inputs, failed sampling, failed restoration, and controller death. A fixture adapter is not proof of live CI restoration. Verify each installed host adapter against its real machine before a timed comparison. `lab doctor` cannot establish physical radio performance, whole-core memory boundaries, or maximum traffic-generator capacity.
