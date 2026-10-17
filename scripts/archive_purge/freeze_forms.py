"""Freeze SurveyCTO forms (data + every attachment) before a purge.

Usage: python freeze_forms.py <dest_dir> <form_id> [<form_id> ...]

For each form, writes into <dest_dir>/<form_id>/:
  <form_id>_wide.csv        exact bytes of GET /api/v1/forms/data/wide/csv/<id>
  <form_id>_wide.json       exact bytes of GET /api/v2/forms/data/wide/json/<id>?date=0
  <form_id>_definition_<version>.xlsx   deployed form definition
  media/<file>              every attachment, downloaded from the server
  <form_id>_attachments.csv one row per attachment (KEY, column, file, url, bytes, sha256)
and <dest_dir>/manifest.json with counts, hashes and the server's storage figure.
Attachments are found from the deployed XForm's binary fields; a cell may hold a
URL (v1 or v2 style) or a bare file name, in which case the v2 attachment URL is
built from the KEY. Stops at the first login failure. Writes nothing elsewhere.
"""
import concurrent.futures as cf, csv, hashlib, io, json, os, re, sys, time
from urllib.parse import urlparse, parse_qs
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import requests
import surveycto_upload as su

S = 'kilongajfl.surveycto.com'; U = 'duncan.webb@psemail.eu'
DEST = sys.argv[1]; FORMS = sys.argv[2:]
pw = su.read_keychain_password(S, U)
AUTH = (U, pw)

def get(url, **kw):
    for attempt in range(1, 16):
        try:
            r = requests.get(url, auth=AUTH, headers={'X-OpenRosa-Version': '1.0'}, timeout=(15, 600), **kw)
        except requests.RequestException as e:
            print('  network error, waiting 30 s:', type(e).__name__, flush=True); time.sleep(30); continue
        if r.status_code == 401:
            raise SystemExit(f'HTTP 401 on {url}; stopping (never retry login failures)')
        if r.status_code == 417:
            # wait exactly as long as the server says, so the slot is ours when it opens
            m = re.search(r'wait for (\d+) seconds', r.text)
            wait = int(m.group(1)) + 2 if m else 60
            print(f'  417, waiting {wait} s', flush=True); time.sleep(wait); continue
        if r.status_code in (409, 429) or r.status_code >= 500:  # 409: another export is running
            ra = r.headers.get('Retry-After', '')
            wait = int(ra) if ra.isdigit() else min(30 * attempt, 300)
            print(f'  HTTP {r.status_code}, waiting {wait} s', flush=True); time.sleep(wait); continue
        return r
    raise SystemExit(f'gave up on {url}')

def attachment_name(v):
    q = parse_qs(urlparse(v).query)
    return q['file'][0] if 'file' in q else urlparse(v).path.rsplit('/', 1)[-1]

BARE = re.compile(r'^[\w.\-]+\.(jpe?g|png|gif|m4a|amr|mp3|wav|3gpp?|mp4|aac|ogg|webm|csv)$', re.I)

session = su.load_session(S, username=U, auth_mode='keychain')
tok = su.fetch_csrf_token(session, S)
storage = session.get(f'https://{S}/console/usage/storage?t={int(time.time()*1000)}', headers={'X-csrf-token': tok}, timeout=60).json()
formlist = get(f'https://{S}/formList').text
dl_urls = dict(re.findall(r'<formID>(.*?)</formID>.*?<downloadUrl>(.*?)</downloadUrl>', formlist, re.S))

manifest_path = os.path.join(DEST, 'manifest.json')
manifest = json.load(open(manifest_path)) if os.path.exists(manifest_path) else {'server': S, 'forms': {}}
for fid in FORMS:
    out = os.path.join(DEST, fid); media = os.path.join(out, 'media')
    prev = manifest['forms'].get(fid)
    if prev and not prev['failed'] and prev['downloaded'] + prev.get('empty_on_server', 0) == prev['attachments']:
        print(f'== {fid}: already frozen completely; skipping', flush=True); continue
    csv_path, json_path = os.path.join(out, f'{fid}_wide.csv'), os.path.join(out, f'{fid}_wide.json')
    # resume an interrupted freeze: reuse its exports and the media already on disk
    resume = os.path.exists(csv_path) and os.path.exists(json_path)
    os.makedirs(media, exist_ok=True)
    print(f'== {fid}' + (' (resuming)' if resume else ''), flush=True)
    # binary fields from the deployed XForm
    xform = get(dl_urls[fid].replace('&amp;', '&')).text
    binary = set()
    for m in re.finditer(r'<bind\b[^>]*>', xform):
        b = m.group(0)
        if re.search(r'type="binary"', b):
            ns = re.search(r'nodeset="([^"]+)"', b).group(1)
            binary.add(ns.rstrip('/').rsplit('/', 1)[-1])
    if resume:
        v1c, v2c = open(csv_path, 'rb').read(), open(json_path, 'rb').read()
    else:
        v1 = get(f'https://{S}/api/v1/forms/data/wide/csv/{fid}')
        if v1.status_code != 200: raise SystemExit(f'{fid}: v1 CSV HTTP {v1.status_code}')
        v1c = v1.content; open(csv_path, 'wb').write(v1c)
        v2 = get(f'https://{S}/api/v2/forms/data/wide/json/{fid}?date=0')
        if v2.status_code != 200: raise SystemExit(f'{fid}: v2 JSON HTTP {v2.status_code}')
        v2c = v2.content; open(json_path, 'wb').write(v2c)
    rows_json = json.loads(v2c)
    rows_csv = list(csv.DictReader(io.StringIO(v1c.decode('utf-8-sig'))))
    keys_csv = [r['KEY'] for r in rows_csv]; keys_json = [r['KEY'] for r in rows_json]
    assert len(set(keys_csv)) == len(keys_csv), 'duplicate KEYs in CSV'
    if set(keys_csv) != set(keys_json): raise SystemExit(f'{fid}: CSV and JSON KEY sets differ')
    # attachments: binary-field cells in either export, URL or bare name
    wanted = {}
    for source in (rows_json, rows_csv):
        for r in source:
            for col, v in r.items():
                if not isinstance(v, str) or not v.strip(): continue
                v = v.strip()
                base = re.sub(r'(_\d+)+$', '', col)
                is_url = v.startswith(f'https://{S}/') and ('submission-attachment' in v or '/attachments/' in v)
                # Any media-like file name counts, not only the current XForm's binary
                # fields: fields dropped in later versions keep bare names in the exports
                # (sqb_scoping_v1 nominee audio). A false positive fails safe as a 404.
                if is_url or BARE.match(v):
                    name = attachment_name(v) if is_url else v
                    url = (f'https://{S}/api/v2/forms/{fid}/submissions/{r["KEY"]}/attachments/{name}'
                           if not (is_url and '/attachments/' in v) else v)
                    wanted.setdefault((r['KEY'], name), {'KEY': r['KEY'], 'column': col, 'file': name, 'url': url})
    items = list(wanted.values())
    print(f'  {len(rows_csv)} submissions, {len(binary)} binary fields, {len(items)} attachments to download', flush=True)

    def fetch(it):
        r = get(it['url'])
        if r.status_code != 200 or not r.content:
            return it, None, f'HTTP {r.status_code}, {len(r.content)} bytes'
        return it, r.content, None
    seen = {}; rows_out = []; failures = []

    def record(it, content):
        """Store one attachment (atomic write) unless an identical copy is already recorded."""
        h = hashlib.sha256(content).hexdigest()
        rel = os.path.join('media', it['file'])
        if it['file'] in seen and seen[it['file']] != h:
            rel = os.path.join('media', it['KEY'].replace('uuid:', '') + '_' + it['file'])
        target = os.path.join(out, rel)
        if not (os.path.exists(target) and os.path.getsize(target) == len(content)
                and hashlib.sha256(open(target, 'rb').read()).hexdigest() == h):
            with open(target + '.part', 'wb') as f: f.write(content)
            os.replace(target + '.part', target)
        seen.setdefault(it['file'], h)
        rows_out.append({**it, 'archive_path': rel, 'bytes': len(content), 'sha256': h})

    to_fetch = items
    if resume:
        # Files are written one at a time after a full download, so only the most
        # recently written file can be incomplete; re-download the newest few.
        on_disk = sorted((e for e in os.scandir(media) if e.is_file() and not e.name.endswith('.part')),
                         key=lambda e: e.stat().st_mtime)
        redo = {e.name for e in on_disk[-8:]}
        have = {e.name for e in on_disk if e.stat().st_size > 0} - redo
        to_fetch = []
        for it in items:
            prefixed = it['KEY'].replace('uuid:', '') + '_' + it['file']
            if prefixed in have:
                record(it, open(os.path.join(media, prefixed), 'rb').read())
            elif it['file'] in have and prefixed not in redo:
                record(it, open(os.path.join(media, it['file']), 'rb').read())
            else:
                to_fetch.append(it)
        print(f'  resuming: {len(items) - len(to_fetch)} reused from disk, {len(to_fetch)} to download', flush=True)
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        for n, (it, content, err) in enumerate(ex.map(fetch, to_fetch), 1):
            if err:
                failures.append((it['KEY'], it['file'], err)); continue
            record(it, content)
            if n % 250 == 0: print(f'  {n}/{len(to_fetch)} downloaded', flush=True)
    with open(os.path.join(out, f'{fid}_attachments.csv'), 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['KEY', 'column', 'file', 'url', 'archive_path', 'bytes', 'sha256'])
        w.writeheader(); w.writerows(rows_out)
    files = session.get(f'https://{S}/forms/{fid}/files?t={int(time.time()*1000)}', timeout=60).json()
    dep = files['deployedGroupFiles']['definitionFile']; link = dep['downloadLink']
    link = link if link.startswith('http') else f'https://{S}{link}'
    ver = files.get('xForm', {}).get('version') or dep.get('formVersion', 'unknown')
    open(os.path.join(out, f'{fid}_definition_{ver}.xlsx'), 'wb').write(session.get(link, timeout=120).content)
    on_disk = sum(os.path.getsize(os.path.join(media, x)) for x in os.listdir(media))
    info = {'frozen_at_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'submissions': len(keys_csv), 'csv_sha256': hashlib.sha256(v1c).hexdigest(),
            'json_sha256': hashlib.sha256(v2c).hexdigest(), 'binary_fields': sorted(binary),
            'attachments': len(items), 'downloaded': len(rows_out), 'failed': len(failures),
            'media_files_on_disk': len(os.listdir(media)), 'media_bytes_on_disk': on_disk,
            'server_storage_bytes': storage.get(fid), 'definition_version': ver}
    manifest['forms'][fid] = info
    json.dump(manifest, open(manifest_path, 'w'), indent=1)
    print(' ', json.dumps({k: info[k] for k in ('submissions', 'attachments', 'downloaded', 'failed', 'media_files_on_disk', 'media_bytes_on_disk', 'server_storage_bytes')}), flush=True)
    for fl in failures[:10]: print('  FAILED', fl)
