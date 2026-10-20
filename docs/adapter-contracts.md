# Adapter contracts

Read [the method](methodology.md) for required behavior and measurement boundaries. Follow [the repository rules](../AGENTS.md). Use [getting started](getting-started.md) for setup and commands.

## Other cores and radios

Use an external Python module with `--extension /srv/adapter.py --extension-config /srv/adapter.json`. Keep private adapters, binaries, configurations, and credentials outside this repository.

The module has four functions:

- `prepare(config)` returns `paths` (all files to pin), `identities` (exact core and radio identities), `cores`, `radios`, and `config`.
- `start_core(lab, core, count)` provisions the same subscriber population and starts the core with `lab.start`.
- `start_radio(lab, kind, count, core)` returns elapsed milliseconds and the UE TUN interface names.
- `binaries(config, core, radio)` returns the core binary list and a map from UE/gNB role to radio binary path.

Each adapter must meet the common evidence and feature contracts. An adapter cannot waive a required test. Use the source as the interface reference. Run the same public method against each adapter.

## Public adapters and the shared lab

`adapters/open5gs.py` and `adapters/free5gc.py` implement `start(lab, count)`. `adapters/ueransim.py` implements `start(lab, count, core)` and returns elapsed milliseconds and UE interface names. `adapters/open5gs_config.py` creates the Open5GS configuration. `adapters/common.py` contains the public test identities and process helpers.

Adapters call `lab.start`, `lab.ns`, `lab.wait`, and the database helpers. The shared `Lab` in `native_lab.py` owns process tracking, network namespaces, captures, monitoring, UE routes, and cleanup. Core and radio adapters must not create separate measurement or cleanup rules. The existing `Lab.start_open5gs`, `Lab.start_free5gc`, and configuration command remain available.

## Evidence and source identity

All adapters use the metric and feature definitions in `runner/contracts.py`. Evidence validation is in `runner/evidence.py`. Statistics are in `runner/statistics.py`; report decisions and output are in `runner/reporting.py`. `benchmark.py` retains the original Python imports for external adapters and report tools.

The prepared plan pins runner and public adapter modules as well as binaries and configurations. External `prepare` must list every private dependency in `paths`. A changed source, input, requirement, or measurement boundary requires a new plan and cohort. A failed or missing required feature remains a failed check. Private integration cannot waive the requirement or replace failed evidence.

## Ella Core and QCore

The public adapters `adapters/ella.py` and `adapters/qcore.py` use the same process, namespace, capture, traffic, and cleanup code. Select their installed binaries with `--ella-bin` and `--qcore-bin`. Both need `--layout split`. Use `--cores` to select adapters; preparation checks only the selected core binaries. The shared UERANSIM binaries remain mandatory.

Ella Core uses its API to create an isolated operator and subscribers. The adapter targets the 1.19 API. It creates a random local API credential for each disposable database and keeps it out of the report.

The QCore adapter targets the disclosed interop build from upstream commit `c38e5b11d624a5102425530c3dbaee5098e6bb22`. It requires `--cipher-algorithm 0 --slice-sd 000000 --provisioned-sqn 32`. The binary needs the response-correlation and 1,000-Mbit/s AMBR changes used by that build, plus the ARM ABI fix on ARM. This adapter does not add encrypted NAS support. Label patched QCore results as patched results. Do not present them as results for an unmodified upstream release.

`adapters/common.py` defines the shared service profile: PLMN 00101, DNN internet, NIA2, selected NEA0 or NEA2, selected slice SD, and 1,000-Mbit/s subscription AMBR. The optional fixed SQN replaces per-identity SQNs. The shared launcher makes the subscriber population explicit. Studies default to one provisioned subscriber per requested device. The standalone native lab retains a minimum of two identities for existing recovery tests; `--minimum-subscribers` selects this minimum. The setting applies to external core adapters as well. Each trial records the requested and provisioned counts. Configuration is not proof of signalled AMBR or enforcement. Keep those checks separate.

The split layout puts the core and radio in separate namespaces with an N3 veth pair. N2 and N3 use 10.3.0.2 on the core and 10.3.0.3 on the radio. N6 uses the existing isolated receiver namespace. Core, gNB, UE/traffic, and observer CPU lists can be set with `--core-cpus`, `--gnb-cpus`, `--generator-cpus`, and `--observer-cpus`. If one list is set, all lists must be set. They must be disjoint and available. Use physical CPUs rather than sibling hardware threads for controlled comparisons.

An external adapter can provide optional `configure_start(lab, name, argv, env, role)`. It returns `(argv, env)` before the shared process launcher applies CPU affinity. Use this hook for implementation-specific addresses and runtime libraries. Keep private environment variable names and source outside the public repository. The hook does not replace process tracking, measurement, or cleanup.

free5GC configuration needs PyYAML in the Linux environment. Preparation pins its installed parser files. The shared runner and report commands use the Python standard library.

`lab.start(..., required=True)` marks a process that must remain alive during readiness checks. Core, UE, and gNB roles have this requirement by default. Short traffic clients can exit normally. This separates process lifetime from CPU-accounting roles. The shared launcher still owns all started processes and cleans them up.

`runtime_environment` can point to an external JSON file with `cores` and `processes` maps. Each named entry contains environment variable names and string values. Explicit inventory values replace adapter defaults; the file is pinned. Use it for shared-library paths and implementation-specific binds. Keep this file outside the public repository. An external adapter cannot replace shared lab settings through its returned configuration.

Process and namespace custody is recorded before cancellation can leave a newly created resource untracked. Native signal handlers use `lab.interrupt(signum)`; resource creation defers the exception until custody is saved. Cleanup retains errors and refuses to claim success when ownership is uncertain.
