# Third-party source and patches

The runner's license does not replace upstream licenses. Build and distribute each core under its own license.

- The QCore patches are changes to [nplrkn/qcore](https://github.com/nplrkn/qcore) at commit `c38e5b11d624a5102425530c3dbaee5098e6bb22`. QCore is Copyright 2025 Nic Larkin and is licensed under AGPL-3.0-or-later. Preserve that license for the patched source. Apply `qcore-compatibility.diff`, `qcore-response-correlation.patch`, then `qcore-gigabit-ambr.patch`. These change one-gNB response correlation and AMBR to the shared 1,000-Mbit/s profile. They do not add full feature parity.
- On ARM, `qcore-arm-tun.patch` changes the `async-tun` 0.10.1 interface-name buffer to the existing platform `c_char` type. Apply it to that dependency's `src/linux/request.rs`. Use a local Cargo patch for the vendored dependency; keep all other locked packages unchanged. Preserve the dependency's upstream license and notice.
- The Linux GTP patch changes [Linux v6.8](https://github.com/torvalds/linux/tree/v6.8). It remains under the Linux source license, GPL-2.0-only. The receive classifier has an SPDX GPL-2.0 marker.

For the tested QCore recipe, use Rust 1.97.1 for the host build, `nightly-2026-10-17` with `rust-src` for the eBPF build, and bpf-linker 0.12.0. Run `cargo +1.97.1 build --release --locked -p qcore` after applying the patches and ARM dependency update. The `aya-build` request for `+nightly` must resolve to the recorded dated compiler. Record actual compilers, lockfile, patch hashes, and final binary hash; a different architecture will produce different bytes.

Public adapters launch installed artifacts. They do not download a private core, supply its source, or override its license. Core binaries, subscriber databases, captures, host inventories, and live receipts stay outside this repository.

The iperf clock patch changes iperf 3.16, Copyright The Regents of the University of California through Lawrence Berkeley National Laboratory. Preserve the upstream BSD-style license and notices. The build uses the complete upstream source archive and keeps its license. Only the small instrumentation patch is stored here.
