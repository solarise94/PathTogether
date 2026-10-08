"""Registration release from suite-20261008-domains.

preflight checks baseline/image and secret presence without printing credentials.
accept-check restores a read-only production snapshot into a private PostgreSQL
container and replaces every writable production mount with private directories.
All acceptance workers are off. It never starts candidate code against production.
prepare requires a 0600 secret and successful acceptance with the same configuration.
cutover-pt additionally requires an explicit --approved flag and a fresh backup.
rollback restores the old container; additive migration 0079 remains in place.
"""
import hashlib
import getpass
import json
import os
import secrets
import socket
import stat
import subprocess
import sys
import time
import urllib.request
from urllib.parse import quote
from pathlib import Path

ROOT = Path('/home/solarise/releases/suite-20261008-registration')
TAG = 'suite-20261008-registration'
PT = 'pathtogether-demo'
PT_STAGED = PT + '-staged-' + TAG
PT_ACCEPT = PT + '-accept-' + TAG
PT_PRE = PT + '-pre-' + TAG
PT_FAILED = PT + '-failed-' + TAG
NEW_IMAGE = 'localhost/%s:%s' % (PT, TAG)
OLD_IMAGE = 'localhost/pathtogether-demo:suite-20261008-domains'
OLD_IMAGE_ID = '6aaea3a020648f5acb5d82f8f424bae8bb5f7e040023b8bed90a0d640ff2ce6e'
BUDGET = {'capacity': 10_000_000_000, 'safety': 500_000_000, 'max_upload': 9_500_000_000}
WORKERS_OFF = ('SAMPLE_TMA_BACKEND', 'REGISTRATION_MAIL_WORKER', 'FORMAT_REQUEST_WORKER',
               'RESEARCH_DELETION_WORKER', 'CONVERSION_WORKER', 'COS_INGEST_WORKER',
               'BAIDU_IMPORT_WORKER')
PUBLIC = ('https://histopilot.cn', 'https://histopilot.com')
EXTRA_ENV = {
    'REGISTRATION_TURNSTILE_REQUIRED': '1',
    'TURNSTILE_SITE_KEY': '0x4AAAAAAFQ5a-1qUKR2Uyyz',
    'TURNSTILE_HOSTNAMES': 'histopilot.cn,histopilot.com',
}
SECRET_FILE = ROOT / 'turnstile.secret.env'
ACCEPT_DB = 'registration-accept-pg-' + TAG
ACCEPT_DB_PORT = 18479
os.umask(0o077)


def candidate_identity():
    info = json.loads((ROOT / 'release.json').read_text())
    assert info['image'] == NEW_IMAGE
    assert info['baseline_image_id'] == OLD_IMAGE_ID
    return info


def load_secret(required=True):
    if not SECRET_FILE.exists():
        if required:
            raise RuntimeError('TURNSTILE_SECRET file is missing; no production change made')
        return ''
    st = SECRET_FILE.lstat()
    if not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) != 0o600 or st.st_uid != os.getuid():
        raise RuntimeError('secret file must be an owned regular file with mode 0600')
    lines = [s.strip() for s in SECRET_FILE.read_text().splitlines()
             if s.strip() and not s.lstrip().startswith('#')]
    if len(lines) != 1 or not lines[0].startswith('TURNSTILE_SECRET='):
        raise RuntimeError('secret file must contain only TURNSTILE_SECRET=value')
    value = lines[0].partition('=')[2].strip()
    if (not value or value.startswith(('1x000', '2x000', '3x000', '<'))
            or value in ('REPLACE_ME', 'YOUR_SECRET_KEY') or any(c.isspace() for c in value)):
        raise RuntimeError('production secret is empty, a placeholder, or a test key')
    return value


def store_secret():
    """Interactive terminal only: credentials never enter argv or shell history."""
    if not sys.stdin.isatty():
        raise RuntimeError('store-secret requires an interactive terminal')
    value = getpass.getpass('Cloudflare TURNSTILE_SECRET (input hidden): ').strip()
    if (not value or value.startswith(('1x000', '2x000', '3x000', '<'))
            or value in ('REPLACE_ME', 'YOUR_SECRET_KEY') or any(c.isspace() for c in value)):
        raise RuntimeError('production secret is empty, a placeholder, or a test key')
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as f:
        os.fchmod(f.fileno(), 0o600)
        f.write('TURNSTILE_SECRET=' + value + '\n')
    load_secret()
    print('Saved production secret to', SECRET_FILE, 'mode 0600; value not printed')


def release_env(required=True):
    env = dict(EXTRA_ENV)
    secret = load_secret(required=required)
    if secret:
        env['TURNSTILE_SECRET'] = secret
    return env


def write_env(path, env):
    if any('\n' in str(k) + str(v) or '\r' in str(k) + str(v) for k, v in env.items()):
        raise RuntimeError('multiline environment value is unsupported')
    path.write_text(''.join('%s=%s\n' % (k, v) for k, v in sorted(env.items())))
    path.chmod(0o600)


def effective_config_fingerprint():
    # Stored privately and never printed; acceptance cannot be reused after key rotation.
    return hashlib.sha256(json.dumps(release_env(), sort_keys=True).encode()).hexdigest()


def preflight():
    verify_baseline()
    ready = bool(load_secret(required=False))
    print('TURNSTILE_SECRET configured:', ready, 'file mode required: 0600')
    print('Widget hostnames/mode require console confirmation; production hosts:', EXTRA_ENV['TURNSTILE_HOSTNAMES'])
    if not ready:
        raise SystemExit(2)


def run(*args):
    return subprocess.check_output(list(args), text=True).strip()


def inspect(name):
    return json.loads(run('podman', 'inspect', name))[0]


def exists(name):
    return subprocess.run(['podman', 'container', 'exists', name]).returncode == 0


def env_dict(env_list):
    return dict(e.split('=', 1) for e in env_list)


def image_cfg(image):
    return json.loads(run('podman', 'image', 'inspect', image))[0]['Config']


def image_env(image):
    return env_dict(image_cfg(image).get('Env') or [])


def own_env(container, image=None):
    """Env set on the container itself (values equal to the image default excluded)."""
    img = image_env(image or container['ImageName'])
    return {k: v for k, v in env_dict(container['Config']['Env']).items() if img.get(k) != v}


def shape(x):
    return {
        'command': x['Config']['Cmd'], 'workdir': x['Config']['WorkingDir'],
        'user': x['Config']['User'], 'network': x['HostConfig']['NetworkMode'],
        'restart': x['HostConfig']['RestartPolicy']['Name'],
        'mounts': sorted((m['Type'], m.get('Name') or m['Source'], m['Destination'], m['RW'])
                         for m in x['Mounts']),
        'memory': x['HostConfig']['Memory'], 'ulimits': x['HostConfig']['Ulimits'],
    }


def budget_in(container):
    out = run('podman', 'exec', container, 'python3', '-c',
              'import cos_config, upload_guard, json; print(json.dumps({'
              '"capacity": cos_config.COS_POOL_CAPACITY_BYTES, '
              '"safety": cos_config.COS_POOL_SAFETY_BYTES, '
              '"max_upload": upload_guard.UPLOAD_MAX_REQUEST_BYTES}))')
    return json.loads(out.splitlines()[-1])


def verify_baseline():
    info = candidate_identity()
    cur = inspect(PT)
    assert cur['State']['Running'], PT + ' is not running'
    assert cur['Image'] == OLD_IMAGE_ID, 'production baseline changed; rebuild/review before deployment'
    img = json.loads(run('podman', 'image', 'inspect', NEW_IMAGE))[0]
    assert img['Id'] == info['image_id'], 'candidate image id changed'
    assert img['Config']['Labels'].get('org.opencontainers.image.revision') == info['revision']
    b = budget_in(PT)
    assert b == BUDGET, 'production budget %s != %s' % (b, BUDGET)
    print('BASELINE ok', PT, cur['ImageName'], 'candidate', NEW_IMAGE, 'budget', b)
    return cur


def baseline():
    path = ROOT / ('before-%s.json' % PT)
    if path.exists():
        cur = json.loads(path.read_text())
        assert cur['Image'] == OLD_IMAGE_ID
        return cur
    cur = verify_baseline()
    path.write_text(json.dumps(cur))
    return cur


def clone(old, new_name, image, port=None, restart=None, workers_off=False,
          extra=None, isolated_mounts=False):
    if old['Config']['Entrypoint'] != image_cfg(image).get('Entrypoint'):
        raise RuntimeError('entrypoint differs between images for ' + new_name)
    env = own_env(old)
    env.pop('REGISTRATION_TURNSTILE_ALLOW_TEST_KEYS', None)
    env.update(extra or {})
    if port is not None:
        env['PORT'] = str(port)
    if workers_off:
        for k in WORKERS_OFF:
            env[k] = '0'
    env_path = ROOT / (new_name + '.env')
    write_env(env_path, env)
    args = ['podman', 'create', '--name', new_name, '--network', old['HostConfig']['NetworkMode'],
            '--restart', restart or old['HostConfig']['RestartPolicy']['Name'] or 'no',
            '--env-file', str(env_path), '--workdir', old['Config']['WorkingDir']]
    if old['HostConfig'].get('ShmSize'):
        args += ['--shm-size', '%db' % old['HostConfig']['ShmSize']]
    for limit in old['HostConfig']['Ulimits'] or []:
        key = limit['Name'].removeprefix('RLIMIT_').lower()
        args += ['--ulimit', '%s=%s:%s' % (key, limit['Soft'], limit['Hard'])]
    for m in old['Mounts']:
        source = m.get('Name') if m['Type'] == 'volume' else m['Source']
        if isolated_mounts and m['RW']:
            source = ROOT / 'accept-data' / m['Destination'].strip('/').replace('/', '_')
            source.mkdir(parents=True, exist_ok=True)
            bootstrap = env.get('BOOTSTRAP_OWNER_PASSWORD_FILE', '')
            if bootstrap.startswith(m['Destination'].rstrip('/') + '/'):
                import shutil
                relative = bootstrap.removeprefix(m['Destination'].rstrip('/') + '/')
                origin = Path(m['Source']) / relative
                target = source / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(origin, target)
                target.chmod(0o600)
        args += ['-v', '%s:%s:%s' % (source, m['Destination'], 'rw' if m['RW'] else 'ro')]
    if old['Config']['User']:
        args += ['--user', old['Config']['User']]
    args += [image, *old['Config']['Cmd']]
    run(*args)
    new = inspect(new_name)
    exp, act = shape(old), shape(new)
    if restart:
        exp['restart'] = act['restart']
    if isolated_mounts:
        assert all(not m['RW'] or m['Source'].startswith(str(ROOT / 'accept-data') + '/')
                   for m in new['Mounts']), 'acceptance has writable production mounts'
        exp['mounts'] = act['mounts']
    if exp != act:
        raise RuntimeError('shape mismatch %s: %s' % (new_name, [k for k in exp if exp[k] != act[k]]))
    o, n = own_env(old), own_env(new, image)
    diff = sorted(k for k in set(o) | set(n) if o.get(k) != n.get(k))
    allowed = set(['PORT'] if port is not None else []) | (set(WORKERS_OFF) if workers_off else set())
    allowed |= set(extra or {}) | {'REGISTRATION_TURNSTILE_ALLOW_TEST_KEYS'}
    if not set(diff) <= allowed or (port is not None and 'PORT' not in diff):
        raise RuntimeError('env mismatch %s: %s' % (new_name, diff))
    if workers_off and any(n.get(k, image_env(image).get(k)) != '0' for k in WORKERS_OFF):
        raise RuntimeError('worker switch not forced off in ' + new_name)
    if 'REGISTRATION_TURNSTILE_ALLOW_TEST_KEYS' in env_dict(new['Config']['Env']):
        raise RuntimeError('test-key override leaked into candidate')
    print('CREATED', new_name, image, 'shape verified; env diff keys', diff)


def health(port, timeout=180, need_sidecar=False):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen('http://127.0.0.1:%d/healthz' % port, timeout=3) as r:
                last = json.load(r)
            if last.get('ok') and (not need_sidecar or last.get('sidecar') == 'reachable'):
                return last
        except Exception as exc:
            last = str(exc)
        time.sleep(1)
    raise RuntimeError('health check failed on %d: %s' % (port, last))


def fetch(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (HistoPilot release acceptance)'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.headers, r.read()


def prepare():
    verify_baseline()
    extras = release_env()
    stamp = json.loads((ROOT / 'acceptance-ok.json').read_text())
    assert stamp['image_id'] == candidate_identity()['image_id']
    assert stamp['configured'] and stamp['config_fingerprint'] == effective_config_fingerprint(), 'repeat acceptance with production configuration'
    pt_old = baseline()
    if exists(PT_STAGED):
        raise SystemExit(PT_STAGED + ' already exists')
    clone(pt_old, PT_STAGED, NEW_IMAGE, extra=extras)


def dump_database(path):
    with path.open('wb') as f:
        subprocess.check_call(['podman', 'exec', 'svs-pg', 'pg_dump', '-p', '5433', '-U', 'svs',
                               '-d', 'svs_demo', '-Fc'], stdout=f)
    path.chmod(0o600)


def prepare_acceptance_database():
    if exists(ACCEPT_DB):
        raise RuntimeError('acceptance PostgreSQL container already exists; inspect before removing it')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', ACCEPT_DB_PORT))
    password = secrets.token_urlsafe(32)
    pg_env = ROOT / 'accept-pg.secret.env'
    write_env(pg_env, {'POSTGRES_USER': 'svs', 'POSTGRES_DB': 'svs_demo', 'POSTGRES_PASSWORD': password})
    pg_image = inspect('svs-pg')['ImageName']
    run('podman', 'run', '-d', '--name', ACCEPT_DB, '--network', 'host', '--restart', 'no',
        '--env-file', str(pg_env), pg_image, 'postgres', '-p', str(ACCEPT_DB_PORT),
        '-c', 'listen_addresses=127.0.0.1')
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if subprocess.run(['podman', 'exec', ACCEPT_DB, 'pg_isready', '-p', str(ACCEPT_DB_PORT),
                           '-U', 'svs'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError('private acceptance database failed to start')
    dump = ROOT / 'acceptance-snapshot.dump'
    dump_database(dump)
    with dump.open('rb') as f:
        subprocess.run(['podman', 'exec', '-i', ACCEPT_DB, 'pg_restore', '-p', str(ACCEPT_DB_PORT),
                        '-U', 'svs', '-d', 'svs_demo', '--no-owner', '--no-privileges',
                        '--exit-on-error'], stdin=f, check=True)
    return 'postgresql://svs:%s@127.0.0.1:%d/svs_demo' % (quote(password, safe=''), ACCEPT_DB_PORT)


def accept_check():
    old = verify_baseline()
    configured = bool(load_secret(required=False))
    if exists(PT_ACCEPT):
        raise RuntimeError('acceptance container already exists; inspect before removing it')
    migration_before = psql('psql', '-p', '5433', '-U', 'svs', '-d', 'svs_demo', '-At', '-c',
                            'SELECT filename FROM schema_migrations ORDER BY filename')
    try:
        db_url = prepare_acceptance_database()
        extras = release_env(required=False)
        extras['DATABASE_URL'] = db_url
        clone(old, PT_ACCEPT, NEW_IMAGE, port=18090, restart='no', workers_off=True,
              extra=extras, isolated_mounts=True)
        run('podman', 'start', PT_ACCEPT)
        h = health(18090)
        print('ACCEPT_HEALTH', h)
        logs = run('podman', 'logs', PT_ACCEPT)
        for k in WORKERS_OFF:
            assert ('%s=0' % k) in logs, 'entrypoint did not skip ' + k
        b = budget_in(PT_ACCEPT)
        assert b == BUDGET, 'acceptance budget %s != %s' % (b, BUDGET)
        csp_new = fetch('http://127.0.0.1:18090/tools/slides')[0]['Content-Security-Policy']
        csp_old = fetch('http://127.0.0.1:18080/tools/slides')[0]['Content-Security-Policy']
        assert csp_new == csp_old, 'tool-page CSP differs from production'
        for host in ('histopilot.cn', 'histopilot.com'):
            for path in ('/register', '/registration-help'):
                req = urllib.request.Request('http://127.0.0.1:18090' + path, headers={'Host': host})
                with urllib.request.urlopen(req, timeout=15) as r:
                    body = r.read().decode()
                    csp = r.headers['Content-Security-Policy']
                assert 'solarise94@gmail.com' in body
                assert ('https://challenges.cloudflare.com' in csp) == (configured and path == '/register')
        applied = run('podman', 'exec', ACCEPT_DB, 'psql', '-p', str(ACCEPT_DB_PORT), '-U', 'svs',
                      '-d', 'svs_demo', '-At', '-c',
                      "SELECT count(*) FROM schema_migrations WHERE filename='0079_registration_antibot_redelivery.sql'")
        assert applied == '1', '0079 did not apply in isolated acceptance database'
        bad = 0
        lines = (ROOT / 'expected-static.sha').read_text().splitlines()
        for line in lines:
            sha, rel = line.split(None, 1)
            got = hashlib.sha256(fetch('http://127.0.0.1:18090/' + rel)[1]).hexdigest()
            if got != sha:
                bad += 1
                print('STATIC_MISMATCH', rel)
        assert bad == 0
        stamp = {'image_id': candidate_identity()['image_id'], 'configured': configured,
                 'config_fingerprint': effective_config_fingerprint() if configured else None,
                 'checked_at': time.time(), 'isolated_database': True}
        (ROOT / 'acceptance-ok.json').write_text(json.dumps(stamp))
        print('ACCEPT ok: isolated migration 0079; workers off; static files', len(lines),
              'production secret configured:', configured)
    finally:
        if exists(PT_ACCEPT):
            run('podman', 'rm', '-f', PT_ACCEPT)
        if exists(ACCEPT_DB):
            run('podman', 'rm', '-f', '-v', ACCEPT_DB)
        migration_after = psql('psql', '-p', '5433', '-U', 'svs', '-d', 'svs_demo', '-At', '-c',
                               'SELECT filename FROM schema_migrations ORDER BY filename')
        assert migration_after == migration_before, 'production migration state changed during acceptance'
        print('ACCEPT stopped; production migration state unchanged')


def psql(*args):
    return run('podman', 'exec', 'svs-pg', *args)


def backup():
    dump = ROOT / 'precutover.dump'
    dump_database(dump)
    with open(dump, 'rb') as f:
        listing = subprocess.run(['podman', 'exec', '-i', 'svs-pg', 'pg_restore', '-l'],
                                 stdin=f, capture_output=True, text=True, check=True).stdout
    (ROOT / 'precutover-dump-list.txt').write_text(listing)
    print('BACKUP', dump, dump.stat().st_size, 'bytes;', listing.count('\n'), 'TOC lines')


def quiesce_check():
    sql = (ROOT / 'quiesce.sql').read_text()
    busy = {}
    for line in psql('psql', '-p', '5433', '-U', 'svs', '-d', 'svs_demo', '-AtF|', '-c', sql).splitlines():
        if '|' in line:
            k, v = line.split('|')
            busy[k] = int(v)
    running = [n for n in (PT, PT_ACCEPT) if exists(n) and inspect(n)['State']['Running']]
    print('QUIESCE', json.dumps(busy), 'RUNNING', running)
    if any(busy.values()):
        sys.exit(2)


def cutover_pt():
    if '--approved' not in sys.argv:
        raise SystemExit('cutover requires --approved after explicit user deployment approval')
    verify_baseline()
    release_env()
    if exists(PT_PRE):
        raise RuntimeError('rollback container slot already exists')
    assert (ROOT / 'acceptance-ok.json').exists()
    stamp = json.loads((ROOT / 'acceptance-ok.json').read_text())
    assert stamp['image_id'] == candidate_identity()['image_id']
    assert stamp['configured'] and stamp['config_fingerprint'] == effective_config_fingerprint()
    if exists(PT_ACCEPT) and inspect(PT_ACCEPT)['State']['Running']:
        raise SystemExit('acceptance container still running')
    if not exists(PT_STAGED):
        raise SystemExit('staged container missing')
    candidate = inspect(PT_STAGED)
    assert candidate['Image'] == candidate_identity()['image_id']
    new_env = env_dict(candidate['Config']['Env'])
    assert all(new_env.get(k) == v for k, v in release_env().items())
    assert 'REGISTRATION_TURNSTILE_ALLOW_TEST_KEYS' not in new_env
    # Check work before stopping service; check again after all old writers stopped.
    quiesce_check()
    backup()
    run('podman', 'stop', '-t', '30', PT)
    try:
        quiesce_check()
        # Final backup after quiescence, before candidate startup applies 0079.
        backup()
    except BaseException:
        run('podman', 'start', PT)
        raise
    try:
        run('podman', 'rename', PT, PT_PRE)
        run('podman', 'rename', PT_STAGED, PT)
        run('podman', 'start', PT)
        print('PLATFORM_HEALTH', health(18080, need_sidecar=True), flush=True)
    except BaseException:
        rollback()
        raise
    (ROOT / 'cutover-pt-ok.txt').write_text(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))


def post_check():
    lines = (ROOT / 'expected-static.sha').read_text().splitlines()
    for base in PUBLIC:
        h = json.loads(fetch(base + '/healthz')[1])
        assert h.get('ok') and h.get('sidecar') == 'reachable', (base, h)
        hdr = fetch(base + '/tools/slides')[0]
        bad = []
        for line in lines:
            sha, rel = line.split(None, 1)
            if hashlib.sha256(fetch('%s/%s' % (base, rel))[1]).hexdigest() != sha:
                bad.append(rel)
        assert not bad, (base, bad)
        print('PUBLIC ok', base, 'static files', len(lines), 'CSP', hdr['Content-Security-Policy'][:40] + '…')


def rollback():
    NEW_IMAGE_ID = candidate_identity()['image_id']
    # Recover partial cutover/rollback as well as a fully running candidate.
    # Validate identities before stopping or renaming anything.
    cur = inspect(PT) if exists(PT) else None
    prev = inspect(PT_PRE) if exists(PT_PRE) else None
    failed = inspect(PT_FAILED) if exists(PT_FAILED) else None
    if prev and prev['Image'] != OLD_IMAGE_ID:
        raise SystemExit('rollback target is not the verified domains image; refusing')
    if failed and (failed['Image'] != NEW_IMAGE_ID or failed['State']['Running']):
        raise SystemExit('failed-container slot is unexpected or running; refusing')
    if exists(PT_STAGED) and inspect(PT_STAGED)['State']['Running']:
        raise SystemExit('staged container is running; refusing competing writers')
    if cur and cur['Image'] not in (OLD_IMAGE_ID, NEW_IMAGE_ID):
        raise SystemExit('current PT has an unexpected image; refusing')
    if cur and cur['Image'] == OLD_IMAGE_ID:
        if prev:
            raise SystemExit('two rollback candidates exist; refusing ambiguous state')
        # Old container stopped before cutover, or rollback already renamed it.
    else:
        if not prev:
            raise SystemExit(PT_PRE + ' missing; no verified rollback target')
        if cur:
            if failed:
                raise SystemExit('failed-container slot already occupied; refusing overwrite')
            if cur['State']['Running']:
                run('podman', 'stop', '-t', '30', PT)
            run('podman', 'rename', PT, PT_FAILED)
        # PT can be absent after either the first cutover rename or the first
        # rollback rename. In both cases the verified old container is PT_PRE.
        run('podman', 'rename', PT_PRE, PT)
    if not inspect(PT)['State']['Running']:
        run('podman', 'start', PT)
    print('ROLLBACK_HEALTH', health(18080, need_sidecar=True), flush=True)
    assert inspect(PT)['Image'] == OLD_IMAGE_ID
    (ROOT / 'rollback-ok.txt').write_text(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    {'store-secret': store_secret, 'preflight': preflight, 'verify-baseline': verify_baseline, 'prepare': prepare, 'accept-check': accept_check,
     'backup': backup, 'quiesce-check': quiesce_check, 'cutover-pt': cutover_pt,
     'post-check': post_check, 'rollback': rollback}.get(cmd, lambda: sys.exit(__doc__))()
