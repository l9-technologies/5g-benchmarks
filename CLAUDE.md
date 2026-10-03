# Benchmark runner rules

## Required 3GPP features

- A missing 3GPP feature is a core defect. It is not an efficiency improvement.
- If another core provides a 3GPP feature in the declared test scope, require that feature from every core in that comparison. Record the 3GPP release, specification, clause, and test that establish the requirement.
- If a core does not provide a required feature, fail its test. If the feature is not verified, record a failed requirement check. Keep the raw evidence and the reason.
- Do not disable a feature, reduce the requirement, remove a test, change the pass limit, or omit a failure to benefit the in-house core.
- Block efficiency and performance claims when any required feature check fails or is missing. Resource values from that configuration can remain as diagnostic data, with the failure visible.
- Correct the core defect, then run the same test again with a new source version and a new result directory. Preserve all previous results. Do not combine results from different requirements or source versions.
- Declare optional features and test scope before a run. Use the same scope for every core. Do not change the scope after results are known. An optional feature within a comparison's declared scope is required from every compared core.
- These rules apply to the test plans, adapters, feature checks, statistics, reports, exported runner, and repeat-run skill.

## Neutral comparison

- Give every core and radio candidate the same workload, required behavior, starting state, resource accounting, failure rules, and statistical rules.
- Repeat each candidate's baseline. Keep every failed or missing result visible.
- Record all planned metrics and load levels. Do not select report content from the measured winner.
- A simulation does not verify physical radio. Missing lab equipment must remain a test coverage limit; it does not prove that the core supports the required feature.

## Public repository

- Publish only the runner, public test inputs, fixture tests, and method. Keep private adapters, implementation code, binaries, credentials, customer data, captures, host receipts, and live results outside the repository.
- Review the complete staged file list and run the publication check before a public push. Known test subscriber keys are public fixtures; never replace them with real keys.
- Use ASD-STE100 Simplified Technical English. Keep documentation in `docs/`. Do not create or edit `README.md`.
- Read existing code and contracts before a change. Reuse common rules. Preserve unrelated work and prior results.
- Keep `CLAUDE.md` identical to `AGENTS.md`. Run `npm run check:agent-instructions` after an instruction-file change.
- Run the fixture command checks after a runner change. State separately whether live measurements and independent reruns were performed.
