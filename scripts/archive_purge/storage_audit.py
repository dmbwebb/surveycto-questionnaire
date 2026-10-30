"""Read-only SurveyCTO storage audit: total usage and the largest forms.

Usage: python storage_audit.py [--top N] [--json <out.json>]
Prints current/max storage and, per form, bytes, submissions, last incoming
date, encryption and group, largest first. Changes nothing on the server.
"""
import argparse, datetime, json, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import surveycto_upload as su

S = 'kilongajfl.surveycto.com'; U = 'duncan.webb@psemail.eu'
ap = argparse.ArgumentParser(); ap.add_argument('--top', type=int, default=25); ap.add_argument('--json')
a = ap.parse_args()

s = su.load_session(S, username=U, auth_mode='keychain'); h = {'X-csrf-token': su.fetch_csrf_token(s, S)}
get = lambda path: s.get(f'https://{S}/console/{path}?t={int(time.time() * 1000)}', headers=h, timeout=60).json()
storage, usage, fgd = get('usage/storage'), get('usage/get')['usageModel'], get('forms-groups-datasets/get')
if a.json:
    json.dump({'storage': storage, 'usage': usage, 'fgd': fgd}, open(a.json, 'w'))

cur, mx = usage['currentDataUsage'], usage['maxDataUsage']
print(f'{cur / 1e9:.2f} GB of {mx / 1e9:.2f} GB ({cur / mx:.0%})')
forms = {f['id']: f for f in fgd['forms']}; groups = {g['id']: g['title'] for g in fgd['groups']}
for fid, b in sorted(storage.items(), key=lambda x: -x[1])[:a.top]:
    f = forms.get(fid, {})
    n = f.get('completeSubmissionCount', 0) + f.get('incompleteSubmissionCount', 0)
    last = f.get('lastIncomingDataDate')
    last = datetime.date.fromtimestamp(last / 1000).isoformat() if last else '-'
    enc = 'enc' if f.get('encrypted') else '   '
    print(f'{b / 1e9:6.2f} GB  {fid:42s} n={n:5d} last={last} {enc} {groups.get(f.get("groupId"), "?")[:30]}')
