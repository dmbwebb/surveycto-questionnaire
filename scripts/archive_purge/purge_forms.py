"""Purge exactly the frozen submissions of SurveyCTO forms.

Usage: python purge_forms.py <archive_dir> <form_id> [<form_id> ...] [--execute]
<archive_dir>/<form_id>/<form_id>_wide.csv (from freeze_forms.py) supplies the KEYs.
Without --execute: checks only. Stops if the server holds any submission that is
not frozen, if a freeze is incomplete, or at the first login failure. Records
linked server-dataset row counts before and after.
"""
import csv, json, os, re, sys, time
from urllib.parse import urlparse, parse_qs
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import requests
import surveycto_upload as su

S = 'kilongajfl.surveycto.com'; U = 'duncan.webb@psemail.eu'
args = [a for a in sys.argv[1:] if a != '--execute']; EXECUTE = '--execute' in sys.argv
ARCH, FORMS = args[0], args[1:]
pw = su.read_keychain_password(S, U)

def api_get(url):
    for attempt in range(1, 16):
        try:
            r = requests.get(url, auth=(U, pw), headers={'X-OpenRosa-Version': '1.0'}, timeout=(15, 600))
        except requests.RequestException as e:
            print('  network error, waiting 60 s:', type(e).__name__, flush=True); time.sleep(60); continue
        if r.status_code == 401: raise SystemExit(f'HTTP 401 on {url}; stopping')
        if r.status_code == 417:
            m = re.search(r'wait for (\d+) seconds', r.text)
            wait = int(m.group(1)) + 2 if m else 60
            print(f'  417, waiting {wait} s', flush=True); time.sleep(wait); continue
        if r.status_code in (409, 429) or r.status_code >= 500:  # 409: another export is running
            ra = r.headers.get('Retry-After', '')
            wait = int(ra) if ra.isdigit() else min(30 * attempt, 300)
            print(f'  HTTP {r.status_code}, waiting {wait} s', flush=True); time.sleep(wait); continue
        return r
    raise SystemExit(f'gave up on {url}')

def server_keys(fid):
    r = api_get(f'https://{S}/api/v2/forms/data/wide/json/{fid}?date=0')
    if r.status_code == 204: return set()
    r.raise_for_status()
    return {row['KEY'] for row in r.json()}

def console():
    s = su.load_session(S, username=U, auth_mode='keychain'); tok = su.fetch_csrf_token(s, S)
    t = lambda: int(time.time() * 1000)
    st = s.get(f'https://{S}/console/usage/storage?t={t()}', headers={'X-csrf-token': tok}, timeout=60).json()
    us = s.get(f'https://{S}/console/usage/get?t={t()}', headers={'X-csrf-token': tok}, timeout=60).json()['usageModel']
    fgd = s.get(f'https://{S}/console/forms-groups-datasets/get?t={t()}', headers={'X-csrf-token': tok}, timeout=60).json()
    ds = {d['id']: d.get('rows') for d in fgd['datasets']
          if any(l['linkObjectId'] in FORMS for l in d['dataLinkSummaries']) or set(d['outgoingFormIds']) & set(FORMS)}
    return s, tok, st, us, ds

manifest = json.load(open(os.path.join(ARCH, 'manifest.json')))
archived = {}
BARE = re.compile(r'^[\w.\-]+\.(jpe?g|png|gif|m4a|amr|mp3|wav|3gpp?|mp4|aac|ogg|webm|csv)$', re.I)
for fid in FORMS:
    info = manifest['forms'][fid]
    # attachments the server itself returns as 0-byte files, documented with evidence
    ep = os.path.join(ARCH, fid, 'empty_on_server.json')
    empty = {(e['KEY'], e['file']) for e in json.load(open(ep))['attachments']} if os.path.exists(ep) else set()
    if info['failed'] or info['downloaded'] + len(empty) != info['attachments']:
        raise SystemExit(f'{fid}: freeze incomplete ({info["downloaded"]}/{info["attachments"]} attachments, {info["failed"]} failed, {len(empty)} documented empty)')
    # independent check: every attachment the exports mention is in the archive, on disk and non-empty
    att = list(csv.DictReader(open(os.path.join(ARCH, fid, f'{fid}_attachments.csv'))))
    have = {(a['KEY'], a['file']) for a in att}
    for a in att:
        if a['sha256'] == 'EMPTY_ON_SERVER':
            if (a['KEY'], a['file']) not in empty or int(a['bytes']) != 0:
                raise SystemExit(f'{fid}: {a["file"]} marked empty but not documented in empty_on_server.json')
            continue
        p = os.path.join(ARCH, fid, a['archive_path'])
        if not os.path.exists(p) or os.path.getsize(p) != int(a['bytes']) or int(a['bytes']) == 0:
            raise SystemExit(f'{fid}: archived file missing or wrong size: {a["archive_path"]}')
    rows = json.load(open(os.path.join(ARCH, fid, f'{fid}_wide.json')))
    with open(os.path.join(ARCH, fid, f'{fid}_wide.csv'), newline='', encoding='utf-8-sig') as f:
        rows += list(csv.DictReader(f))
    gaps = set()
    for r in rows:
        for c, v in r.items():
            if not isinstance(v, str) or not v.strip(): continue
            v = v.strip()
            if v.startswith(f'https://{S}/') and ('submission-attachment' in v or '/attachments/' in v):
                q = parse_qs(urlparse(v).query); name = q['file'][0] if 'file' in q else urlparse(v).path.rsplit('/', 1)[-1]
            elif BARE.match(v):
                name = v
            else:
                continue
            if (r['KEY'], name) not in have: gaps.add((r['KEY'], name))
    if gaps:
        raise SystemExit(f'{fid}: {len(gaps)} attachment(s) named in the exports are not archived, e.g. {sorted(gaps)[:2]}')
    with open(os.path.join(ARCH, fid, f'{fid}_wide.csv'), newline='', encoding='utf-8-sig') as f:
        archived[fid] = [r['KEY'] for r in csv.DictReader(f)]
    assert len(archived[fid]) == len(set(archived[fid])) == info['submissions'], fid

s, tok, st, us, ds = console()
print(f"before: {us['currentDataUsage']/1e9:.2f} of {us['maxDataUsage']/1e9:.2f} GB;",
      {fid: f'{st.get(fid, 0)/1e9:.3f} GB' for fid in FORMS}, '| linked datasets rows:', ds)
for fid in FORMS:
    srv = server_keys(fid); arc = set(archived[fid])
    print(f'{fid}: server {len(srv)}, archive {len(arc)}, server-not-archived {len(srv - arc)}, archived-not-on-server {len(arc - srv)}', flush=True)
    if srv - arc: raise SystemExit(f'{fid}: the server has submissions that are not frozen; refreeze first')

if not EXECUTE:
    print('dry run only; nothing purged'); sys.exit(0)

log = {}
for fid in FORMS:
    keys = archived[fid]; purged = []
    for i in range(0, len(keys), 50):
        batch = keys[i:i + 50]
        r = s.post(f'https://{S}/forms/{fid}/purge-specific', data={'uuids': ','.join(batch)},
                   headers={'X-csrf-token': tok}, timeout=120)
        r.raise_for_status(); got = r.json(); purged += got
        if set(got) != set(batch):
            raise SystemExit(f'{fid}: batch {i//50 + 1} mismatch (not purged: {sorted(set(batch) - set(got))[:3]}); stopping')
    log[fid] = purged
    print(f'{fid}: purged {len(purged)} of {len(keys)}', flush=True)
    with open(os.path.join(ARCH, fid, 'purge_log.json'), 'w') as f:
        json.dump({'purged_at_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                   'endpoint': f'/forms/{fid}/purge-specific', 'purged_keys': purged}, f, indent=1)

time.sleep(5)
for fid in FORMS:
    left = server_keys(fid)
    print(f'after: {fid}: server now has {len(left)} submissions; frozen-but-still-on-server {len(left & set(archived[fid]))}', flush=True)
_, _, st2, us2, ds2 = console()
print(f"after: {us2['currentDataUsage']/1e9:.2f} of {us2['maxDataUsage']/1e9:.2f} GB;",
      {fid: f'{st2.get(fid, 0)/1e9:.3f} GB' for fid in FORMS}, '| linked datasets rows:', ds2)
