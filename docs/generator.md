# Linux GTP generator

The default native lab uses UERANSIM for control and traffic. The optional Linux GTP path uses UERANSIM for real registration and session setup, then reads live tunnel state from NGAP and NAS. It routes traffic through a Linux GTP endpoint. It does not assume TEIDs or UE addresses. All cores use the same selected path.

Set these native lab fields before preparation:

```json
{
  "user_plane": "linux-gtp",
  "layout": "split",
  "cipher_algorithm": 0,
  "kernel_gtp_module": "/opt/benchmark/generator/gtp.ko"
}
```

NEA0 permits the independent packet decoder to read NAS session state. This is a labelled integrity-only comparison. It is not an encrypted-profile result. The adapter checks each UE's selected NAS algorithms, distinct tunnel addresses, uplink/downlink TEIDs, QFI, and signalled 1,000-Mbit/s session AMBR. It reads the kernel context table back before traffic. After traffic, it checks the uplink PDU Session Container against the live QFI for every UE.

## Prepare the generator once

Use a disposable Linux VM with matching kernel headers. Install `pyroute2`, `clang` with the BPF target, `libbpf` headers, `gcc`, `bpftool`, `iproute2`, and `ethtool`, in addition to the normal native-lab dependencies. The tool does not install packages or replace a running host kernel.

The generator module starts from Linux v6.8's `drivers/net/gtp.c`:

- Source: <https://raw.githubusercontent.com/torvalds/linux/v6.8/drivers/net/gtp.c>
- Source SHA256: `c356d57ccd3ce2f3e29be3a1b4faaf1f337d02422723413ed740cd4b656901f8`
- Patch: [5G session container](../patches/linux-gtp-session-container.patch)
- Patched source SHA256: `f9ee35d23cb86c3e1b367bb2ccbfee27478d7e7e4a840f11798e50813d1419c7`

Apply the patch to `gtp.c` in a build directory. Put `obj-m += gtp.o` in that directory's `Makefile`. Build with the VM's kernel headers:

```sh
make -C /lib/modules/$(uname -r)/build M="$PWD" modules
```

The patch adds the uplink PDU Session Container and bounded decoding of downlink extension headers. This changes the generator, not a tested core. Keep both source hashes, the patch, kernel version, compiler version, and module hash with the campaign method. The native preflight checks the module's kernel version. Trials verify its loaded source version. They unload the module only when the trial loaded it.

When CPU roles are set, preparation compiles the shared receive classifier and runs its packet tests before collection. The test covers both directions, plain IPv4, N3, NAT TCP/UDP, invalid lengths, unsupported peers, and byte preservation. The classifier changes the packet hash; it does not change packet bytes. The compiled objects and build receipt are pinned. Supported receive allocations are 1, 2, 4, 8, and 16 CPUs. This profile uses IPv4 without options, one QoS flow per UE, and at most 200 traffic endpoints.

Each trial reads back receive CPU masks. The observer includes active traffic processes and retains their final CPU counters after normal exit. Headroom limits remain 90% of one CPU for a generator thread and 80% of its assigned CPU allocation. A headroom failure prevents attribution of a capacity limit to the core. It is not evidence that a core reached maximum capacity.

Probe interval and resource-sample interval come from the study file. Raw probes include epoch timestamps. Phase records include epoch and monotonic start anchors. Preserve them when you check probe coverage and traffic-window alignment. A denser probe setting needs a shared overhead check before a new timed campaign.

## Loaded probe timing

Build the instrumented iperf generator once, before collection:

```sh
python3 scripts/build-iperf.py --output /opt/benchmark/iperf
```

The command downloads iperf 3.16 from its upstream release archive, checks SHA256, applies the public clock patch and the upstream CPU pacing and UDP listener fixes, builds it, and tests TCP/UDP in both directions on loopback. It saves a build receipt and the four raw checks. These are functional checks, not 5G measurements. A source archive can be supplied with `--source-archive`; it must have the same pinned hash. The build command needs Linux, Python 3.12 or later, Autoconf, Automake, Libtool, and normal C build tools. Each 1-Mbit/s check must use at most 25% of one CPU for the client and server together. Raw CPU counters and elapsed time remain in the build output. This check detects a busy loop; trial headroom limits still apply.

The pacing patch is upstream commit [783095e](https://github.com/esnet/iperf/commit/783095e138b7257dfcbf4e9aa2a48685d14c7f16), which fixes CPU use when the bitrate is limited. The UDP listener patch is upstream commit [ac6b9f7](https://github.com/esnet/iperf/commit/ac6b9f7fd335ddebc5212eed40083ef4cd3cb86d), merged through PR 1708. It removes a duplicate listener from the main thread's read set after a worker takes the socket. A loopback check can miss its CPU cost across namespaces. Run the direct calibration on the declared namespace path before core collection. The receipt binds all patches and the resulting binary. New builds and campaigns use new output directories. Keep earlier binaries, receipts, and failed trials.

Set `iperf_bin` to the installed binary and `iperf_build_receipt` to its receipt in the lab inventory. Preparation checks the binary and patch hashes. Studies that select loaded RTT measurements require this contract before traffic starts.

The clock patch records two events per client: the end of warm-up and the test timer's stop boundary. Each event brackets a realtime clock read with monotonic reads. It does not change packet contents, rate limits, or traffic code. The runner starts probes after every client's warm-up and leaves two seconds of margin in the scheduled probe count.

For each phase, a separate 128-byte capture records echo requests and replies inside the UE namespace. The verifier requires zero capture drops, exact agreement with per-UE sender counters, one timer pair per client, no duplicate or unmatched packets, and no clock step above 1 ms. Every captured request and reply must be inside the shared active interval. It does not clip slow replies to improve a percentile. RTT is the captured reply timestamp minus its request timestamp, before and after the software network path. Missing or invalid proof leaves a visible metric note and blocks that comparison.

Timer events, raw probe captures, decoded fields, observed ping output, and the complete verification result remain with each trial. The saved method distinguishes these packet timestamps from application-level ping timing. Use fresh main and confirmation trials when this measurement boundary changes.
