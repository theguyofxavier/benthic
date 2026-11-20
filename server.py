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
    models = [GROQ_MODEL] + [m for m in ('llama-3.3-70b-versatile', 'llama-3.1-8b-instant', 'openai/gpt-oss-120b') if m != GROQ_MODEL]
    for i, model in enumerate(models):
        body = json.dumps({'model': model, 'temperature': 0, 'max_tokens': 120, 'response_format': {'type': 'json_object'},
                           'messages': [{'role': 'system', 'content': system},
                                        {'role': 'user', 'content': f'Category: "{prompt}"\nPlayer typed: "{answer}"'}]}).encode()
        req = urllib.request.Request('https://api.groq.com/openai/v1/chat/completions', data=body,
                                     headers={'Authorization': f'Bearer {GROQ_KEY}', 'Content-Type': 'application/json', 'User-Agent': 'benthic'})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                out = json.load(r)
            GROQ_MODEL = model
            break
        except urllib.error.HTTPError as e:
            if e.code not in (400, 404) or i == len(models) - 1: raise
    v = json.loads(out['choices'][0]['message']['content'])
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
                return self.reply(200, {'ok': True, 'judge': bool(GROQ_KEY)})
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
                q('INSERT INTO users(name,pw,salt,created,seen) VALUES(?,?,?,?,?)', (name, hash_pw(pw, salt), salt, time.time(), time.time()))
                return self.login(name, pw)
            if path == '/api/login' and method == 'POST':
                if limited('l:' + self.ip(), 10): return self.reply(429, {'error': 'Too many tries, wait a minute.'})
                b = self.body(5000)
                return self.login(str(b.get('name', '')).strip(), str(b.get('password', '')))
            u = self.user()
            if not u:
                return self.reply(401, {'error': 'Not signed in.'})
            uid = u['id']
            if path == '/api/logout' and method == 'POST':
                a = self.headers.get('Authorization', '')[7:]
                q('DELETE FROM sessions WHERE token=?', (tok_hash(a),))
                return self.reply(200, {'ok': True})
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
            m = re.match(r'^/api/lobby/([a-z0-9][a-z0-9_.-]{0,47})$', path)
            if m and method == 'POST':
                b = self.body(6000)
                peer = str(b.get('peer', ''))[:40]
                if not peer: return self.reply(400, {'error': 'no peer'})
                pres = b.get('presence') if not b.get('leave') else None
                if pres is not None and len(json.dumps(pres)) > 4096: return self.reply(413, {'error': 'too big'})
                return self.reply(200, {'peers': lobby_sync(m.group(1), peer, pres)})
            return self.reply(404, {'error': 'not found'})
        except urllib.error.HTTPError as e:
            self.reply(502, {'error': f'ai {e.code}'})
        except ValueError as e:
            self.reply(400, {'error': str(e)})
        except Exception as e:
            self.reply(500, {'error': type(e).__name__})

    def login(self, name, pw):
        u = q('SELECT * FROM users WHERE name=?', (name,), one=True)
        if not u or not hmac.compare_digest(u['pw'], hash_pw(pw, u['salt'])):
            return self.reply(401, {'error': 'Wrong username or password.'})
        t = secrets.token_urlsafe(32)
        q('INSERT INTO sessions(token,uid,created) VALUES(?,?,?)', (tok_hash(t), u['id'], time.time()))
        return self.reply(200, {'token': t, 'name': u['name'], 'data': json.loads(u['data'] or '{}')})

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

if __name__ == '__main__':
    threading.Thread(target=watch_updates, daemon=True).start()
    print(f'Benthic Online on 127.0.0.1:{PORT} (judge: {"on" if GROQ_KEY else "off"}, model {GROQ_MODEL})', flush=True)
    ThreadingHTTPServer(('127.0.0.1', PORT), H).serve_forever()
