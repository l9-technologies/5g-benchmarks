# Repeatable studies

A study file selects cases, device counts, offered traffic, measurements, and confirmation sets. A lab file selects installed artifacts and adapters. Keep machine paths in the lab file, outside the repository. Keep secrets out of both files. Use Linux for live trials; the report and archive commands also work on macOS.

For an existing pinned plan, the lab file is:

```json
{"prepared_plan": "/opt/benchmark/prepared/plan.json"}
```

For the public native lab, use installed release binaries and a cached MongoDB image:

```json
{"native": {"ueransim": "/opt/UERANSIM", "open5gs_bin": "/usr/bin", "free5gc": "/opt/free5gc"}}
```

The native lab can also use its existing external adapter and adapter configuration. See [the adapter contract](adapter-contracts.md). Preparation checks tools, binaries, runtime libraries, required-feature references, and input hashes. It does not build or download a core, allocate a cloud machine, or pause CI.

Check every candidate before a long collection:

```sh
sudo python3 benchmark.py study check profiles/mini-weekly.json --lab /opt/benchmark/lab.json --output /opt/benchmark/results/check-001
```

This uses a fresh five-second trial for each candidate and its repeat at the largest device count in each case. It keeps all feature and cleanup requirements. These trials are verification only and remain separate from the main collection. A failed check returns exit 2. Selected measurements must also be present and have verified boundaries.

Start a study:

```sh
sudo python3 benchmark.py study run profiles/mini-weekly.json --lab /opt/benchmark/lab.json --output /opt/benchmark/results/weekly-001
```

Prepare without starting traffic with `study prepare`, using the same arguments. Start a prepared study, or continue a stopped study, with:

```sh
sudo python3 benchmark.py study resume /opt/benchmark/results/weekly-001
```

Use `--max-trials 5` with `run` or `resume` to stop at a trial boundary. This permits a host controller to use bounded idle windows. The controller must reserve the host, allow time for cleanup, and restore CI. The runner does not terminate another task or change its CPU allocation. Do not run timed measurements beside active CI. A host reboot changes the host identity and requires a new cohort.

```sh
python3 benchmark.py study status /opt/benchmark/results/weekly-001
python3 benchmark.py study report /opt/benchmark/results/weekly-001
python3 benchmark.py study archive /opt/benchmark/results/weekly-001 --output /opt/benchmark/weekly-001.tgz
python3 benchmark.py study verify-archive /opt/benchmark/weekly-001.tgz
```

An unbounded `run` or `resume` command writes the report when collection finishes. With `--max-trials`, report generation stays separate so it can run after the host reservation ends. Reports go into new directories. They contain every expected trial, every recorded value, missing values, failures, coverage, and pairwise comparisons. Each study saves a standalone `report-tool.py`. It can reconstruct a report without the source checkout or core binaries:

```sh
python3 /opt/benchmark/results/weekly-001/report-tool.py study report /opt/benchmark/results/weekly-001
```

Resume verifies saved plans, results, evidence, report tools, and host identity. If a crash left a complete result before its manifest checkpoint, resume verifies it and saves a recovery receipt. It does not rerun that trial. It skips all committed trials, including failures. It refuses to continue after an unsuccessful cleanup or an uncommitted trial directory. Inspect those failures before a new run; do not delete them to obtain a pass. Archive creation verifies the full archive before it receives its final name. Keep the archive outside the study directory.

## Scope and quality

The example profile is a small software comparison, not a maximum-capacity test. It uses 1 and 10 devices for fixed load and a 50-device connection batch. Ten repetitions are a minimum. Preparation rounds up to complete candidate-order rotations. Each identity has one repeated baseline. Confirmation cases use new trials with the same declared settings and decision rules.

`metrics` selects comparison questions. It does not discard other recorded metrics or their errors. The optional `cores` list selects prepared adapters. The optional `required_features` list must select requirements from the pinned registry before collection. Without that field, the full prepared requirements remain mandatory. Missing required features block comparisons. A short run or fixture cannot support a performance claim.

A confirmation match is a statistical screen, not independent verification or standards certification. Review raw evidence and all failed trials before publication. Do not select only favorable comparisons. Intervals apply to individual comparisons; they do not establish an overall ranking.

Process PSS includes the selected database only when the driver accounts for it. It excludes kernel and eBPF memory. Whole-guest memory includes the generator when it shares that guest. Use a separate generator guest and an admitted driver to test core-only guest memory. Keep that topology separate from the existing single-guest measurements.

The supported case overrides are `duration_seconds`, `warmup_seconds`, `packet_bytes`, `offered_mbps_per_ue`, `traffic_modes`, `probe_interval_seconds`, and `resource_sample_seconds`, and `load_tolerance_percent`. An adapter must implement the selected protocol. Unknown keys are errors. The study file, compiled plans, lab file, and tool hashes are saved with the results.

## Mini lab settings

Prepare the Linux VM and core artifacts once. Keep this lab inventory outside the public repository. A six-vCPU Mini layout can use:

```json
{
  "native": {
    "ueransim": "/opt/UERANSIM",
    "open5gs_bin": "/opt/open5gs/bin",
    "free5gc": "/opt/free5gc",
    "ella_bin": "/opt/bin/ella-core",
    "qcore_bin": "/opt/bin/qcore-interop-gigabit",
    "layout": "split",
    "iperf_bin": "/opt/benchmark/iperf/install/bin/iperf3",
    "iperf_build_receipt": "/opt/benchmark/iperf/build-receipt.json",
    "cipher_algorithm": 0,
    "slice_sd": "000000",
    "provisioned_sqn": 32,
    "core_cpus": [0, 1],
    "gnb_cpus": [2],
    "generator_cpus": [3, 4],
    "observer_cpus": [5]
  }
}
```

Add the external adapter fields for a private core. Use the same profile and service settings for all selected cores. For each later campaign, change the study file and result directory, then run the same commands. Do not edit Python for each test matrix. Read [the adapter contracts](adapter-contracts.md) for version and patch limits.

The Mini host still needs a reservation controller when it shares CPU and memory with CI. The bounded trial command is its integration point. Host-specific SSH, VM inventory, and CI service names are infrastructure settings; they do not belong in test definitions. A host controller must retain quality failures and restoration receipts. The study runner alone does not prove a quiet host or complete kernel-memory accounting.

The default profile requires receiver delivery within 5% of the declared fixed load. This check applies to every adapter in the shared executor. Raw values remain in failed outcomes. This is separate from the statistical equivalence margin. The connection case sends readiness probes without a full throughput phase.

A metric with an unverified boundary keeps its observed value and an explicit `metric_note`. That metric cannot pass a comparison or a health check. Other observations remain available. An unverified observation is not a competitive win.
