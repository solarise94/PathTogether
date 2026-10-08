import json,hashlib,subprocess as sp
from pathlib import Path
R=Path('/home/solarise/releases/suite-20261008-registration')
image='localhost/pathtogether-demo:suite-20261008-registration'
code="""import pathlib,hashlib,json
p=pathlib.Path('/app')
print(json.dumps({str(f.relative_to(p)):hashlib.sha256(f.read_bytes()).hexdigest() for f in p.rglob('*') if f.is_file() and '__pycache__' not in str(f)}))
"""
h=json.loads(sp.check_output(['podman','run','--rm',image,'python3','-c',code],text=True))
for rel,sha in h.items():
 p=R/'source'/rel
 assert p.is_file(),rel
 assert hashlib.sha256(p.read_bytes()).hexdigest()==sha,rel
for rel in ('registration_antibot.py','migrations/0079_registration_antibot_redelivery.sql',
            'static/register-turnstile.js','templates/registration_help.html',
            'upload_direct_class.py','migrations/0078_ingestion_direct_class.sql','static/upload/cos-uploader.js'):
 assert rel in h
m=json.loads((R/'source/static/tools/slide-transform/build-manifest.json').read_text())
for rel,sha in m['artifacts'].items():
 if rel=='slide-transform':continue
 assert h['static/tools/slide-transform/'+rel]==sha,rel
p=R/'source/plugins/pathtogether-admin';m=json.loads((p/'manifest.json').read_text())
policy=json.loads((R/'source/plugins/source-policy.json').read_text())
assert hashlib.sha256((p/'manifest.json').read_bytes()).hexdigest()==policy['pathtogether-admin']
for rel,sha in m['ui']['fileHashes'].items():assert hashlib.sha256((p/rel).read_bytes()).hexdigest()==sha
(R/'expected-static.sha').write_text(''.join(sha+'  '+rel+'\n' for rel,sha in sorted(h.items()) if rel.startswith('static/')))
(R/'image-hashes.json').write_text(json.dumps(h,indent=2))
print('PASS image files',len(h),'manifest artifacts, admin plugin',m['pluginVersion'],'static',sum(r.startswith('static/') for r in h))

previous=json.loads(Path('/home/solarise/releases/suite-20261008-domains/image-hashes.json').read_text())
changed=sorted(r for r in set(h)|set(previous) if h.get(r)!=previous.get(r))
allowed={'app.py','identity_store.py','registration_antibot.py','registration_store.py',
         'registration_mail_worker.py','migrations/0079_registration_antibot_redelivery.sql',
         'static/entry-auth.js','static/entry.css','static/i18n.js','static/register-turnstile.js',
         'static/registration-help.css','static/registration-help.js','templates/_login_dialog.html',
         'templates/entry.html','templates/registration_help.html','templates/verify_email.html'}
assert set(changed)==allowed,(set(changed)-allowed,allowed-set(changed))
assert all(h[r]==previous[r] for r in previous if r.startswith('plugins/'))
print('Shipped registration changes:',len(changed),'; plugins unchanged')
info=json.loads(sp.check_output(['podman','image','inspect',image],text=True))[0]
(R/'release.json').write_text(json.dumps({'image':image,'image_id':info['Id'],
    'revision':info['Config']['Labels']['org.opencontainers.image.revision'],
    'baseline_image_id':'6aaea3a020648f5acb5d82f8f424bae8bb5f7e040023b8bed90a0d640ff2ce6e'},indent=2))
