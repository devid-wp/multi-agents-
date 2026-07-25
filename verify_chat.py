"""End-to-end verification: send a test prompt to Trinity and stream events."""
import requests, json, time, sys, io

# Force UTF-8 output to avoid cp1251 charmap errors on Windows console
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = sys.stdout
except Exception:
    pass


def safe(s):
    return str(s).encode('ascii', 'replace').decode('ascii')

url = 'http://localhost:8000/api/chat'
provider = {
    'provider': 'ollama',
    'base_url': 'http://localhost:11434',
    'model_name': 'llama3.1:8b',
    'api_key': '',
}
# Trivial task that fits the available model and avoids lengthy generation
payload = {
    'message': 'Create file hello.txt with text "Hi from Trinity".',
    'strategy': 'direct',
    'session_id': 'verify-test-1',
    'ephemeral_credentials': {
        'planner':  provider,
        'critic':   provider,
        'executor': provider,
    },
}

print('Sending chat request to Trinity /api/chat...', flush=True)
t0 = time.time()
r = requests.post(url, json=payload, stream=True, timeout=900)
print(f'HTTP {r.status_code} - content-type={r.headers.get("content-type")}', flush=True)

events = []
for raw in r.iter_lines(decode_unicode=True):
    if not raw:
        continue
    if raw.startswith('data:'):
        body = raw[5:].strip()
        if not body:
            continue
        try:
            ev = json.loads(body)
        except Exception as e:
            print(f'  [parse-err] {e}: {body[:80]}', flush=True)
            continue
        kind = ev.get('kind') or '?'
        agent = ev.get('agent') or '?'
        content = safe((ev.get('content') or '')[:240]).replace('\n', ' ')
        events.append((kind, agent, content))
        print(f'  [{kind:10}] {agent:9} : {content}', flush=True)
elapsed = time.time() - t0
print(f'--- Total events: {len(events)}; total time: {elapsed:.1f}s ---', flush=True)

# Tally by agent
by_agent = {}
for k, a, _ in events:
    by_agent.setdefault(a, []).append(k)
print('Per-agent event count:', flush=True)
for a, kinds in by_agent.items():
    print(f'  {a:9} : {len(kinds):3} events; kinds={sorted(set(kinds))}', flush=True)

# Detect 500-style errors
errs = [c for k, _, c in events if k == 'error']
if errs:
    print('ERRORS in stream:', errs, flush=True)
    sys.exit(2)
if not events:
    print('No events received — likely 500.', flush=True)
    sys.exit(3)

# Check for final event
finals = [c for k, _, c in events if k == 'final']
if finals:
    print('FINAL:', finals[0][:300], flush=True)
    sys.exit(0)
print('No final event — run did not complete.', flush=True)
sys.exit(4)
