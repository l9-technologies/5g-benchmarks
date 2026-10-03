#!/usr/bin/env python3
"""Check the complete public file set before a push. No network access."""
from pathlib import Path
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
ALLOWED={
    'AGENTS.md','CLAUDE.md','LICENSE','.gitleaks.toml','.gitignore','package.json','bundle.json',
    'benchmark.py','native_lab.py','bundle.py','campaign.py','experiments.py',
    'open5gs_config.py','requirements.json','test_benchmark.py','test_full_suite.py',
    'test_neutrality.py','configs/profile.toml','configs/ue.yaml','configs/gnb.yaml',
    'docs/methodology.md','scripts/check-agent-instructions.mjs',
    'scripts/check-publication.py','.github/workflows/verification.yml',
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
