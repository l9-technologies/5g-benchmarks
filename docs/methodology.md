# 5G benchmark method

Use [getting started](getting-started.md) for setup and commands. Use [adapter contracts](adapter-contracts.md) for public and external integrations. This method applies equally to every candidate.

## Required features

A missing 3GPP feature is a core defect, not an efficiency gain. Read [the repository rules](../AGENTS.md). The default [requirement registry](../requirements.json) names the release, specification, clause, source, and test for each feature in scope. A failed or missing check fails the trial and blocks performance claims. An untested feature does not prove that the core lacks it. Add the missing test or fix the core. Do not reduce the requirement to obtain a pass.

The default scope includes registration, PDU sessions, paging, mobility, IPv6, multiple sessions, slice isolation, session release, and deregistration. The current native driver does not verify all these features. Its runs will fail the required feature checks until this coverage is complete. These checks are not full 3GPP conformance certification. Declare optional features and their applicability before measurement; apply the same scope to each core.

## Neutral controls

Method 2 repeats every distinct core/radio candidate. It uses at least six fresh repetitions and increases that count to a multiple of candidate count. Candidate order rotates through every position equally from the saved seed. Candidate labels do not change statistical decisions. Each pair requires both candidates' baseline checks. A live method 1 plan is rejected. Historical results keep their saved report tool.

Use the same PLMN, subscribers, DNN, slice, NAS algorithms, AMBR target, packet size, offered traffic, duration, warm-up, receiver, service contract, and fault scenario. Initialize each subscriber SQN to its index times 32. Retain authentication logs; a resynchronization event blocks authentication timing claims. Record the same verified persistence and security behavior before an efficiency claim. A configuration hash alone does not prove this behavior.

All metrics and UE counts remain in reports, including missing and failed outcomes. There is no overall rank. Paired bootstrap intervals use 4000 draws and a 95 percent interval. The equivalence margin uses the mean of the two absolute medians. Relative differences use `200*(A-B)/(abs(A)+abs(B))`, with zero for two zeros. Each interval applies to one metric. Multiple metric exploration needs separate confirmation before a selected headline claim.

The default driver records an unverified durability contract and radio capability contract. Comparisons that need those contracts are blocked. Process CPU and memory omit kernel work and memory. Process-only core efficiency and binary size cannot support a complete core resource claim. Whole-host CPU includes system work, the database, radio, and generator. It can support a whole-lab claim only with matched and verified service and durability contracts. Whole-host memory is MemTotal minus MemAvailable, not isolated core memory.

## Evidence and boundaries

Attach readiness ends when all UE TUN addresses exist. Stack readiness includes provisioning, radio setup, routes, and an external N6 reply from each UE. Binaries and images are cached. These are not cold deployment measurements. Procedure intervals use per-UE NAS log events. Receiver counters supply throughput and loss. Report probe percentiles with reply counts and loss. UDP packet size is payload length; TCP uses MSS and write length.

Resource samples run every 0.1 second. A sampled peak is not an exact continuous peak. RSS and PSS are separate readings. CPU efficiency uses the traffic window after warm-up. Reject host busy time above the declared limit, steal above 0.5 percent, or a generator thread above 90 percent of one CPU for one second. A host or generator limit prevents attribution to the core.

Keep N2/N3 captures, UE logs, routes, resource samples, receiver logs, phase windows, result errors, and cleanup. Changed evidence prevents report reconstruction. A feature failure retains both the original driver measurement and the failed requirement result. Hashes detect changes; they do not independently prove that a live measurement occurred. Reports remain `publishable: false` pending independent review and rerun.

## Faults, capacity, and stability

Recovery kills and restarts only the named AMF, SMF, or UPF. Keep UE and gNB process identities. Measure existing flows and new sessions in separate labs. Run the no-fault new-session control first. A manual radio restart changes the scenario. A simulator N2 association failure is an attribution limit. Churn uses real session release and establishment, TUN removal, new traffic, and final deregistration. Negative tests need an explicit protocol rejection; timeout is unconfirmed. Stability states actual duration, counters, loss, and memory change. One hour does not prove a longer duration or absence of a memory leak.

Capacity requires all UEs attached, at least 95 percent of offered UDP traffic in each direction, and no more than one percent UDP and probe loss. A largest passing point is a lower bound, not a maximum.

Software simulators do not prove physical radio performance. A physical test needs named radio, gNB, real UE, firmware, band, bandwidth, power, channel conditions, and protocol and traffic evidence. Record missing physical hardware as untested.

## Public data

This repository contains no measured result set, private implementation, customer data, host receipts, or credentials. Review and remove confidential data before any separate result publication. Keep complete raw evidence privately, including failure data and its immutable hashes. Never publish raw captures or logs without this review.
