#!/usr/bin/env python3
"""Export the runner and public lab inputs without the product repository."""
import argparse
from pathlib import Path
import shutil
from benchmark import digest, write

ROOT = Path(__file__).resolve().parent


def export(output):
    output.mkdir(parents=True, exist_ok=False)
    mappings = {name: ROOT / name for name in ('benchmark.py', 'native_lab.py', 'bundle.py', 'experiments.py', 'campaign.py', 'test_benchmark.py', 'test_full_suite.py', 'test_neutrality.py', 'requirements.json', 'AGENTS.md', 'CLAUDE.md', 'LICENSE')}
    for name in ('docs/methodology.md','open5gs_config.py','configs/profile.toml','configs/ue.yaml','configs/gnb.yaml'):
        mappings[name]=ROOT/name
    for name in ('.gitleaks.toml','.gitignore','package.json','scripts/check-agent-instructions.mjs','scripts/check-publication.py'):
        if (ROOT/name).is_file():mappings[name]=ROOT/name
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
