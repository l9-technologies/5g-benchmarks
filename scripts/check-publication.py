#!/usr/bin/env python3
"""Check the complete public file set before a push. No network access."""
from pathlib import Path
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
ALLOWED = {
    '.github/workflows/ci-host-check.yml',
    '.github/workflows/verification.yml',
    '.gitignore',
    '.gitleaks.toml',
    'AGENTS.md',
    'CLAUDE.md',
    'LICENSE',
    'adapters/__init__.py',
    'adapters/common.py',
    'adapters/free5gc.py',
    'adapters/open5gs.py',
    'adapters/open5gs_config.py',
    'adapters/ueransim.py',
    'benchmark.py',
    'bundle.json',
    'bundle.py',
    'campaign.py',
    'configs/gnb.yaml',
    'configs/profile.toml',
    'configs/ue.yaml',
    'docs/adapter-contracts.md',
    'docs/getting-started.md',
    'docs/methodology.md',
    'experiments.py',
    'native_lab.py',
    'open5gs_config.py',
    'package.json',
    'requirements.json',
    'runner/__init__.py',
    'runner/cli.py',
    'runner/contracts.py',
    'runner/evidence.py',
    'runner/execution.py',
    'runner/reporting.py',
    'runner/snapshot.py',
    'runner/statistics.py',
    'scripts/check-agent-instructions.mjs',
    'scripts/check-publication.py',
    'tests/__init__.py',
    'tests/test_benchmark.py',
    'tests/test_full_suite.py',
    'tests/test_layout.py',
    'tests/test_neutrality.py',
}
TEST_KEYS={'00112233445566778899aabbccddeeff','000102030405060708090a0b0c0d0e0f','10112233445566778899aabbccddeeff'}
PATTERNS=[
    r'-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----',
    r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b',
    r'\bgh[pousr]_[A-Za-z0-9]{20,}\b',
    r'\bgithub_pat_[A-Za-z0-9_]{20,}\b',
    r'\bxox[baprs]-[A-Za-z0-9-]{10,}\b',
    r'(?i)(?:password|secret|access_token)\s*[=:]\s*["\'][^"\']{8,}["\']',
    r'/Users/[A-Za-z0-9_-]+/',
    r'FIVEGC_|NGRAN_|fivegc-[a-z]|ngran-[a-z]|5g-core-vercel|/srv/L9/',
]

def main():
    files=subprocess.check_output(['git','ls-files','-z','--cached','--others','--exclude-standard'],cwd=ROOT).decode().split('\0')
    problems=[]
    for name in sorted(set(files)-{''}):
        path=ROOT/name
        if name not in ALLOWED:problems.append(f'file is outside public allowlist: {name}');continue
        if path.is_symlink() or not path.is_file():problems.append(f'not a regular file: {name}');continue
        data=path.read_text()
        if name!='scripts/check-publication.py':
            if any(re.search(pattern,data) for pattern in PATTERNS):problems.append(f'credential or private integration marker: {name}')
            keys=set(re.findall(r'(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])',data))
            if keys-TEST_KEYS:problems.append(f'unapproved 128-bit value: {name}')
    if problems:
        print('\n'.join(problems),file=sys.stderr);return 1
    print(f'Public allowlist and credential checks passed: {len(set(files)-{chr(0),""})} files.');return 0

if __name__=='__main__':sys.exit(main())
