"""Create local Langfuse credentials without printing secrets or overwriting a deployment."""
import argparse
import os
from pathlib import Path
import secrets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    values = {key: secrets.token_hex(24) for key in (
        'POSTGRES_PASSWORD', 'CLICKHOUSE_PASSWORD', 'MINIO_ROOT_PASSWORD',
        'REDIS_AUTH', 'SALT', 'NEXTAUTH_SECRET', 'LANGFUSE_INIT_USER_PASSWORD')}
    values.update(
        ENCRYPTION_KEY=secrets.token_hex(32),
        LANGFUSE_BASE_URL='http://localhost:17300',
        LANGFUSE_PUBLIC_KEY='pk-lf-' + secrets.token_hex(16),
        LANGFUSE_SECRET_KEY='sk-lf-' + secrets.token_hex(24),
        LANGFUSE_INIT_USER_EMAIL='admin@moi-benchmark.local',
    )
    try:
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(f'Using existing credential file: {args.output}')
        return
    with os.fdopen(fd, 'w') as stream:
        for key, value in values.items():
            stream.write(f'{key}={value}\n')
    print(f'Created credential file: {args.output}')


if __name__ == '__main__':
    main()
