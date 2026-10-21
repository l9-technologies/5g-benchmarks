# Direct generator calibration

Before a core load or capacity test, check the sender and receiver on a direct Linux veth path. This is a tool test, not a core or radio comparison. Keep its results separate from core trials.

```sh
sudo python3 benchmark.py lab calibrate profiles/generator-calibration.json --output /opt/benchmark/results/generator-001
```

Use a service allocation that includes every declared generator and observer CPU. The command checks its effective CPU affinity before it creates output or sends traffic. A service confined to one observer CPU cannot start clients on other CPUs. Do not change another owner's CPU limits to bypass this check.

The example uses ten simultaneous streams, both UDP directions, 64-byte and 1200-byte application payloads, and six aggregate rates from 10 through 1000 Mbit/s. Each direction has five seconds of warm-up and thirty seconds of measurement. The command disables checksum and segmentation/coalescing offloads on its owned veth interfaces and saves readbacks. With `receive_steering` enabled (the default), it builds and checks the existing receive classifier once, applies the same RPS and classifier method as the native core lab, and saves readbacks. This requires the native lab's Clang/BPF build tools. Set `receive_steering: false` only for a declared diagnostic of the unsteered path. Keep those results separate. It reuses the native lab's namespace, process, monitoring, traffic, and cleanup code. It does not start a core, radio, or database.

For each point, save sender and receiver goodput, receiver packets/s, packet counts, loss, CPU samples, socket counters, and cleanup. Require both sender and receiver rates within 5% of the offer, loss at or below the profile limit, no new UDP socket errors, and the existing thread and CPU headroom limits. Tool errors and failed cleanup stop collection. A point that exceeds a delivery or headroom limit remains visible while later declared points run. An unsuccessful point is not a core limit.

`tool_supported_core_rates_mbps` divides passing direct rates by the declared headroom multiple. It is only a screening ceiling for later offered loads. It does not prove that the generator has enough headroom when it also runs the selected radio or GTP endpoint. Check those resources during every core trial. Keep core-feature checks, matched load, host quality, repeated baselines, and confirmation requirements.

The output contains a complete public source export, the frozen profile, source hashes, tool identity, raw per-stream JSON, socket and CPU evidence, point results, and `evidence.json`. Interruption preserves partial data and runs owned cleanup. Do not retry into that output. Use a new directory after a correction. A completed diagnostic run may contain failed points; completion is not admission to publish a core comparison.

The command does not pause CI or establish a quiet physical host. Use the host reservation and quality controls before comparative collection. Add `packet_patterns` with one payload size per stream to test a declared mixture. An empty `packet_bytes` list is permitted when patterns are present. Mixed patterns use equal offered bitrates per stream; they are not a standardized packet-count mix. The tool does not establish physical radio performance, whole-core memory, or sustained production capacity. Those require their declared follow-up tests.


A closed collection creates and fully reads back the adjacent `.tgz` archive and saves its receipt. This includes partial failed collections. Rebuild tables without a running core or native Linux tools:

```sh
python3 benchmark.py lab calibration-report /opt/benchmark/results/generator-001 --output /opt/benchmark/reports/generator-001
```

The report checks every saved evidence hash first. `points.csv` includes all declared directions and points, with failures and missing values. `all-metrics.csv` retains all observed diagnostic metrics and boundary notes. The summary never admits a core comparison. Do not write reports inside the raw directory.
