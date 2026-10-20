#!/usr/bin/env python3
"""Export the runner and public lab inputs without the product repository."""
import argparse
from pathlib import Path
import shutil
from benchmark import digest, write

ROOT = Path(__file__).resolve().parent


def export(output):
    output.mkdir(parents=True, exist_ok=False)
    names = (
        'requirements-lab.txt',
        'benchmark.py',
        'native_lab.py',
        'bundle.py',
        'experiments.py',
        'campaign.py',
        'requirements.json',
        'AGENTS.md',
        'CLAUDE.md',
        'LICENSE',
        'open5gs_config.py',
        'configs/profile.toml',
        'configs/ue.yaml',
        'configs/gnb.yaml',
        'docs/getting-started.md',
        'docs/methodology.md',
        'docs/adapter-contracts.md',
        '.gitleaks.toml',
        '.gitignore',
        'package.json',
        'scripts/check-agent-instructions.mjs',
        'scripts/check-publication.py',
        'adapters/__init__.py',
        'adapters/common.py',
    'adapters/ella.py',
    'adapters/qcore.py',
    'adapters/kernel_gtp.py',
    'adapters/flow_hash.c',
    'adapters/check_flow_hash.py',
    'adapters/steering.py',
    'adapters/probe_window.py',
    'patches/iperf-3.16-phase-clock.patch',
    'scripts/build-iperf.py',
    'scripts/verify-live-study.py',
    'tests/test_probe_window.py',
    'patches/qcore-compatibility.diff',
    'patches/qcore-response-correlation.patch',
    'patches/qcore-gigabit-ambr.patch',
    'patches/qcore-arm-tun.patch',
    'patches/linux-gtp-session-container.patch',
    'docs/generator.md',
    'docs/third-party.md',
    'adapters/session_ambr.py',
    'tests/test_gtp_endpoint.py',
    'tests/test_session_ambr.py',
    'tests/test_native_profile.py',
        'adapters/free5gc.py',
        'adapters/open5gs.py',
        'adapters/open5gs_config.py',
        'adapters/ueransim.py',
        'runner/__init__.py',
        'runner/cli.py',
        'runner/workflow.py',
        'runner/contracts.py',
        'runner/evidence.py',
        'runner/execution.py',
        'runner/reporting.py',
        'runner/snapshot.py',
        'runner/statistics.py',
    'runner/studies.py',
    'runner/archives.py',
    'tests/test_studies.py',
    'profiles/mini-weekly.json',
    'docs/studies.md',
        'tests/__init__.py',
        'tests/test_benchmark.py',
        'tests/test_full_suite.py',
        'tests/test_layout.py',
        'tests/test_neutrality.py',
    )
    mappings = {name: ROOT / name for name in names}
    for name, source in mappings.items():
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    write(output / 'bundle.json', {'schema_version': 1,
        'files': {name: digest(output / name) for name in sorted(mappings)}})
    print(output)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('export').add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    export(args.output)
