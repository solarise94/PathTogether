"""Build atop the verified production image when dependencies are unchanged."""
import hashlib
from pathlib import Path
import re
import subprocess
import sys

import deploy


def main():
    revision = sys.argv[1] if len(sys.argv) == 2 else ''
    if not re.fullmatch('[0-9a-f]{40}', revision):
        raise SystemExit('usage: build.py FULL_GIT_REVISION')
    current = deploy.inspect(deploy.PT)
    if current['Image'] != deploy.OLD_IMAGE_ID:
        raise SystemExit('production baseline changed; refusing build')
    info = deploy.inspect(deploy.OLD_IMAGE)
    if info['Id'] != deploy.OLD_IMAGE_ID:
        raise SystemExit('baseline image tag changed; refusing build')
    source = deploy.ROOT / 'source'
    expected = hashlib.sha256((source / 'requirements.txt').read_bytes()).hexdigest()
    actual = deploy.run('podman', 'run', '--rm', deploy.OLD_IMAGE, 'python3', '-c',
                        "import hashlib; print(hashlib.sha256(open('/app/requirements.txt','rb').read()).hexdigest())")
    if actual != expected:
        raise SystemExit('dependencies changed; a full dependency build is required')
    original = (source / 'Containerfile').read_text().splitlines()
    pip_step = 'RUN pip install --no-cache-dir -r requirements.txt'
    if sum(line == pip_step for line in original) != 1:
        raise SystemExit('Containerfile dependency step changed; refusing partial build')
    output = ['FROM ' + deploy.OLD_IMAGE if line.startswith('FROM ') else line
              for line in original if line != pip_step]
    recipe = deploy.ROOT / 'Containerfile.release'
    recipe.write_text('\n'.join(output) + '\n')
    with (deploy.ROOT / 'build.log').open('w') as log:
        subprocess.run(['podman', 'build', '--pull=never',
                        '--label', 'org.opencontainers.image.revision=' + revision,
                        '-t', deploy.NEW_IMAGE, '-f', str(recipe), str(source)],
                       stdout=log, stderr=subprocess.STDOUT, check=True)
    print('BUILD ok: exact production dependencies reused; revision', revision)
    subprocess.run([sys.executable, str(deploy.ROOT / 'check-image.py')], check=True)


if __name__ == '__main__':
    main()
