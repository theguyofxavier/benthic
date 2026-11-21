#!/usr/bin/env python3
"""Benthic Online: serves the game, accounts, cloud saves, friends, chat,
daily leaderboards, multiplayer lobbies and the AI judge. Python standard library only."""
import hashlib, hmac, json, os, re, secrets, sqlite3, subprocess, sys, threading, time, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ENV = os.path.join(HERE, '.env')
if os.path.exists(ENV):
    for line in open(ENV):
        if '=' in line and not line.lstrip().startswith('#'):
            k, v = line.strip().split('=', 1)
            os.environ.setdefault(k.strip(), v.strip())
GROQ_KEY = os.environ.get('GROQ_API_KEY', '')
GROQ_MODEL = os.environ.get('GROQ_MODEL', 'llama-3.3-70b-versatile')
PORT = int(os.environ.get('PORT', '8787'))
DB_PATH = os.path.join(HERE, 'benthic.db')
INDEX = os.path.join(HERE, 'index.html')
USER_RE = re.compile(r'^[a-zA-Z0-9_]{3,16}$')

# ---------- database ----------
db_lock = threading.Lock()
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript('''
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT UNIQUE COLLATE NOCASE, pw TEXT, salt TEXT, created REAL, seen REAL, data TEXT DEFAULT '{}', level INTEGER DEFAULT 1, xp INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, uid INTEGER, created REAL);
CREATE TABLE IF NOT EXISTS friends(a INTEGER, b INTEGER, status TEXT, created REAL, PRIMARY KEY(a, b));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, frm INTEGER, too INTEGER, text TEXT, t REAL, read INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS scores(uid INTEGER, day INTEGER, depth INTEGER, sq TEXT, t REAL, PRIMARY KEY(uid, day));
CREATE INDEX IF NOT EXISTS msg_pair ON messages(frm, too, id);
''')
db.execute('CREATE TABLE IF NOT EXISTS answers(prompt TEXT, ans TEXT, n INTEGER, PRIMARY KEY(prompt, ans))')
try: db.execute('ALTER TABLE users ADD COLUMN rcode TEXT')
except sqlite3.OperationalError: pass
def new_code():
    a = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    return '-'.join(''.join(secrets.choice(a) for _ in range(4)) for _ in range(3))
def code_hash(c): return hashlib.sha256(re.sub(r'[^A-Z0-9]', '', c.upper()).encode()).hexdigest()

def q(sql, args=(), one=False):
    with db_lock:
        cur = db.execute(sql, args)
        rows = cur.fetchall()
        db.commit()
    return (rows[0] if rows else None) if one else rows

def hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac('sha256', pw.encode(), bytes.fromhex(salt), 120_000).hex()

def tok_hash(t):
    return hashlib.sha256(t.encode()).hexdigest()

# ---------- rate limiting ----------
buckets, day_count = {}, {'d': '', 'n': 0}
def limited(key, per_min):
    now = time.time()
    q_ = [t for t in buckets.get(key, []) if now - t < 60]
    if len(q_) >= per_min:
        buckets[key] = q_
        return True
    q_.append(now)
    buckets[key] = q_
    return False

# ---------- AI judge ----------
def available_models():
    req = urllib.request.Request('https://api.groq.com/openai/v1/models',
                                 headers={'Authorization': f'Bearer {GROQ_KEY}', 'User-Agent': 'benthic'})
    with urllib.request.urlopen(req, timeout=15) as r:
        ids = [m['id'] for m in json.load(r).get('data', []) if m.get('active', True)]
    bad = ('whisper', 'tts', 'guard', 'embed', 'vision', 'audio', 'orpheus', 'prompt', 'compound')
    ids = [i for i in ids if not any(x in i.lower() for x in bad)]
    def score(i):
        l = i.lower()
        return ('llama' in l) * 3 + ('70b' in l or '120b' in l or 'maverick' in l) * 2 + ('versatile' in l) + ('instant' in l) * 0.5
    return sorted(ids, key=score, reverse=True)[:6]

def judge(prompt, answer):
    today = time.strftime('%Y-%m-%d')
    if day_count['d'] != today:
        day_count.update(d=today, n=0)
    if day_count['n'] >= 3000:
        raise RuntimeError('daily limit')
    day_count['n'] += 1
    system = ("You are the judge for a trivia game. Players type ONE thing that fits a category; rarer answers score more. "
              "Allow spelling mistakes, nicknames, missing accents and extra describing words. The answer must genuinely fit the category, "
              "including any starting-letter or other rule in it. Real things only. Be fair, not picky. "
              'Reply with JSON only: {"valid": true/false, "name": "properly spelled common name of what they meant", '
              '"rarity": 1-5 (1 = one of the first answers most people give, 3 = decent but not obvious, 5 = almost nobody would think of it), '
              '"reason": "if invalid, under 12 words"}')
    global GROQ_MODEL
    def call(model, fmt=True):
        payload = {'model': model, 'temperature': 0, 'max_tokens': 300,
                   'messages': [{'role': 'system', 'content': system},
                                {'role': 'user', 'content': f'Category: "{prompt}"\nPlayer typed: "{answer}"'}]}
        if fmt: payload['response_format'] = {'type': 'json_object'}
        req = urllib.request.Request('https://api.groq.com/openai/v1/chat/completions', data=json.dumps(payload).encode(),
                                     headers={'Authorization': f'Bearer {GROQ_KEY}', 'Content-Type': 'application/json', 'User-Agent': 'benthic'})
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.load(r)
    try:
        out = call(GROQ_MODEL)
    except urllib.error.HTTPError as e:
        if e.code not in (400, 404): raise
        detail = e.read()[:300].decode('utf-8', 'replace')
        print(f'judge: model {GROQ_MODEL} failed {e.code}: {detail}', flush=True)
        out = None
        for model in available_models():
            for fmt in (True, False):
                try:
                    out = call(model, fmt); GROQ_MODEL = model
                    print(f'judge: switched to {model}', flush=True); break
                except urllib.error.HTTPError as e2:
                    print(f'judge: {model} fmt={fmt} failed {e2.code}', flush=True)
            if out: break
        if not out: raise
    txt = out['choices'][0]['message']['content'] or ''
    txt = txt[txt.find('{'):txt.rfind('}') + 1]
    v = json.loads(txt)
    return {'valid': bool(v.get('valid')), 'name': str(v.get('name', ''))[:60],
            'rarity': max(1, min(5, int(v.get('rarity') or 3))), 'reason': str(v.get('reason', ''))[:120]}

# ---------- multiplayer lobbies (in memory) ----------
lobbies, lob_lock = {}, threading.Lock()
LOBBY_RE = re.compile(r'^[a-z0-9][a-z0-9_.-]{0,47}$')

def lobby_sync(name, peer, presence):
    now = time.time()
    with lob_lock:
        for n in list(lobbies):
            for p in list(lobbies[n]):
                if now - lobbies[n][p]['t'] > 12: del lobbies[n][p]
            if not lobbies[n]: del lobbies[n]
        room = lobbies.setdefault(name, {})
        if peer not in room and (len(room) >= 12 or len(lobbies) > 300):
            raise ValueError('lobby full')
        if presence is None:
            room.pop(peer, None)
        else:
            room[peer] = {'presence': presence, 't': now}
        return [{'peer': p, 'presence': v['presence']} for p, v in room.items()]

# ---------- profanity guard for usernames / chat (light) ----------
BAD = re.compile(r'f+u+c+k|sh[i1]t|c+u+n+t|n[i1]gg|f[a@]gg|b[i1]tch|wh[o0]re|sl[u]t|r[a@]pe|k[y]s\b', re.I)
def clean(text):
    return BAD.sub(lambda m: '*' * len(m.group(0)), text)

class H(BaseHTTPRequestHandler):
    server_version = 'Benthic'
    def ip(self):
        return self.headers.get('X-Forwarded-For', self.client_address[0]).split(',')[0].strip()
    def cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.send_header('Access-Control-Max-Age', '86400')
    def reply(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code); self.cors()
        self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers(); self.wfile.write(data)
    def body(self, cap=400_000):
        n = int(self.headers.get('Content-Length', '0') or 0)
        if n > cap: raise ValueError('too big')
        return json.loads(self.rfile.read(n) or b'{}')
    def user(self):
        a = self.headers.get('Authorization', '')
        if not a.startswith('Bearer '): return None
        s = q('SELECT uid FROM sessions WHERE token=?', (tok_hash(a[7:]),), one=True)
        if not s: return None
        u = q('SELECT * FROM users WHERE id=?', (s['uid'],), one=True)
        if u: q('UPDATE users SET seen=? WHERE id=?', (time.time(), u['id']))
        return u
    def log_message(self, *a): pass

    def do_OPTIONS(self):
        self.send_response(204); self.cors(); self.end_headers()

    def do_GET(self): self.route('GET')
    def do_POST(self): self.route('POST')
    def do_PUT(self): self.route('PUT')
    def do_DELETE(self): self.route('DELETE')

    def route(self, method):
        path = self.path.split('?')[0].rstrip('/') or '/'
        qs = dict(p.split('=', 1) for p in self.path.split('?', 1)[1].split('&') if '=' in p) if '?' in self.path else {}
        try:
            if method == 'GET' and path in ('/', '/index.html'):
                data = open(INDEX, 'rb').read()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8'); self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'no-cache'); self.end_headers(); self.wfile.write(data)
                return
            if path == '/api/health':
                return self.reply(200, {'ok': True, 'judge': bool(GROQ_KEY), 'model': GROQ_MODEL})
            if limited(self.ip(), 240):
                return self.reply(429, {'error': 'Slow down a bit.'})
            if path == '/judge' and method == 'POST':
                if limited('j:' + self.ip(), 15): return self.reply(429, {'error': 'slow down'})
                b = self.body(2000)
                prompt, answer = str(b.get('prompt', ''))[:160], str(b.get('answer', ''))[:80]
                if not prompt.startswith('Name ') or not answer.strip(): return self.reply(400, {'error': 'bad request'})
                return self.reply(200, judge(prompt, answer))
            if path == '/api/signup' and method == 'POST':
                if limited('s:' + self.ip(), 5): return self.reply(429, {'error': 'Too many sign-ups, try again soon.'})
                b = self.body(5000); name, pw = str(b.get('name', '')).strip(), str(b.get('password', ''))
                if not USER_RE.match(name): return self.reply(400, {'error': 'Username: 3 to 16 letters, numbers or _'})
                if BAD.search(name): return self.reply(400, {'error': 'Pick a different username.'})
                if len(pw) < 6: return self.reply(400, {'error': 'Password needs at least 6 characters.'})
                if q('SELECT id FROM users WHERE name=?', (name,), one=True): return self.reply(409, {'error': 'That username is taken.'})
                salt = secrets.token_hex(16)
                code = new_code()
                q('INSERT INTO users(name,pw,salt,created,seen,rcode) VALUES(?,?,?,?,?,?)', (name, hash_pw(pw, salt), salt, time.time(), time.time(), code_hash(code)))
                return self.login(name, pw, {'recovery': code})
            if path == '/api/login' and method == 'POST':
                if limited('l:' + self.ip(), 10): return self.reply(429, {'error': 'Too many tries, wait a minute.'})
                b = self.body(5000)
                return self.login(str(b.get('name', '')).strip(), str(b.get('password', '')))
            m = re.match(r'^/api/lobby/([a-z0-9][a-z0-9_.-]{0,47})$', path)
            if m and method == 'POST':
                b = self.body(6000)
                peer = str(b.get('peer', ''))[:40]
                if not peer: return self.reply(400, {'error': 'no peer'})
                pres = b.get('presence') if not b.get('leave') else None
                if pres is not None and len(json.dumps(pres)) > 4096: return self.reply(413, {'error': 'too big'})
                return self.reply(200, {'peers': lobby_sync(m.group(1), peer, pres)})
            if path == '/api/answer' and method == 'POST':
                if limited('a:' + self.ip(), 90): return self.reply(429, {'error': 'slow down'})
                b = self.body(2000)
                pr, an = str(b.get('p', ''))[:160], str(b.get('a', '')).strip().lower()[:60]
                if not pr.startswith('Name ') or not an: return self.reply(400, {'error': 'bad request'})
                q('INSERT INTO answers(prompt,ans,n) VALUES(?,?,1) ON CONFLICT(prompt,ans) DO UPDATE SET n=n+1', (pr, an))
                return self.reply(200, {'ok': True})
            if path == '/api/stats' and method == 'GET':
                from urllib.parse import unquote
                pr = unquote(qs.get('p', ''))[:160]
                rows = q('SELECT ans, n FROM answers WHERE prompt=? ORDER BY n DESC LIMIT 400', (pr,))
                tot = q('SELECT COALESCE(SUM(n),0) t FROM answers WHERE prompt=?', (pr,), one=True)['t']
                return self.reply(200, {'total': tot, 'm': {r['ans']: r['n'] for r in rows}})
            if path == '/api/reset' and method == 'POST':
                if limited('r:' + self.ip(), 6): return self.reply(429, {'error': 'Too many tries, wait a minute.'})
                b = self.body(5000); name, code, pw = str(b.get('name', '')).strip(), str(b.get('code', '')), str(b.get('password', ''))
                u = q('SELECT * FROM users WHERE name=?', (name,), one=True)
                if not u or not u['rcode'] or not hmac.compare_digest(u['rcode'], code_hash(code)):
                    return self.reply(401, {'error': 'That username and recovery code don’t match.'})
                if len(pw) < 6: return self.reply(400, {'error': 'Password needs at least 6 characters.'})
                salt = secrets.token_hex(16); code2 = new_code()
                q('UPDATE users SET pw=?, salt=?, rcode=? WHERE id=?', (hash_pw(pw, salt), salt, code_hash(code2), u['id']))
                q('DELETE FROM sessions WHERE uid=?', (u['id'],))
                return self.login(u['name'], pw, {'recovery': code2})
            u = self.user()
            if not u:
                return self.reply(401, {'error': 'Not signed in.'})
            uid = u['id']
            if path == '/api/logout' and method == 'POST':
                a = self.headers.get('Authorization', '')[7:]
                q('DELETE FROM sessions WHERE token=?', (tok_hash(a),))
                return self.reply(200, {'ok': True})
            if path == '/api/password' and method == 'POST':
                b = self.body(5000); old_pw, pw = str(b.get('old', '')), str(b.get('password', ''))
                if not hmac.compare_digest(u['pw'], hash_pw(old_pw, u['salt'])): return self.reply(403, {'error': 'Your current password is wrong.'})
                if len(pw) < 6: return self.reply(400, {'error': 'Password needs at least 6 characters.'})
                salt = secrets.token_hex(16)
                q('UPDATE users SET pw=?, salt=? WHERE id=?', (hash_pw(pw, salt), salt, uid))
                cur = tok_hash(self.headers.get('Authorization', '')[7:])
                q('DELETE FROM sessions WHERE uid=? AND token<>?', (uid, cur))
                return self.reply(200, {'ok': True})
            if path == '/api/recovery' and method == 'POST':
                b = self.body(5000)
                if not hmac.compare_digest(u['pw'], hash_pw(str(b.get('password', '')), u['salt'])): return self.reply(403, {'error': 'Wrong password.'})
                code = new_code(); q('UPDATE users SET rcode=? WHERE id=?', (code_hash(code), uid))
                return self.reply(200, {'recovery': code})
            if path == '/api/account' and method == 'DELETE':
                b = self.body(5000)
                if not hmac.compare_digest(u['pw'], hash_pw(str(b.get('password', '')), u['salt'])): return self.reply(403, {'error': 'Wrong password.'})
                for sql in ('DELETE FROM sessions WHERE uid=?', 'DELETE FROM scores WHERE uid=?', 'DELETE FROM users WHERE id=?'):
                    q(sql, (uid,))
                q('DELETE FROM friends WHERE a=? OR b=?', (uid, uid)); q('DELETE FROM messages WHERE frm=? OR too=?', (uid, uid))
                return self.reply(200, {'ok': True})
            if path == '/api/board' and method == 'GET':
                kind, day = qs.get('kind', 'xp'), int(qs.get('day', '0') or 0)
                ids = [r['b'] for r in q("SELECT b FROM friends WHERE a=? AND status='ok'", (uid,))] + [uid]
                ph = ','.join('?' * len(ids))
                if kind == 'week':
                    rows = q(f'SELECT u.id, u.name, u.level, COALESCE(SUM(s.depth),0) v, COUNT(s.day) n FROM users u LEFT JOIN scores s ON s.uid=u.id AND s.day>? AND s.day<=? WHERE u.id IN ({ph}) GROUP BY u.id ORDER BY v DESC', (day - 7, day, *ids))
                elif kind == 'best':
                    rows = q(f'SELECT u.id, u.name, u.level, COALESCE(MAX(s.depth),0) v, COUNT(s.day) n FROM users u LEFT JOIN scores s ON s.uid=u.id WHERE u.id IN ({ph}) GROUP BY u.id ORDER BY v DESC', ids)
                elif kind == 'streak':
                    rows = []
                    for i in ids:
                        us = q('SELECT id, name, level FROM users WHERE id=?', (i,), one=True)
                        if not us: continue
                        days = {r['day'] for r in q('SELECT day FROM scores WHERE uid=? AND day>?', (i, day - 400))}
                        d, n = (day if day in days else day - 1), 0
                        while d in days: n += 1; d -= 1
                        rows.append({'id': us['id'], 'name': us['name'], 'level': us['level'], 'v': n, 'n': len(days)})
                    rows.sort(key=lambda r: -r['v'])
                else:
                    rows = q(f'SELECT id, name, level, xp v, 0 n FROM users WHERE id IN ({ph}) ORDER BY xp DESC', ids)
                return self.reply(200, {'rows': [{'name': r['name'], 'level': r['level'], 'v': r['v'], 'n': r['n'], 'me': r['id'] == uid} for r in rows]})
            m = re.match(r'^/api/profile/([A-Za-z0-9_]{3,16})$', path)
            if m and method == 'GET':
                o = q('SELECT * FROM users WHERE name=?', (m.group(1),), one=True)
                if not o: return self.reply(404, {'error': 'No one with that name.'})
                fr = o['id'] == uid or q("SELECT 1 FROM friends WHERE a=? AND b=? AND status='ok'", (uid, o['id']), one=True)
                if not fr: return self.reply(403, {'error': 'Only friends can see that profile.'})
                d = json.loads(o['data'] or '{}'); pg = d.get('prog') or {}; st = d.get('stats') or {}
                codex = sorted((pg.get('codex') or {}).values(), key=lambda c: -(c.get('d') or 0))
                best = q('SELECT MAX(depth) v, COUNT(*) n FROM scores WHERE uid=?', (o['id'],), one=True)
                return self.reply(200, {'name': o['name'], 'level': o['level'], 'xp': o['xp'], 'joined': o['created'], 'seen': o['seen'],
                    'ach': len(pg.get('ach') or {}), 'codex': len(codex), 'finds': [{'n': c.get('n'), 'p': c.get('p'), 't': c.get('t')} for c in codex if c.get('t') in ('abyssal', 'deep')][:8],
                    'answered': (pg.get('n') or {}).get('answers', 0), 'abyssal': (pg.get('n') or {}).get('abyssal', 0),
                    'best': best['v'] or 0, 'dailies': best['n'] or 0, 'streak': st.get('streak', 0), 'endless': st.get('endless', 0), 'pressure': st.get('pressure', 0), 'skin': d.get('skin', '')})
            if path == '/api/me' and method == 'GET':
                return self.reply(200, {'name': u['name'], 'data': json.loads(u['data'] or '{}')})
            if path == '/api/save' and method == 'PUT':
                b = self.body()
                data = b.get('data') or {}
                prog = data.get('prog') or {}
                xp = int(prog.get('xp') or 0) if isinstance(prog, dict) else 0
                q('UPDATE users SET data=?, xp=?, level=? WHERE id=?', (json.dumps(data)[:390_000], xp, int(b.get('level') or 1), uid))
                return self.reply(200, {'ok': True})
            if path == '/api/score' and method == 'POST':
                b = self.body(5000)
                q('INSERT OR REPLACE INTO scores(uid,day,depth,sq,t) VALUES(?,?,?,?,?)',
                  (uid, int(b.get('day', 0)), max(0, min(100000, int(b.get('depth', 0)))), str(b.get('sq', ''))[:80], time.time()))
                return self.reply(200, {'ok': True})
            if path == '/api/leaderboard' and method == 'GET':
                day = int(qs.get('day', '0'))
                fr = [r['b'] for r in q("SELECT b FROM friends WHERE a=? AND status='ok'", (uid,))] + [uid]
                rows = q('SELECT u.name, u.level, s.depth, s.sq, s.uid FROM scores s JOIN users u ON u.id=s.uid WHERE s.day=? ORDER BY s.depth DESC LIMIT 100', (day,))
                allr = [{'name': r['name'], 'level': r['level'], 'depth': r['depth'], 'sq': r['sq'], 'me': r['uid'] == uid, 'friend': r['uid'] in fr} for r in rows]
                return self.reply(200, {'all': allr[:50], 'friends': [r for r in allr if r['friend']]})
            if path == '/api/friends' and method == 'GET':
                today = int(qs.get('day', '-1'))
                out = {'friends': [], 'incoming': [], 'outgoing': []}
                for r in q('SELECT f.status, f.a, f.b, u.name, u.level, u.seen FROM friends f JOIN users u ON u.id=f.b WHERE f.a=?', (uid,)):
                    sc = q('SELECT depth FROM scores WHERE uid=? AND day=?', (r['b'], today), one=True)
                    unread = q('SELECT COUNT(*) c FROM messages WHERE frm=? AND too=? AND read=0', (r['b'], uid), one=True)['c']
                    item = {'name': r['name'], 'level': r['level'], 'online': time.time() - (r['seen'] or 0) < 120, 'today': sc['depth'] if sc else None, 'unread': unread}
                    out['friends' if r['status'] == 'ok' else 'outgoing'].append(item)
                for r in q("SELECT u.name, u.level FROM friends f JOIN users u ON u.id=f.a WHERE f.b=? AND f.status='pending' AND NOT EXISTS (SELECT 1 FROM friends g WHERE g.a=? AND g.b=f.a)", (uid, uid)):
                    out['incoming'].append({'name': r['name'], 'level': r['level']})
                return self.reply(200, out)
            m = re.match(r'^/api/friends/([A-Za-z0-9_]{3,16})$', path)
            if m:
                other = q('SELECT id, name FROM users WHERE name=?', (m.group(1),), one=True)
                if not other or other['id'] == uid: return self.reply(404, {'error': 'No player with that username.'})
                oid = other['id']
                if method == 'POST':   # send or accept a request
                    theirs = q('SELECT status FROM friends WHERE a=? AND b=?', (oid, uid), one=True)
                    if theirs:   # they already asked us: accept
                        q("INSERT OR REPLACE INTO friends(a,b,status,created) VALUES(?,?,'ok',?)", (uid, oid, time.time()))
                        q("UPDATE friends SET status='ok' WHERE a=? AND b=?", (oid, uid))
                        return self.reply(200, {'ok': True, 'status': 'friends'})
                    q("INSERT OR IGNORE INTO friends(a,b,status,created) VALUES(?,?,'pending',?)", (uid, oid, time.time()))
                    return self.reply(200, {'ok': True, 'status': 'requested'})
                if method == 'DELETE':  # remove friend, cancel or decline
                    q('DELETE FROM friends WHERE (a=? AND b=?) OR (a=? AND b=?)', (uid, oid, oid, uid))
                    return self.reply(200, {'ok': True})
            m = re.match(r'^/api/chat/([A-Za-z0-9_]{3,16})$', path)
            if m:
                other = q('SELECT id FROM users WHERE name=?', (m.group(1),), one=True)
                if not other: return self.reply(404, {'error': 'No such player.'})
                oid = other['id']
                ok = q("SELECT 1 FROM friends WHERE a=? AND b=? AND status='ok'", (uid, oid), one=True)
                if not ok: return self.reply(403, {'error': 'You can only chat with friends.'})
                if method == 'POST':
                    if limited('c:' + str(uid), 20): return self.reply(429, {'error': 'Slow down a bit.'})
                    text = clean(str(self.body(4000).get('text', '')).strip())[:300]
                    if not text: return self.reply(400, {'error': 'Empty message.'})
                    q('INSERT INTO messages(frm,too,text,t) VALUES(?,?,?,?)', (uid, oid, text, time.time()))
                    return self.reply(200, {'ok': True})
                after = int(qs.get('after', '0'))
                rows = q('SELECT id, frm, text, t FROM messages WHERE ((frm=? AND too=?) OR (frm=? AND too=?)) AND id>? ORDER BY id DESC LIMIT 80', (uid, oid, oid, uid, after))
                q('UPDATE messages SET read=1 WHERE frm=? AND too=?', (oid, uid))
                return self.reply(200, {'messages': [{'id': r['id'], 'mine': r['frm'] == uid, 'text': r['text'], 't': r['t']} for r in reversed(rows)]})
            return self.reply(404, {'error': 'not found'})
        except urllib.error.HTTPError as e:
            self.reply(502, {'error': f'ai {e.code}'})
        except ValueError as e:
            self.reply(400, {'error': str(e)})
        except Exception as e:
            self.reply(500, {'error': type(e).__name__})

    def login(self, name, pw, extra=None):
        u = q('SELECT * FROM users WHERE name=?', (name,), one=True)
        if not u or not hmac.compare_digest(u['pw'], hash_pw(pw, u['salt'])):
            return self.reply(401, {'error': 'Wrong username or password.'})
        t = secrets.token_urlsafe(32)
        q('INSERT INTO sessions(token,uid,created) VALUES(?,?,?)', (tok_hash(t), u['id'], time.time()))
        return self.reply(200, dict({'token': t, 'name': u['name'], 'data': json.loads(u['data'] or '{}')}, **(extra or {})))

# restart automatically when an update lands (systemd brings us back up)
def watch_updates():
    def head():
        try: return subprocess.check_output(['git', '-C', HERE, 'rev-parse', 'HEAD'], text=True).strip()
        except Exception: return ''
    start = head()
    while True:
        time.sleep(30)
        if head() and head() != start:
            print('update landed, restarting', flush=True)
            os._exit(0)

if __name__ == '__main__' and len(sys.argv) == 4 and sys.argv[1] == 'reset-password':
    # on the Pi:  python3 ~/benthic-online/server.py reset-password USERNAME NEWPASSWORD
    u = q('SELECT id FROM users WHERE name=?', (sys.argv[2],), one=True)
    if not u: sys.exit('No user called ' + sys.argv[2])
    salt = secrets.token_hex(16)
    q('UPDATE users SET pw=?, salt=? WHERE id=?', (hash_pw(sys.argv[3], salt), salt, u['id'])); q('DELETE FROM sessions WHERE uid=?', (u['id'],))
    print('Password reset for', sys.argv[2]); sys.exit(0)
if __name__ == '__main__':
    threading.Thread(target=watch_updates, daemon=True).start()
    print(f'Benthic Online on 127.0.0.1:{PORT} (judge: {"on" if GROQ_KEY else "off"}, model {GROQ_MODEL})', flush=True)
    ThreadingHTTPServer(('127.0.0.1', PORT), H).serve_forever()
