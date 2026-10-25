# Mini buyer tests

The profile `profiles/mini-buyer.json` reuses each traffic run to measure delay,
receiver delivery, process CPU, and process memory. Device-count runs also
measure registration, session success, and the first external reply. This keeps
the workloads matched and reduces duplicate collection.

Set the lab's native `minimum_subscribers` to 50 for this profile. Keep that
provisioned population fixed while the active device count changes. Report
provisioned subscribers and active devices separately.

This profile needs live installation checks and a measurement pilot before a
full collection. It is not evidence that these checks passed on a given host.
The separate service-recovery experiment also needs a live pilot for each core.

## Process resource boundary

Set `resource_accounting` to `selected-core-processes` in profile defaults or a
case to request a process comparison. The native driver records per-process
PSS and CPU counters in every resource sample. Its proof checks their sums,
the database PID, the subscriber population, and the NAS profile. The report
requires the same fresh-state policy and explicit process scope on both sides.

PSS includes the selected database process. Kernel and eBPF allocations remain
outside this boundary. Process CPU omits kernel threads and interrupt work.
Label charts as **core and database process memory** or **core and database
process CPU**. These values do not establish complete core resource use.

Each trial starts with fresh subscriber and session state. The accounting
policy makes no restart or session-persistence claim. Keep the tested source,
configuration, required features, authentication, and storage differences with
the results. A missing required feature still blocks a comparison. The default
resource gates remain active when the explicit process scope is absent.

Complete core memory needs a separate generator and admitted kernel/eBPF
accounting. Do not add process PSS to overlapping kernel or cgroup totals.

## Useful TCP and UDP recovery

Use a prepared plan with `user_plane` set to `ueransim`. It must pin this source
export and all installed artifacts:

```sh
sudo python3 experiments.py /opt/benchmark/plan.json \
  --core open5gs --case service-control --ue-count 10 --seconds 120 \
  --output /opt/benchmark/results/control-001
sudo python3 experiments.py /opt/benchmark/plan.json \
  --core open5gs --case service-recovery --nf upf --ue-count 10 --seconds 120 \
  --output /opt/benchmark/results/recovery-001
```

Run one declared no-fault control and three fresh fault trials per core. Keep
the control and fault configuration, resource limits, stream counts and
observation duration equal. These commands do not reserve a shared host; use
the installed host controller for each run.

Every device has one TCP and one UDP stream. Each request has a 1,200-byte
payload and a sequence number. The target interval is 20 ms. Receivers echo
the payload; clients check the complete reply. Timeouts and changed payloads
remain failed requests. TCP clients can reconnect. UE and gNB processes must
keep their original identities and stay alive.

Socket requests have a 100 ms timeout, so request gaps can grow during an
outage. Use the recorded request and reply times; do not claim fixed 20 ms
measurement accuracy. Requests sent before the failed process is confirmed
dead do not count toward the recovery boundary.

The fault kills the selected UPF process. Ella Core and QCore implement it in
one core process; their fault therefore restarts the complete monolithic
process. The receipt states this difference. It is not an isolated component
comparison across architectures.

The recovery boundary is the first of ten consecutive verified replies after
the last failed request. Requests must start after confirmed process death.
The last ten replies must also pass. The
receipt includes the confirmation time, the gap from the last pre-fault reply,
restored stream and device counts, failed requests, and radio identities.
This does not prove PDU context persistence or new-session recovery.
The probe interval limits time resolution. This is a service interruption
measurement, not a maximum-throughput or packet-delay benchmark.

A no-fault control requires every request in its observation window to pass.
Baseline failures remain in the raw records. A fault trial can
fail to recover; keep that outcome and its logs. A timeout does not establish
that the core lacks the required protocol feature.

Check the probe without a core:

```sh
python3 tests/check_service_probe_e2e.py
```

This loopback check verifies real TCP/UDP delivery, a server outage, recovery,
wrong-payload rejection, raw counters, and child cleanup. It does not replace
the live core pilot or independent result review.

For short process CPU measurements, set `cpu_clock` to `process-nanoseconds`.
This reads the Linux process CPU clock and avoids scheduler-tick rounding.
Nanoseconds are counter units, not a claim of measurement accuracy. Run
`python3 tests/check_process_clock_e2e.py` on Linux before collection.

## Small retained batches

Use `python3 benchmark.py run plan.json --output pilot-01 --pilot` to select
one full repetition at every declared device count. With five cores, a repeat
for each core, and three device counts, this selects 30 trials. A pilot is a
coverage and measurement check; it cannot clear comparison claims. Do not
select the first 30 trials from a full schedule: those can all use one device.

Choose the trial IDs from the frozen plan schedule before collection. Run them
in schedule order. Keep each batch in its own output directory:

```sh
python3 benchmark.py run plan.json --output batch-01 --trial-ids ID1 ID2 ID3 ID4 ID5
python3 benchmark.py run plan.json --output batch-01 --resume
python3 benchmark.py merge batch-01 batch-02 --output complete
```

Use the actual saved IDs in place of ID1 through ID5. Resume keeps completed
results and verifies their evidence. It does not run them again. A changed
selection is rejected. Merge requires complete plan coverage and rejects
duplicate IDs. Partial batches cannot clear comparison claims.

The host controller must retain each failed host window and its replacement
in the attempt ledger. Retry only that window with the same selected IDs.
A core failure in a clean host window remains a failed result; do not retry it
to obtain a passing comparison. Keep the host and statistical limits fixed.

## Mini host admission

Use one shared host reservation for CI, builds, package installs, and timed
measurements. A lock protects only commands that use it. Do not treat it as
an operating-system CPU partition. Check active local install and build
commands before acquisition, then check host activity throughout measurement.

For the Mini repair controller, use one trial per reservation and require
18 consecutive five-second quiet samples before that trial. Keep the existing
host CPU, swap, memory, and guest quality limits. A host rejection retains the
attempt and permits at most three attempts of that trial. A core failure in a
clean reservation stays failed. Admission waits do not count as trial attempts.
Restore CI on every exit and at the existing bounded window deadline.

Run the pilot through the same archive, raw-data review, and report path as
collection. Prove the full candidate/device-count matrix before admitting
main collection. A command check with fixture data is not live host admission.
