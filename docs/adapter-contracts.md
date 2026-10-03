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
