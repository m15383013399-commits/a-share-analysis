"""Local state, task queue and credentials. No research data is stored in git."""
from __future__ import annotations
import contextlib, fcntl, hashlib, hmac, json, os, secrets, sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from market_diary.storage import atomic_write_json

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = Path(os.environ.get('ASHARE_RUNTIME', ROOT / 'runtime')).resolve()
TZ = ZoneInfo('Asia/Shanghai')
TERMINAL = {'succeeded','degraded','failed','cancelled','interrupted','waiting_close','skipped_closed'}

def now(): return datetime.now(TZ).isoformat(timespec='seconds')
def today(): return datetime.now(TZ).date().isoformat()
def digest(data): return hashlib.sha256(json.dumps(data,ensure_ascii=False,sort_keys=True,allow_nan=False).encode()).hexdigest()
def read(path, default=None):
    return json.loads(Path(path).read_text()) if Path(path).exists() else default

def init():
    RUNTIME.mkdir(parents=True, exist_ok=True); RUNTIME.chmod(0o700)
    with lock('initialization'):_init_locked()

def _init_locked():
    for name in ['snapshots','forecasts','evaluations','reports','runs','evidence','supplements','demo']:
        (RUNTIME/name).mkdir(exist_ok=True)
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, stage TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, error TEXT, result TEXT, cancel_requested INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS schedule_runs(day TEXT PRIMARY KEY, task_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS login_attempts(ip TEXT PRIMARY KEY, count INTEGER NOT NULL, until REAL NOT NULL);
        ''')
    if not (RUNTIME/'settings.json').exists():
        atomic_write_json(RUNTIME/'settings.json', {'model_base_url':'https://api.deepseek.com/v1','model_name':'deepseek-chat','api_key':'','max_output_tokens':6500,'max_calls':3,'temperature':0.2,'schedule_enabled':False,'schedule_time':'15:20','watch_codes':['601899','600760','002458','300274','300502'],'gold_api_key':'','gold_provider':'goldapi','max_task_seconds':1200})
        (RUNTIME/'settings.json').chmod(0o600)
    if not (RUNTIME/'auth.json').exists():
        password=secrets.token_urlsafe(15); salt=secrets.token_hex(16)
        atomic_write_json(RUNTIME/'auth.json',{'salt':salt,'hash':password_hash(password,salt)})
        (RUNTIME/'auth.json').chmod(0o600)
        p=RUNTIME/'admin-credentials.txt';p.write_text('账号：admin\n密码：'+password+'\n');p.chmod(0o600)

@contextlib.contextmanager
def db():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    c=sqlite3.connect(RUNTIME/'platform.sqlite',timeout=15)
    c.row_factory=sqlite3.Row;c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA foreign_keys=ON')
    try:
        yield c;c.commit()
    except BaseException:
        c.rollback();raise
    finally:c.close()

@contextlib.contextmanager
def lock(name='ledger',nonblocking=False):
    with (RUNTIME/f'{name}.lock').open('a') as f:
        fcntl.flock(f, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        try:yield
        finally:fcntl.flock(f,fcntl.LOCK_UN)

def password_hash(password,salt):
    return hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),260000).hex()
def authenticate(password):
    a=read(RUNTIME/'auth.json');return hmac.compare_digest(password_hash(password,a['salt']),a['hash'])
def settings(public=False):
    s=read(RUNTIME/'settings.json',{})
    if public:
        for key in ['api_key','gold_api_key']:
            s[key+'_configured']=bool(s.pop(key,''))
    return s

def update_settings(values):
    with lock('settings'):
        s=settings()
        for k,v in values.items():
            if k in s:
                if k in ['api_key','gold_api_key'] and v == '':continue
                s[k]=v
        atomic_write_json(RUNTIME/'settings.json',s);(RUNTIME/'settings.json').chmod(0o600)

def enqueue(kind,payload):
    encoded=json.dumps(payload,sort_keys=True,ensure_ascii=False)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        old=c.execute("SELECT * FROM tasks WHERE kind=? AND payload=? AND status IN ('queued','running')",(kind,encoded)).fetchone()
        if old:return old['id']
        task_id=secrets.token_hex(12)
        c.execute('INSERT INTO tasks(id,kind,payload,status,stage,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',(task_id,kind,encoded,'queued','等待执行',now(),now()))
    return task_id

def unpack(row):
    if row is None:return None
    d=dict(row);d['payload']=json.loads(d['payload']);d['result']=json.loads(d['result']) if d['result'] else None;return d

def task(task_id):
    with db() as c:return unpack(c.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone())
def tasks():
    with db() as c:return [unpack(r) for r in c.execute('SELECT * FROM tasks ORDER BY created_at DESC,rowid DESC LIMIT 60')]
def update(task_id,**fields):
    fields['updated_at']=now()
    if 'result' in fields:fields['result']=json.dumps(fields['result'],ensure_ascii=False)
    assert set(fields)<={'status','stage','error','result','updated_at','cancel_requested'}
    with db() as c:c.execute('UPDATE tasks SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',(*fields.values(),task_id))
def claim():
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute("SELECT * FROM tasks WHERE status='queued' ORDER BY created_at,rowid LIMIT 1").fetchone()
        if not row:return None
        c.execute("UPDATE tasks SET status='running',stage='开始执行',updated_at=? WHERE id=?",(now(),row['id']))
        return unpack(row)
