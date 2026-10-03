# Getting started

Use Python 3.11 or later. The runner uses the standard library. The MIT license covers the runner and public test inputs. Other programs keep their own licenses.

Read [the method](methodology.md) before measurement. Read [adapter contracts](adapter-contracts.md) before adding a core or radio. The native driver cannot yet verify the complete required feature scope. Missing checks fail trials and block performance claims.

## Local verification

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
```

These tests use fixtures. They do not measure a core or radio. They check evidence retention, failed trials, missing features, candidate order, symmetric decisions, shard coverage, changed input rejection, and report reconstruction.

## Prepare the public lab

Live runs require Linux root, SCTP, TUN, network namespaces, Podman, iproute2, iptables, iperf3, ping, tcpdump, and tshark. Use reserved hosts. Do not use a production host. Build UERANSIM and Open5GS, and pull the database image before the campaign. Record source commits, compiler flags, versions, image digests, hardware, kernel, CPU affinity, and governor in a provenance receipt. The plan pins executable, configuration, library, database image, and requirement hashes.

```sh
sudo podman pull docker.io/library/mongo:8.0.12
sudo python3 native_lab.py prepare --ueransim /srv/UERANSIM \
  --open5gs-bin /srv/open5gs/install/bin --output /srv/plan \
  --ue-counts 1 10 50 100 200 --seconds 30 --warmup 5 --repetitions 6
sudo python3 benchmark.py run /srv/plan/plan.json --output /srv/run
python3 benchmark.py report /srv/run
```

All output directories must be new. Do not overwrite prior runs. Add `--free5gc /srv/free5gc` for free5GC. This path needs `bin/<nf>`, `source/config/`, and `source/cert/`. Install and load the matching gtp5g kernel module. Upstream SBI authorization settings are retained. Authorization, rate enforcement, and persistence equivalence need separate verification. The public subscriber key and OPC are fixed test values. Never use customer credentials in these inputs.

## Full campaign and parallel execution

```sh
sudo python3 campaign.py prepare /srv/plan/plan.json --output /srv/campaign --soak-seconds 3600
sudo python3 campaign.py run /srv/campaign/campaign.json --lane matrix --worker-index 0 --worker-count 3
sudo python3 campaign.py run /srv/campaign/campaign.json --lane sweeps --worker-index 0 --worker-count 3
sudo python3 campaign.py run /srv/campaign/campaign.json --lane capacity --worker-index 0 --worker-count 3
sudo python3 campaign.py run /srv/campaign/campaign.json --lane stability --worker-index 0 --worker-count 3
sudo python3 campaign.py run /srv/campaign/campaign.json --lane experiments
```

Use indices 1 and 2 on two other equal reserved hosts for the first four lanes. Run experiments on a fourth host. Copy pinned inputs to the same absolute paths. Whole repetition groups remain on one host. One host runs one active trial. Cache builds once. Each source or method change requires a new cohort. Derive expected coverage from the plans, not a past total.

The matrix tests all declared UE counts and candidates. Sweeps use one UE at 25, 100, and 500 Mbit/s with 1200-byte packets, and 64, 256, and 1400-byte packets at 25 Mbit/s. Capacity tests endpoint counts at 1 Mbit/s per UE. Add a separate low-rate plan for intermediate counts. Keep its workload separate.

Merge completed shards:

```sh
python3 benchmark.py merge /srv/shard-0 /srv/shard-1 /srv/shard-2 --output /srv/complete
```

Merge requires equal host classes, complete paired coverage, equal plans, and valid evidence hashes. Failed trials remain in the output. A partial shard cannot supply a competitive claim.

## Repository layout

- `benchmark.py`, `native_lab.py`, `campaign.py`, and `experiments.py`: public commands.
- `runner/`: common contracts, execution, evidence validation, statistics, reporting, and saved report tools.
- `adapters/`: Open5GS, free5GC, UERANSIM, and shared public lab helpers.
- `tests/`: fixture command checks. These do not produce performance measurements.
- `configs/`: public test inputs.
- `requirements.json`: required feature scope and 3GPP references.
- `docs/`: setup, method, and adapter contracts.
- `scripts/`: repository verification.

A run saves `report-tool.py` as one executable Python archive with its module dependencies. Rebuild its report with `python3 report-tool.py report <run-directory>`. Earlier runs retain their original saved Python script. Neither form requires the source repository. Preserve all saved tools, plans, raw evidence, and results. Each output directory must be new.
