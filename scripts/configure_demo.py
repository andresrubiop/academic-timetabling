#!/usr/bin/env python3
"""Create a private local .env without overwriting an existing configuration."""
from pathlib import Path
import os
import secrets

ROOT = Path(__file__).resolve().parents[1]


def main():
    text = (ROOT / '.env.example').read_text()
    values = {key: secrets.token_hex(32) for key in ['POSTGRES_PASSWORD', 'REDIS_PASSWORD', 'BACKEND_SECRET_KEY']}
    lines = []
    for line in text.splitlines():
        key = line.split('=', 1)[0]
        lines.append(key + '=' + values[key] if key in values else line)
    target = ROOT / '.env'
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        handle.write('\n'.join(lines) + '\n')
    print('Created private .env for the local demo. Existing deployments were not modified.')


if __name__ == '__main__':
    main()
