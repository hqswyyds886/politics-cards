# -*- coding: utf-8 -*-
"""
考研政治刷题站 · 后端
零第三方依赖：标准库 http.server + sqlite3。
单文件后端 + SQLite，默认端口 8001。
"""
import base64
import datetime as dt
import gzip
import hashlib
import hmac
import json
import os
import random
import re
import secrets
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

try:
    from traffic_guard import guard as traffic_guard
except Exception:
    class _NoGuard:
        def allow(self, ip): return True
        def track(self, n, ip=''): pass
    traffic_guard = _NoGuard()

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, 'data')
DB_PATH = os.path.join(DATA, 'kaoyan.db')
STATIC = os.path.join(BASE, 'static')
PORT = int(os.environ.get('KAOYAN_PORT', '8001'))
SECRET_PATH = os.path.join(DATA, '.secret')

DAILY_NEW = int(os.environ.get('KAOYAN_DAILY_NEW', '20'))      # 每天新学卡片数
DAILY_REVIEW_MAX = int(os.environ.get('KAOYAN_DAILY_REVIEW', '120'))  # 每天复习上限


# ---------------------------------------------------------------- 基础设施
def now_ts() -> int:
    return int(time.time())


def today_str() -> str:
    return dt.date.today().isoformat()


def get_secret() -> bytes:
    os.makedirs(DATA, exist_ok=True)
    if os.path.exists(SECRET_PATH):
        return open(SECRET_PATH, 'rb').read()
    s = secrets.token_bytes(32)
    with open(SECRET_PATH, 'wb') as f:
        f.write(s)
    os.chmod(SECRET_PATH, 0o600)
    return s


def hash_pw(pw: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac('sha256', pw.encode(), bytes.fromhex(salt), 120_000).hex()


def make_token(uid: int) -> str:
    payload = f'{uid}.{now_ts()}'
    sig = hmac.new(get_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(f'{payload}.{sig}'.encode()).decode()


def parse_token(tok: str):
    try:
        raw = base64.urlsafe_b64decode(tok.encode()).decode()
        uid, ts, sig = raw.split('.')
        payload = f'{uid}.{ts}'
        good = hmac.new(get_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, good):
            return None
        if now_ts() - int(ts) > 86400 * 60:
            return None
        return int(uid)
    except Exception:
        return None


# ---------------------------------------------------------------- 数据库
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  nick TEXT UNIQUE NOT NULL,
  pw_hash TEXT NOT NULL,
  salt TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  streak INTEGER DEFAULT 0,
  last_day TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS points(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  subject TEXT NOT NULL,          -- 马原/毛中特/史纲/思修/时政
  chapter TEXT DEFAULT '',
  front TEXT NOT NULL,            -- 卡片正面：问题
  back TEXT NOT NULL,             -- 卡片背面：要点
  tags TEXT DEFAULT '',
  source TEXT DEFAULT '',
  ord INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS atoms(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  subject TEXT,
  chapter TEXT,
  front TEXT,                     -- 卡片正面：问题
  back TEXT,                      -- 卡片背面：要点
  src_point INTEGER,              -- 指回 points.id（由该知识点切分而来）
  ord INTEGER
);
CREATE INDEX IF NOT EXISTS idx_atoms_subject ON atoms(subject, ord);
CREATE TABLE IF NOT EXISTS q_links(
  question_id INTEGER,
  point_id INTEGER,
  score INTEGER,
  PRIMARY KEY(question_id, point_id)
);
CREATE TABLE IF NOT EXISTS user_settings(
  user_id INTEGER PRIMARY KEY,
  daily_new INTEGER DEFAULT 20,
  ts INTEGER
);
CREATE TABLE IF NOT EXISTS reviews(
  user_id INTEGER NOT NULL,
  point_id INTEGER NOT NULL,
  ease REAL DEFAULT 2.5,
  interval REAL DEFAULT 0,
  due INTEGER DEFAULT 0,
  reps INTEGER DEFAULT 0,
  lapses INTEGER DEFAULT 0,
  last_grade INTEGER DEFAULT -1,
  PRIMARY KEY(user_id, point_id)
);
CREATE TABLE IF NOT EXISTS questions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  year INTEGER NOT NULL,
  num INTEGER DEFAULT 0,
  qtype TEXT NOT NULL,            -- single / multi
  subject TEXT DEFAULT '',
  stem TEXT NOT NULL,
  options TEXT NOT NULL,          -- JSON: {"A":"...","B":"..."}
  answer TEXT NOT NULL,           -- "C" 或 "ACD"
  analysis TEXT DEFAULT '',
  source TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS attempts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  question_id INTEGER NOT NULL,
  chosen TEXT NOT NULL,
  correct INTEGER NOT NULL,
  reason TEXT DEFAULT '',          -- base=基础不牢 / trans=看不懂换说法 / match=材料没对应
  ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS atom_reviews(
  user_id INTEGER NOT NULL,
  atom_id INTEGER NOT NULL,
  ease REAL DEFAULT 2.5,
  interval REAL DEFAULT 0,
  due INTEGER DEFAULT 0,
  reps INTEGER DEFAULT 0,
  lapses INTEGER DEFAULT 0,
  last_grade INTEGER DEFAULT -1,
  last_ts INTEGER DEFAULT 0,      -- 最后一次打分时间，用于「当天同一张卡只计一次」
  PRIMARY KEY(user_id, atom_id)
);
CREATE INDEX IF NOT EXISTS idx_arev_due ON atom_reviews(user_id, due);
CREATE TABLE IF NOT EXISTS daily(
  user_id INTEGER NOT NULL,
  day TEXT NOT NULL,
  new_done INTEGER DEFAULT 0,
  review_done INTEGER DEFAULT 0,
  PRIMARY KEY(user_id, day)
);
CREATE TABLE IF NOT EXISTS fitness(
  user_id INTEGER PRIMARY KEY,
  nickname TEXT,
  profile TEXT,
  plan TEXT,
  ts INTEGER
);
CREATE TABLE IF NOT EXISTS fitness_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  exercise TEXT,
  weight REAL,
  reps INTEGER,
  rpe REAL,
  sets INTEGER,
  ts INTEGER
);
CREATE INDEX IF NOT EXISTS idx_flog ON fitness_log(user_id, exercise, ts);
CREATE TABLE IF NOT EXISTS fit_weight(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  d TEXT NOT NULL,
  kg REAL NOT NULL,
  ts INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_fw ON fit_weight(user_id, d);
CREATE TABLE IF NOT EXISTS fit_intake(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  d TEXT NOT NULL,
  kcal REAL NOT NULL,
  protein REAL,
  ts INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_fi ON fit_intake(user_id, d);
CREATE TABLE IF NOT EXISTS dati_wrong(
  user_id INTEGER NOT NULL,
  pid INTEGER NOT NULL,
  sentence TEXT,
  stem TEXT,
  title TEXT,
  chapter TEXT,
  ts INTEGER,
  PRIMARY KEY(user_id, pid, sentence)
);
CREATE TABLE IF NOT EXISTS issues(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  kind TEXT,
  ref_id INTEGER,
  ref_text TEXT,
  note TEXT,
  status TEXT DEFAULT 'open',
  fix TEXT,
  ts INTEGER
);
CREATE INDEX IF NOT EXISTS idx_issue_status ON issues(status, ts);
CREATE TABLE IF NOT EXISTS favorites(
  user_id INTEGER NOT NULL,
  question_id INTEGER NOT NULL,
  ts INTEGER NOT NULL,
  PRIMARY KEY(user_id, question_id)
);
CREATE INDEX IF NOT EXISTS idx_fav ON favorites(user_id, ts);
CREATE INDEX IF NOT EXISTS idx_points_subject ON points(subject, ord);
CREATE INDEX IF NOT EXISTS idx_q_year ON questions(year, id);
CREATE INDEX IF NOT EXISTS idx_rev_due ON reviews(user_id, due);
"""


def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


def ensure_user_cols(conn):
    """老库补列：每个用户自己的每日新卡目标。"""
    cols = {r['name'] for r in conn.execute('PRAGMA table_info(users)')}
    if 'daily_new' not in cols:
        try:
            conn.execute('ALTER TABLE users ADD COLUMN daily_new INTEGER DEFAULT 20')
        except Exception:
            pass


SEED_PATH = os.path.join(BASE, 'seed', 'kaoyan.db')


def maybe_seed():
    """首次启动且库里没有内容时，自动导入 seed/kaoyan.db 的题库。

    只在 points/questions/atoms 三张表全空时才动手，绝不会覆盖已有数据。
    """
    if not os.path.exists(SEED_PATH):
        return False
    conn = db()
    try:
        empty = all(
            conn.execute('SELECT COUNT(*) c FROM %s' % t).fetchone()['c'] == 0
            for t in ('points', 'questions', 'atoms'))
    except sqlite3.OperationalError:
        conn.close()
        return False
    if not empty:
        conn.close()
        return False
    conn.close()

    # 用 SQLite 的 ATTACH 直接搬内容表，避免自己写导入逻辑。
    # 必须显式列出列名：种子库是老库，questions.num / attempts.reason 这些
    # 后加的列在表尾，一旦用 SELECT * 就会整体错位。
    COLS = {
        'atoms': 'id,subject,chapter,front,back,src_point,ord',
        'points': 'id,subject,chapter,front,back,tags,source,ord',
        'questions': 'id,year,qtype,subject,stem,options,answer,analysis,source,num',
        'q_links': 'question_id,point_id,score',
    }
    conn = db()
    try:
        conn.execute('ATTACH DATABASE ? AS seed', (SEED_PATH,))
        for t, cols in COLS.items():
            try:
                conn.execute('INSERT INTO main.%s(%s) SELECT %s FROM seed.%s' % (t, cols, cols, t))
            except sqlite3.OperationalError:
                pass
        conn.commit()
        conn.execute('DETACH DATABASE seed')
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    return True


def init_db():
    os.makedirs(DATA, exist_ok=True)
    conn = db()
    conn.executescript(SCHEMA)
    # 老库平滑补列
    try:
        conn.execute('ALTER TABLE questions ADD COLUMN num INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE attempts ADD COLUMN reason TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute('ALTER TABLE atom_reviews ADD COLUMN last_ts INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()
    if maybe_seed():
        print('[init] 已从 seed/kaoyan.db 导入题库')


# ---------------------------------------------------------------- 复习调度（SM-2 简化版）
def short_back(text: str, n: int = 44) -> str:
    """把要点压成适合做选项的短句：取第一句、去空白、限长。"""
    t = re.sub(r'\s+', ' ', (text or '').strip())
    for sep in ('。', '；', '\n'):
        if sep in t:
            t = t.split(sep)[0]
            break
    t = t.strip(' 、，,。；;')
    return t[:n]


def build_options(conn, point) -> dict:
    """给复习卡生成 4 选 1 的选项（正确答案 + 3 个同科干扰项）。"""
    correct = short_back(point['back'])
    if len(correct) < 6:
        return {}
    rows = conn.execute(
        'SELECT back FROM points WHERE subject=? AND id<>? ORDER BY RANDOM() LIMIT 20',
        (point['subject'], point['id'])).fetchall()
    opts, seen = [correct], {correct}
    for r in rows:
        s = short_back(r['back'])
        if len(s) >= 6 and s not in seen:
            seen.add(s)
            opts.append(s)
        if len(opts) >= 4:
            break
    if len(opts) < 4:
        return {}
    random.shuffle(opts)
    return {'options': opts, 'correct': correct}


STOP_WORDS = {'下列', '关于', '正确', '错误', '选项', '的是', '本题', '下列选项', '表述',
              '说法', '表明', '体现', '说明', '这表', '一说法', '哪一', '以下', '其中'}


def match_points(conn, stem: str, limit: int = 2):
    """从题干里抽关键词，在知识点库里找最相关的条目，用于生成考点解析。"""
    words = set()
    for m in re.finditer(r'[\u4e00-\u9fa5]{3,8}', stem or ''):
        w = m.group()
        if w in STOP_WORDS:
            continue
        words.add(w)
    if not words:
        return []
    rows = conn.execute('SELECT id,subject,chapter,front,back FROM points').fetchall()
    scored = []
    for p in rows:
        text = (p['front'] or '') + ' ' + (p['back'] or '')
        hit = sum(1 for w in words if w in text)
        if hit:
            # 命中词越长越可信
            weight = sum(len(w) for w in words if w in text)
            scored.append((hit * 10 + weight, p))
    scored.sort(key=lambda x: -x[0])
    out = []
    for _, p in scored[:limit]:
        out.append({'id': p['id'], 'subject': p['subject'], 'chapter': p['chapter'],
                    'front': p['front'], 'back': (p['back'] or '')[:320]})
    return out


# 真题干扰项的核心手法：在同一句话上偷换概念
CONCEPT_PAIRS = [
    ('决定', '影响'), ('决定', '取决于'), ('必然', '偶然'), ('绝对', '相对'),
    ('普遍', '特殊'), ('主要', '次要'), ('根本', '一般'), ('唯一', '之一'),
    ('前提', '结果'), ('来源', '动力'), ('目的', '标准'), ('内容', '形式'),
    ('本质', '现象'), ('原因', '结果'), ('肯定', '否定'), ('同一性', '斗争性'),
    ('生产力', '生产关系'), ('经济基础', '上层建筑'), ('实践', '认识'),
    ('物质', '意识'), ('客观', '主观'), ('历史', '逻辑'), ('具体', '抽象'),
    ('一般', '个别'), ('相对', '绝对'), ('指导', '决定'), ('基础', '核心'),
    ('保障', '前提'), ('关键', '基础'), ('核心', '关键'), ('标志', '象征'),
]


def concept_variants(text: str, limit: int = 3):
    """把正确答案里的关键概念换成对立/相近概念，生成形似神错的干扰项。"""
    out = []
    for a, b in CONCEPT_PAIRS:
        if a in text and b not in text:
            v = text.replace(a, b, 1)
            if v != text and v not in out:
                out.append(v)
        if len(out) >= limit:
            break
    return out


def same_label_items(conn, subject: str, label: str, exclude_id: int, limit: int = 10):
    """同标签（如「社会根源」）下、其他知识点的内容——最像真题的干扰项。"""
    if not label or len(label) < 2:
        return []
    key = label.strip('（）()：: ')
    if len(key) < 2:
        return []
    rows = conn.execute(
        'SELECT back FROM points WHERE subject=? AND id<>? ORDER BY RANDOM() LIMIT 30',
        (subject, exclude_id)).fetchall()
    out = []
    for r in rows:
        text = re.sub(r'\n+', '', r['back'] or '')
        for m in re.finditer(r'([^。；]{2,24})[：:]\s*([^。；]{4,60})', text):
            head = m.group(1).strip()
            if key in head or head in key:
                val = m.group(2).strip(' 　、，,')
                if 6 <= len(val) <= 40 and val not in out:
                    out.append(val)
        if len(out) >= limit:
            break
    return out


HALF_HEAD = re.compile(r'^(也|还|并且|既|而|但|又|即|例如|比如|包括|以及|和|与|及|或)')
HALF_TAIL = re.compile(r'(了|在|和|与|及|或|之|其|该|此|等|即|把|被)$')


def is_half_sentence(v: str) -> bool:
    """判断片段是不是半句话（过短 / 半句开头 / 悬空结尾 / 罗列碎片）。"""
    v = (v or '').strip()
    if len(v) < 4:
        return True
    if HALF_HEAD.match(v):
        return True
    if HALF_TAIL.search(v):
        return True
    if v.count('，') >= 2 or v.count('、') >= 3:
        return True
    # 括号严重失衡才算残缺（中英文括号混用很常见，不做严格配对）
    if abs(v.count('（') - v.count('）')) >= 2 or abs(v.count('(') - v.count(')')) >= 2:
        return True
    return False


NUM_HEAD = re.compile(r'^\s*([①②③④⑤⑥⑦⑧⑨⑩])')


def split_phrases(back: str):
    """把要点切成可挖空的片段。并列编号（①②③）合并成一条，不拆散。"""
    raw = back or ''
    # 先收集被标注为「错」的句子，稍后整句排除
    wrong = set()
    for m in re.finditer(r'([^。；\n]{5,100})[。.．]?\s*[（(]\s*错\s*[）)]', raw):
        wrong.add(re.sub(r'\s+', '', m.group(1)).strip('，,。；①-⑳ '))
    text = re.sub(r'\n+', '', raw)
    # 关键：把「①…。②…。③…」中的句号保护起来，避免被当作句子边界切开
    text = re.sub(r'([①②③④⑤⑥⑦⑧⑨⑩][^。；;]{2,120})[。；;](?=\s*[②③④⑤⑥⑦⑧⑨⑩])', r'\1｜', text)
    parts = re.split(r'[。；;]', text)
    out = []
    for p in parts:
        p = re.sub(r'\s+', ' ', p).strip(' 、，,')
        if len(p) < 6:
            continue
        # 命题思路结束 / 下一知识点开始 → 停止采集
        if re.match(r'^[（(]\s*(错|对)\s*[）)]', p):
            break
        if re.search(r'(马克思主义基本原理》|毛泽东思想和中国特色社会主义理论体系概论》|'
                     r'中国近现代史纲要》|思想道德与法治》|第[一二三四五]部分|^第[一二三四五六七八九十]{1,3}章)', p):
            break
        stripped = re.sub(r'\s+', '', p).strip('，,。；①-⑳ ')
        if stripped in wrong or stripped.rstrip('0123456789') in wrong:
            continue
        if re.search(r'[（(]\s*错\s*[）)]', p):
            continue
        if re.search(r'(错误选项|干扰项|命题思路|易错点|选项会|注意不能|注意区分|陷阱)', p):
            continue
        # 并列项：保留 ①②③ 的结构，不再判为半句
        merged = p.replace('｜', '。')
        m = re.match(r'^(.{2,26})[：:]\s*(.{4,90})$', merged)
        if m:
            label = m.group(1).strip()
            body = m.group(2).strip()
            if re.search(r'(部分|续表|核心背诵|考研政治)', label):
                continue
            out.append({'label': label, 'text': body, 'tagged': True})
        elif len(merged) <= 200:
            if is_half_sentence(merged) and not NUM_HEAD.match(merged):
                continue
            out.append({'label': '', 'text': merged, 'tagged': False})
    return out


ENUM_HINT = re.compile(r'(分别是|分别为|包括|分为|主要有|可以概括为|表现为|体现在|即)[：:]?\s*')
ENUM_BAD = re.compile(r'^([0-9０-９①-⑩]|第[一二三四五六七八九十]+)')


def collect_short_items(conn, subject: str, exclude_id: int, limit: int = 60):
    """从同学科其他知识点里收集短词条，作为干扰项。"""
    rows = conn.execute(
        'SELECT back FROM points WHERE subject=? AND id<>? ORDER BY RANDOM() LIMIT 24',
        (subject, exclude_id)).fetchall()
    out = []
    for r in rows:
        text = re.sub(r'\n+', '', r['back'] or '')
        for m in ENUM_HINT.finditer(text):
            tail = re.split(r'[。；]', text[m.end():])[0]
            for x in re.split(r'[、，,]', tail):
                x = x.strip(' 　、，,：:')
                if 2 <= len(x) <= 16 and not ENUM_BAD.match(x) and x not in out:
                    out.append(x)
        if len(out) >= limit:
            break
    return out


def build_enum_fill(conn, point):
    """枚举型挖空：题干一句话 + 一个大空，正确项 2~5 个全选。"""
    text = re.sub(r'\n+', '', point['back'] or '')
    cand = None
    for m in ENUM_HINT.finditer(text):
        s = text.rfind('。', 0, m.start())
        head = text[s + 1:m.start()]
        head = re.sub(r'^[①②③④⑤⑥⑦⑧⑨⑩\s]+', '', head).strip(' 　，,；')
        head = re.sub(r'^(命题思路|注意|例如|错误选项)', '', head).strip(' 　，,；')
        head = re.sub(r'(但|而|并且|因此|所以|但是|然而|同时)$', '', head).strip(' 　，,；')
        if '（错）' in head or '（对）' in head or '(错)' in head or '(对)' in head:
            continue
        if not (8 <= len(head) <= 60):
            continue
        tail = re.split(r'[。；]', text[m.end():])[0]
        if tail.startswith(('“', '”', '"', '（', '(')):
            continue
        items = []
        # 支持 ①②③ 与 顿号 两种枚举写法
        if re.search(r'[①②③④⑤⑥⑦⑧⑨⑩]', tail):
            raw = re.split(r'[①②③④⑤⑥⑦⑧⑨⑩]', tail)
        else:
            raw = re.split(r'[、，,]', tail)
        for x in raw:
            x = x.strip(' 　、，,：:“”"')
            if not (2 <= len(x) <= 16):
                continue
            if ENUM_BAD.match(x):
                continue
            if x.count('“') != x.count('”'):
                continue
            items.append(x)
        if 2 <= len(items) <= 5:
            cand = (head, items)
            break
    if not cand:
        return {}
    head, items = cand

    pool = collect_short_items(conn, point['subject'], point['id'])
    seen = set(items)
    distract = [x for x in pool if x not in seen]
    random.shuffle(distract)
    need = max(1, 4 - len(items))     # 同样固定 4 个选项
    opts = items + distract[:need]
    if len(opts) < len(items) + 2:
        return {}

    order = list(range(len(opts)))
    random.shuffle(order)
    shuffled = [opts[i] for i in order]
    answers = sorted(pos for pos, orig in enumerate(order) if orig < len(items))
    return {'stem': f"【考点】{point['front']}\n\n{head}：＿＿＿＿",
            'options': shuffled, 'answers': answers,
            'n': len(items), 'kind': 'enum'}


def build_fill(conn, point):
    """生成挖空题：题干为原文，空位 1~3 个，选项 4~6 个。"""
    enum_q = build_enum_fill(conn, point)
    if enum_q:
        return enum_q
    all_parts = split_phrases(point['back'])
    tagged = [p for p in all_parts if p['tagged']]
    parts = (tagged if len(tagged) >= 2 else all_parts)[:6]
    if len(parts) < 2:
        return {}

    n = min(random.choice([1, 1, 2, 3]), len(parts))
    if n >= len(parts):          # 至少留一个未挖，给上下文提示
        n = max(1, len(parts) - 1)
    n = min(n, 3)
    picked = random.sample(parts, n)

    lines = []
    for p in parts:
        body = '＿＿＿＿' if p in picked else p['text']
        if p['label']:
            lines.append(f"{p['label']}是{body}")
        else:
            lines.append(('＿＿＿＿' if p in picked else p['text']))
    stem = '。'.join(x for x in lines if x) + '。'
    stem = f"【考点】{point['front']}\n\n{stem}"

    # 干扰项：优先「同句偷换概念」，其次「同标签替换」，最后才用异质内容
    need = max(1, 4 - n)          # 选项总数固定 4 个（考研政治没有五选）
    ans_texts = [p['text'] for p in picked]
    seen = set(ans_texts)
    distract = []

    for t in ans_texts:
        # 同一句只反转一次，避免出现「一般/根本」两个几乎一样的选项
        for v in concept_variants(t, limit=1):
            if v not in seen and 6 <= len(v) <= 44:
                seen.add(v)
                distract.append(v)

    for p in picked:
        if len(distract) >= need + 2:
            break
        for v in same_label_items(conn, point['subject'], p['label'], point['id']):
            if len(distract) >= need + 2:
                break
            if v not in seen and 6 <= len(v) <= 44:
                seen.add(v)
                distract.append(v)

    if len(distract) < need:
        rows = conn.execute(
            'SELECT back FROM points WHERE subject=? AND id<>? ORDER BY RANDOM() LIMIT 16',
            (point['subject'], point['id'])).fetchall()
        bad_head = re.compile(r'^([①②③④⑤⑥⑦⑧⑨⑩]|第[一二三四五六七八九十]+[，,、]|一是|二是|三是|四是|[0-9]+[.、])')
        pool = []
        for r in rows:
            for q in split_phrases(r['back']):
                t = q['text']
                if not q['tagged'] or not (6 <= len(t) <= 34):
                    continue
                if t in seen or bad_head.match(t):
                    continue
                if t.count('，') >= 2 or t.count('、') >= 3:   # 罗列型碎片
                    continue
                # 必须与本题知识点有词汇重叠，避免「世界银行」这类完全无关的干扰
                own = (point.get('back') or '') + (point.get('front') or '')
                if len(set(t) & set(own)) < 1:
                    continue
                seen.add(t)
                pool.append(t)
        random.shuffle(pool)
        distract.extend(pool)

    opts = ans_texts + distract[:need]
    if len(opts) < n + 2:
        return {}

    order = list(range(len(opts)))
    random.shuffle(order)
    shuffled = [opts[i] for i in order]
    answers = sorted(pos for pos, orig in enumerate(order) if orig < n)
    return {'stem': stem, 'options': shuffled, 'answers': answers, 'n': n}


TRAP_RE = re.compile(r'【干扰(?:项|选项)(?:辨析)?】')


FIXED_PATH = os.path.join(DATA, 'fixed_pairs.json')
_FIXED = None


def fixed_pairs():
    global _FIXED
    if _FIXED is None:
        try:
            _FIXED = json.load(open(FIXED_PATH, encoding='utf-8'))
        except Exception:
            _FIXED = []
    return _FIXED


def match_fixed(stem: str, limit: int = 3):
    """只挂精确匹配：题干里同时出现「搭配关键词」和「主题词」才算。"""
    out, seen = [], set()
    for p in fixed_pairs():
        if not p.get('pattern') or p['pattern'] not in stem:
            continue
        if not p.get('topic') or p['topic'] not in stem:
            continue
        key = (p['pattern'], p.get('topic', ''))
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
        if len(out) >= limit:
            break
    return out


def split_analysis(text: str):
    """把原版解析拆成「正文 + 逐项判断」，供前端上色。"""
    text = (text or '').strip()
    m = TRAP_RE.search(text)
    main = text[:m.start()].strip() if m else text
    wrong = text[m.end():].strip() if m else ''
    main = re.sub(r'^【解析】', '', main).strip()

    items = []
    if wrong:
        parts = re.split(r'(?=(?:选项\s*[A-D]|[A-D]\s*选项|[A-D]\s*[错误正确]))', wrong)
        for p in parts:
            p = p.strip()
            if len(p) < 6:
                continue
            lm = re.match(r'(?:选项\s*)?([A-D])', p)
            letter = lm.group(1) if lm else ''
            verdict = 'right' if re.search(r'(正确|符合题意|当选)', p[:26]) else 'wrong'
            items.append({'letter': letter, 'text': p[:300], 'verdict': verdict})
    return {'main': main[:900], 'items': items[:8]}


def shuffle_question(q):
    """打乱选项顺序，并同步映射正确答案。返回 perm：新位置 i 对应的原字母。"""
    try:
        opts = q['options'] if isinstance(q['options'], dict) else json.loads(q['options'])
    except Exception:
        return q
    letters = sorted(opts.keys())
    if len(letters) < 2:
        return q
    perm = letters[:]
    random.shuffle(perm)
    new_opts = {letters[i]: opts[perm[i]] for i in range(len(letters))}
    ans = set((q.get('answer') or '').upper())
    new_ans = ''.join(sorted(letters[i] for i in range(len(letters)) if perm[i] in ans))
    out = dict(q)
    out['options'] = new_opts
    out['answer'] = new_ans
    out['perm'] = perm
    return out


def too_similar(a: str, b: str) -> bool:
    """两个选项是否几乎一模一样（只差一两个字）。"""
    if abs(len(a) - len(b)) > 6:
        return False
    same = sum(1 for x, y in zip(a, b) if x == y)
    return same / max(len(a), len(b), 1) > 0.92


def fill_ok(fill, point) -> bool:
    """挖空题质量闸门：不合格就不出挖空，退回翻卡模式。"""
    if not fill:
        return False
    stem = fill.get('stem', '')
    # 去掉「【考点】…」抬头后再检查正文
    body = stem.split('\n\n', 1)[-1] if '\n\n' in stem else stem
    if re.match(r'^(部分|第[一二三四五]部分|①|②|③|④|⑤|续表|★|（错）|\(错\))', body):
        return False
    if len(body) < 15:
        return False
    head = body.split('＿＿＿＿')[0]
    if len(head) < 6:
        return False
    # 题干不能以半句连词结尾
    if re.search(r'(既|也|还|并且|即|而|但)$', head.strip()):
        return False
    # 选项质量
    opts = fill.get('options') or []
    ans = fill.get('answers') or []
    if len(opts) != 4 or not ans:
        return False
    for o in opts:
        if is_half_sentence(o):
            return False
    # 任意两个选项几乎一样 → 题目无解，判不合格
    for i in range(len(opts)):
        for j in range(i + 1, len(opts)):
            if too_similar(opts[i], opts[j]):
                return False
    for i in ans:
        if i >= len(opts) or len(opts[i]) < 3:
            return False
    # 正确答案不能是半句
    for i in ans:
        if i < len(opts) and re.match(r'^(也|还|并且|既|而|但|又|即)', opts[i]):
            return False
    # 至少一个干扰项与知识点有较强关联
    own = (point.get('back') or '') + (point.get('front') or '')
    correct = {opts[i] for i in ans if i < len(opts)}
    related = sum(1 for o in opts if o not in correct and len(set(o) & set(own)) >= 3)
    return related >= 1


def make_distractors(conn, answer: str, subject: str, exclude_id: int, need: int = 3):
    """按命题学优先级造干扰项：概念偷换 > 绝对化 > 时态混淆 > 无关项。"""
    out, seen = [], {answer}

    def add(v):
        v = (v or '').strip()
        if not v or v in seen or len(v) < 4 or len(v) > 60:
            return
        if too_similar(v, answer) or is_half_sentence(v):
            return
        seen.add(v)
        out.append(v)

    # ① 概念偷换（最高频）：根本/基本、主要/次要、一般/个别…
    for v in concept_variants(answer, limit=2):
        add(v)

    # ② 时态混淆（只在答案本身含时态词时用）
    if len(out) < need:
        for a, b in (('正在', '已经'), ('已经', '将要'), ('目标', '现实')):
            if a in answer:
                add(answer.replace(a, b, 1))
            if len(out) >= need:
                break

    # ④ 无关项兜底（同科其他原子卡）
    if len(out) < need:
        rows = conn.execute(
            'SELECT back FROM atoms WHERE subject=? AND id<>? ORDER BY RANDOM() LIMIT 20',
            (subject, exclude_id)).fetchall()
        for r in rows:
            add((r['back'] or '').strip())
            if len(out) >= need:
                break
    return out[:need]


JUDGE_RE = re.compile(r'([^。；\n]{10,70})[。.]?\s*[（(]\s*(错|对)\s*[）)]')


def extract_judgements(text: str, limit: int = 5):
    """从知识点正文里抽出「（错）/（对）」例句，作为易错判断。"""
    out = []
    for m in JUDGE_RE.finditer(text or ''):
        s = re.sub(r'\s+', '', m.group(1)).strip('，,。；：')
        if len(s) < 8:
            continue
        out.append({'text': s, 'ok': m.group(2) == '对'})
        if len(out) >= limit:
            break
    return out


def build_atom_quiz(conn, card):
    """原子卡 → 四选一：题干=问题，正确项=答案，干扰项按命题学优先级生成。"""
    front = (card.get('front') or '').strip()
    back = (card.get('back') or '').strip()
    if not front or not back:
        return {}
    stem = re.sub(r'是什么[？?]$', '是＿＿＿＿', front)
    if stem == front:
        stem = front.rstrip('？?') + '：＿＿＿＿'

    distract = make_distractors(conn, back, card.get('subject', ''), card.get('id', 0), 3)
    if len(distract) < 1:
        return {}
    opts = [back] + distract
    order = list(range(len(opts)))
    random.shuffle(order)
    shuffled = [opts[i] for i in order]
    answers = sorted(pos for pos, orig in enumerate(order) if orig == 0)
    return {'fill': {'stem': '【考点】' + front + '\n\n' + stem,
                     'options': shuffled, 'answers': answers, 'n': 1}}


TTS_DIR = os.path.join(DATA, 'tts')
VOICE = 'zh-CN-YunyangNeural'      # 专业播音男声，读知识点最像真人
VOICES = {
    'yunyang': 'zh-CN-YunyangNeural',      # 男 · 专业播音（默认）
    'xiaoxiao': 'zh-CN-XiaoxiaoNeural',    # 女 · 温暖自然
    'yunxi': 'zh-CN-YunxiNeural',          # 男 · 阳光活泼
    'yunjian': 'zh-CN-YunjianNeural',      # 男 · 浑厚有激情
    'xiaoyi': 'zh-CN-XiaoyiNeural',        # 女 · 轻快
    'yunxia': 'zh-CN-YunxiaNeural',        # 男 · 少年感
    'xiaobei': 'zh-CN-liaoning-XiaobeiNeural',   # 女 · 东北话
    'xiaoni': 'zh-CN-shaanxi-XiaoniNeural',      # 女 · 陕西话
}

# 可选：填了 Key 就自动切到更高音质的云服务（不填则用 edge-tts）
TTS_PROVIDER = os.environ.get('TTS_PROVIDER', 'edge')   # edge | azure | xfyun
AZURE_KEY = os.environ.get('AZURE_TTS_KEY', '')
AZURE_REGION = os.environ.get('AZURE_TTS_REGION', 'eastasia')
XF_APPID = os.environ.get('XF_APPID', '')
XF_APIKEY = os.environ.get('XF_APIKEY', '')
XF_APISECRET = os.environ.get('XF_APISECRET', '')


def tts_xfyun(text: str, voice_name: str, rate: str) -> bytes:
    """讯飞在线语音合成 WebAPI（免费额度每天 500 次，音色自然）。"""
    import asyncio
    import base64
    import hashlib
    import hmac
    import json
    import ssl
    from datetime import datetime
    from urllib.parse import quote
    import websockets

    host = 'tts-api.xfyun.cn'
    path = '/v2/tts'
    date = datetime.utcnow().strftime('%a, %d %b %Y %H:%M:%S GMT')
    origin = f'host: {host}\ndate: {date}\nGET {path} HTTP/1.1'
    sig = base64.b64encode(
        hmac.new(XF_APISECRET.encode(), origin.encode(), hashlib.sha256).digest()).decode()
    auth_origin = (f'api_key="{XF_APIKEY}", algorithm="hmac-sha256", '
                   f'headers="host date request-line", signature="{sig}"')
    authorization = base64.b64encode(auth_origin.encode()).decode()
    url = f'wss://{host}{path}?authorization={authorization}&date={quote(date)}&host={host}'

    # edge 的音色名 → 讯飞 vcn（优先用 x4/x5 大模型音色，最自然）
    vcn_map = {
        'zh-CN-YunyangNeural': 'x5_lingfeiyi_flow',      # 男 · 沉稳播报
        'zh-CN-XiaoxiaoNeural': 'x5_lingxiaoxuan_flow',  # 女 · 温暖口语
        'zh-CN-YunxiNeural': 'x5_lingyuyan_flow',        # 女 · 年轻自然
        'zh-CN-YunjianNeural': 'x4_lingbosong_oral',     # 男 · 浑厚
        'zh-CN-XiaoyiNeural': 'x4_lingxiaowan_oral',     # 女 · 轻快
        'zh-CN-YunxiaNeural': 'x5_lingyuzhao_flow',      # 女 · 清亮
        'zh-CN-liaoning-XiaobeiNeural': 'x4_lingxiaoshan_oral',
        'zh-CN-shaanxi-XiaoniNeural': 'x4_yezi',
    }
    vcn = vcn_map.get(voice_name, 'x5_lingxiaoxuan_flow')
    speed = 50 + int(str(rate).replace('%', '').replace('+', '') or 0) // 2
    speed = max(0, min(100, speed))

    payload = {
        'common': {'app_id': XF_APPID},
        'business': {'aue': 'lame', 'sfl': 1, 'auf': 'audio/L16;rate=16000',
                     'vcn': vcn, 'tte': 'UTF8', 'speed': speed},
        'data': {'status': 2, 'text': base64.b64encode(text.encode()).decode()},
    }

    async def run():
        chunks = []
        async with websockets.connect(url, ssl=ssl.create_default_context()) as ws:
            await ws.send(json.dumps(payload))
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=20)
                d = json.loads(msg)
                if d.get('code') != 0:
                    raise RuntimeError(d.get('message', 'xfyun error'))
                audio = (d.get('data') or {}).get('audio')
                if audio:
                    chunks.append(base64.b64decode(audio))
                if (d.get('data') or {}).get('status') == 2:
                    break
        return b''.join(chunks)

    return asyncio.run(run())


def tts_azure(text: str, voice_name: str, rate: str) -> bytes:
    """Azure 官方 TTS（需 Key）。音质比 edge 更自然，支持更多情感风格。"""
    import urllib.request
    ssml = (f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="zh-CN">'
            f'<voice name="{voice_name}"><prosody rate="{rate}">{text}</prosody></voice></speak>')
    req = urllib.request.Request(
        f'https://{AZURE_REGION}.tts.speech.microsoft.com/cognitiveservices/v1',
        data=ssml.encode('utf-8'),
        headers={
            'Ocp-Apim-Subscription-Key': AZURE_KEY,
            'Content-Type': 'application/ssml+xml',
            'X-Microsoft-OutputFormat': 'audio-24khz-48kbitrate-mono-mp3',
            'User-Agent': 'kaoyan-words',
        })
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def tts_get(text: str, voice: str = '', rate: str = '+0%') -> bytes:
    """生成中文语音，按 文本+音色+语速 哈希缓存。"""
    import asyncio
    import hashlib
    os.makedirs(TTS_DIR, exist_ok=True)
    vname = VOICES.get(voice, VOICE)
    provider = TTS_PROVIDER
    if provider == 'azure' and not AZURE_KEY:
        provider = 'edge'
    if provider == 'xfyun' and not (XF_APPID and XF_APIKEY and XF_APISECRET):
        provider = 'edge'
    key = hashlib.md5((provider + '|' + vname + '|' + rate + '|' + text).encode('utf-8')).hexdigest()
    path = os.path.join(TTS_DIR, key + '.mp3')
    if os.path.exists(path) and os.path.getsize(path) > 500:
        return open(path, 'rb').read()

    data = b''
    if provider == 'azure':
        try:
            data = tts_azure(text, vname, rate)
        except Exception:
            data = b''
    elif provider == 'xfyun':
        try:
            data = tts_xfyun(text, vname, rate)
        except Exception:
            data = b''
    if not data:
        try:
            import edge_tts
        except ImportError:
            return b''

        async def gen():
            c = edge_tts.Communicate(text, vname, rate=rate)
            await c.save(path)

        try:
            asyncio.run(gen())
        except Exception:
            return b''
        if os.path.exists(path):
            return open(path, 'rb').read()
        return b''
    if data:
        open(path, 'wb').write(data)
    return data


DEEPSEEK_KEY = os.environ.get('DEEPSEEK_KEY', '')
DEEPSEEK_URL = 'https://api.deepseek.com/chat/completions'


def ai_fix_card(front: str, back: str, note: str) -> dict:
    """调用大模型判断并修正一张知识点卡。返回 {action, correct, reason}。

    关键约束：短答案（简答题关键词）禁止被扩写。
    """
    import urllib.request
    if not DEEPSEEK_KEY:
        return {'action': 'none', 'reason': 'AI 未配置'}

    back_s = (back or '').strip()
    # 短答案保护：4-20 字且不含句号 → 视为完整简答
    short_ok = 4 <= len(back_s) <= 20 and '。' not in back_s
    guard = ''
    if short_ok:
        guard = f'''
【硬性约束】这条答案只有 {len(back_s)} 个字，是**完整且正确**的简答关键词
（如"人民立场""客观实在性""对立统一规律"）。
考研政治简答题答对关键词即可，**不需要也不允许扩写成定义句**。
除非用户明确指出了具体的事实错误（年份、人名、方向、"无限/有限"这类），
否则必须返回 action=ok。绝不允许因为"表述不够完整"就扩写。'''

    prompt = f"""你是考研政治阅卷老师。用户刷到下面这张卡时提出了疑问。

【题目】{front[:200]}
【当前答案】{back[:500]}
【用户说明】{note[:300]}
{guard}

判断这张卡的答案**是否有事实性错误**。严格返回 JSON：
{{"action":"fix" 或 "ok" 或 "drop","correct":"修正后的答案","reason":"一句话说明"}}

从严原则 —— 只有下列情况才 action=fix：
· 与教材相悖（"无限"写成"有限"、"决定"写成"被决定"、年份/人名错）
· 答案与题目完全不对应（答非所问）
· 句子残缺到读不通（OCR 串行，如"劳动资料生产资料基本要素劳动对象"）
· 把书里标注为错的示例当成了答案

下列情况必须 action=ok：
· 答案本身是正确的简答（哪怕只有几个字）
· 只是"可以更完整""表述更规范"
· 用户没说出具体错在哪

correct：直接可用的答案，不加任何前缀。ok/drop 时填空串。"""

    body = json.dumps({
        'model': 'deepseek-chat',
        'messages': [{'role': 'user', 'content': prompt}],
        'temperature': 0,
        'max_tokens': 900,
    }).encode()
    req = urllib.request.Request(DEEPSEEK_URL, data=body, headers={
        'Authorization': 'Bearer ' + DEEPSEEK_KEY,
        'Content-Type': 'application/json',
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode())
        txt = d['choices'][0]['message']['content'].strip()
        txt = re.sub(r'^```(?:json)?|```$', '', txt, flags=re.M).strip()
        m = re.search(r'\{[\s\S]*\}', txt)
        if m:
            res = json.loads(m.group(0))
            # 代码级保险：短答案被要求扩写 → 一律拒绝
            corr = (res.get('correct') or '').strip()
            if res.get('action') == 'fix' and short_ok and corr:
                if len(corr) > len(back_s) * 1.8 or corr != back_s:
                    if not re.search(r'(有[限无限]|决定.*被决定|\d{4}年)', note or ''):
                        return {'action': 'ok',
                                'reason': '原答案「%s」是完整的简答关键词，无需扩写' % back_s}
            return res
    except Exception as e:
        return {'action': 'none', 'reason': f'{type(e).__name__}: {e}'[:120]}
    return {'action': 'none', 'reason': '解析失败'}
    body = json.dumps({
        'model': 'deepseek-chat',
        'messages': [{'role': 'user', 'content': prompt}],
        'temperature': 0.2,
        'max_tokens': 900,
    }).encode()
    req = urllib.request.Request(DEEPSEEK_URL, data=body, headers={
        'Authorization': 'Bearer ' + DEEPSEEK_KEY,
        'Content-Type': 'application/json',
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode())
        txt = d['choices'][0]['message']['content'].strip()
        txt = re.sub(r'^```(?:json)?|```$', '', txt, flags=re.M).strip()
        m = re.search(r'\{[\s\S]*\}', txt)
        if m:
            return json.loads(m.group(0))
    except Exception as e:
        return {'action': 'none', 'reason': f'{type(e).__name__}: {e}'[:120]}
    return {'action': 'none', 'reason': '解析失败'}


def split_sentences(text: str):
    """把模板切成交替可挖的句子（按句号/分号/问号切，去掉过短的）。"""
    if not text:
        return []
    # 先按标点切，保留标点
    raw = re.split(r'(?<=[。！？；])', text)
    out = []
    for s in raw:
        s = re.sub(r'\s+', '', s).strip()
        if len(s) >= 14:
            out.append(s)
    return out


def build_dati_quiz(conn, subject: str, limit: int = 40):
    """大题模板 → 交替挖空题：每次挖掉一句，其余句子作为上下文。"""
    rows = conn.execute(
        'SELECT id,front,chapter,back FROM points WHERE subject=? ORDER BY ord', (subject,)).fetchall()
    if not rows:
        return []

    # 全量句子池（做干扰项）
    pool = []
    for r in rows:
        for s in split_sentences(r['back']):
            pool.append(s)

    BLANK = '＿＿＿＿＿＿＿＿'

    def blank_out(back: str, target: str):
        """在原文里找到 target（忽略空白/标点差异），把它替换成空位。"""
        full = back or ''
        # ① 直接匹配
        i = full.find(target)
        if i >= 0:
            return full[:i] + BLANK + full[i + len(target):]
        # ② 去标点后匹配：找出 target 去标点后在原文里的位置
        core = re.sub(r'[\s。；;，,！？、]', '', target)
        if len(core) < 8:
            return None
        # 逐字符扫描原文，构造去标点版本及其到原文的索引
        idx = []
        buf = []
        for pos, ch in enumerate(full):
            if not re.match(r'[\s。；;，,！？、]', ch):
                buf.append(ch)
                idx.append(pos)
        flat = ''.join(buf)
        j = flat.find(core)
        if j < 0:
            # ③ 用前 20 字再试
            head = core[:20]
            j = flat.find(head)
            if j < 0:
                return None
            end_flat = j + len(head)
        else:
            end_flat = j + len(core)
        start = idx[j]
        end = idx[end_flat - 1] + 1
        return full[:start] + BLANK + full[end:]

    quizzes = []
    for r in rows:
        sents = split_sentences(r['back'])
        if len(sents) < 2:
            continue
        title = re.sub(r'^大题｜', '', r['front'] or '')
        for target in sents:
            if len(target) < 16:
                continue
            stem = blank_out(r['back'] or '', target)
            if not stem or BLANK not in stem:
                continue          # 挖不出来就跳过，绝不放出没挖空的题

            # 干扰项：长度相近的其他句子
            cands = [s for s in pool
                     if s != target and abs(len(s) - len(target)) <= max(10, len(target) // 3)]
            random.shuffle(cands)
            opts = [target]
            seen = {target}
            for c in cands:
                if c in seen:
                    continue
                seen.add(c)
                opts.append(c)
                if len(opts) >= 4:
                    break
            if len(opts) < 4:
                for c in pool:
                    if c not in seen and len(c) >= 12:
                        seen.add(c)
                        opts.append(c)
                    if len(opts) >= 4:
                        break
            if len(opts) < 3:
                continue

            order = list(range(len(opts)))
            random.shuffle(order)
            shuffled = [opts[i] for i in order]
            answers = sorted(pos for pos, orig in enumerate(order) if orig == 0)
            quizzes.append({
                'pid': r['id'], 'chapter': r['chapter'] or '', 'title': title,
                'stem': stem, 'options': shuffled, 'answers': answers,
                'answer_text': target, 'full': r['back'] or '',
            })
    random.shuffle(quizzes)
    return quizzes[:limit]


def schedule(grade: int, ease: float, interval: float):
    """grade: 0=忘了 1=模糊 2=记得。返回 (新ease, 新interval天数)。"""
    ease = max(1.3, min(3.0, ease))
    if grade == 0:
        return max(1.3, ease * 0.80), 0.0           # 立即排回待复习
    if grade == 1:
        new_i = max(1.0, interval * 1.2)
        return ease, min(new_i, 120)
    new_i = 1.0 if interval < 1 else interval * ease
    return min(3.0, ease + 0.05), min(new_i, 180)


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = 'KaoyanWords/1.0'

    # ---- 工具
    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode('utf-8')

        # 流量护栏：被封时只放行极小的错误响应
        ip = self._client_ip()
        if not traffic_guard.allow(ip):
            msg = json.dumps({'ok': False, 'error': '今日流量已达上限，明天自动恢复'},
                             ensure_ascii=False).encode()
            self.send_response(429)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            traffic_guard.track(len(msg), ip)
            return

        accept = (self.headers.get('Accept-Encoding') or '').lower()
        use_gzip = ('gzip' in accept and 'audio' not in ctype
                    and len(body) > 800)
        if use_gzip:
            body = gzip.compress(body, 6)

        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        if use_gzip:
            self.send_header('Content-Encoding', 'gzip')
            self.send_header('Vary', 'Accept-Encoding')
        if 'audio' in ctype:
            # 音频允许浏览器缓存 1 天，避免重复下载
            self.send_header('Cache-Control', 'public, max-age=86400')
        else:
            self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)
        traffic_guard.track(len(body), ip)

    def _client_ip(self):
        fwd = self.headers.get('X-Forwarded-For') or ''
        if fwd:
            return fwd.split(',')[0].strip()
        return self.client_address[0] if self.client_address else ''

    def _json(self):
        n = int(self.headers.get('Content-Length') or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode('utf-8'))
        except Exception:
            return {}

    def _uid(self):
        auth = self.headers.get('Authorization') or ''
        if auth.startswith('Bearer '):
            return parse_token(auth[7:])
        return None

    def log_message(self, fmt, *args):
        pass  # 静默，避免刷日志

    # ---- 路由
    def do_GET(self):
        u = urlparse(self.path)
        p, qs = u.path, parse_qs(u.query)
        try:
            if p == '/' or p == '/index.html':
                return self._static('index.html')
            if p.startswith('/static/'):
                return self._static(p[8:])
            if p == '/api/meta':
                return self._meta()
            if p == '/api/settings':
                return self._settings()
            if p == '/api/today':
                return self._today(qs)
            if p == '/api/learned':
                return self._learned(qs)
            if p == '/api/points':
                return self._points(qs)
            if p == '/api/quiz':
                return self._quiz(qs)
            if p == '/api/questions':
                return self._questions(qs)
            if p == '/api/stats':
                return self._stats()
            if p == '/api/curve':
                return self._curve()
            if p == '/api/tts':
                return self._tts(qs)
            if p == '/api/point_questions':
                return self._point_questions(qs)
            if p == '/api/point':
                return self._point_get(qs)
            if p == '/api/wrong':
                return self._wrong()
            if p == '/api/dati':
                return self._dati(qs)
            if p == '/api/dati_wrong':
                return self._dati_wrong()
            if p == '/api/favorites':
                return self._favorites()
            if p == '/api/issues':
                return self._issues(qs)
            return self._send(404, {'ok': False, 'error': 'not found'})
        except Exception as e:
            return self._send(500, {'ok': False, 'error': f'{type(e).__name__}: {e}'})

    def do_POST(self):
        u = urlparse(self.path)
        try:
            if u.path == '/api/register':
                return self._register()
            if u.path == '/api/login':
                return self._login()
            if u.path == '/api/review':
                return self._review()
            if u.path == '/api/answer':
                return self._answer()
            if u.path == '/api/reason':
                return self._reason()
            if u.path == '/api/fav':
                return self._fav()
            if u.path == '/api/issue':
                return self._issue_post()
            if u.path == '/api/dati_wrong':
                return self._dati_wrong_post()
            if u.path == '/api/dati_edit':
                return self._dati_edit()
            if u.path == '/api/dev':
                return self._dev()
            if u.path == '/api/fitness':
                return self._fitness()
            if u.path == '/api/settings':
                return self._settings()
            return self._send(404, {'ok': False, 'error': 'not found'})
        except Exception as e:
            return self._send(500, {'ok': False, 'error': f'{type(e).__name__}: {e}'})

    def _static(self, name):
        path = os.path.join(STATIC, name)
        if not os.path.abspath(path).startswith(os.path.abspath(STATIC)):
            return self._send(403, 'forbidden', 'text/plain')
        if not os.path.exists(path):
            return self._send(404, 'not found', 'text/plain')
        ctype = 'text/html; charset=utf-8'
        if name.endswith('.css'):
            ctype = 'text/css; charset=utf-8'
        elif name.endswith('.js'):
            ctype = 'application/javascript; charset=utf-8'
        elif name.endswith('.svg'):
            ctype = 'image/svg+xml'
        elif name.endswith('.png'):
            ctype = 'image/png'
        return self._send(200, open(path, 'rb').read(), ctype)

    # ---- 业务
    def _fitness(self):
        """健身教练：一个账号一份档案。"""
        uid = self._uid()
        if not uid:
            return self._send(401, {'ok': False, 'error': '请先登录'})
        d = self._json()
        op = (d.get('op') or 'gen').strip()
        import sys as _s
        _s.path.insert(0, BASE)
        try:
            import fitness_agent as fa
        except Exception as e:
            return self._send(500, {'ok': False, 'error': '模块加载失败 %s' % e})
        try:
            import nutrition_engine as ne
        except Exception as e:
            return self._send(500, {'ok': False, 'error': '饮食模块加载失败 %s' % e})

        conn = db()

        # 读自己的档案
        if op == 'get':
            r = conn.execute('SELECT nickname,profile,plan,ts FROM fitness WHERE user_id=?',
                             (uid,)).fetchone()
            conn.close()
            if not r:
                return self._send(200, {'ok': True, 'exists': False})
            return self._send(200, {'ok': True, 'exists': True,
                                    'nickname': r['nickname'],
                                    'profile': json.loads(r['profile'] or '{}'),
                                    'plan': json.loads(r['plan'] or 'null')})

        if op == 'log':
            ex = (d.get('exercise') or '').strip()[:60]
            if not ex:
                conn.close()
                return self._send(400, {'ok': False, 'error': '动作名必填'})
            conn.execute(
                'INSERT INTO fitness_log(user_id,exercise,weight,reps,rpe,sets,ts) '
                'VALUES(?,?,?,?,?,?,?)',
                (uid, ex, float(d.get('weight') or 0), int(d.get('reps') or 0),
                 float(d.get('rpe') or 0), int(d.get('sets') or 1), now_ts()))
            conn.commit()
            conn.close()
            return self._send(200, {'ok': True})

        if op == 'ratchet':
            ex = (d.get('exercise') or '').strip()[:60]
            target_reps = int(d.get('target_reps') or 8)
            category = (d.get('category') or 'lower').strip()
            rows = conn.execute(
                'SELECT weight,reps,rpe,ts FROM fitness_log WHERE user_id=? AND exercise=? '
                'ORDER BY ts DESC LIMIT 30', (uid, ex)).fetchall()
            conn.close()
            if not rows:
                return self._send(200, {'ok': True, 'found': False,
                                        'msg': '还没有这个动作的记录'})
            hist = [dict(r) for r in rows]
            nl = fa.next_load(hist, target_reps, category)
            st = fa.stall_check(hist, target_reps)
            return self._send(200, {
                'ok': True, 'found': True, 'exercise': ex,
                'heaviest': nl.get('floor'),
                'e1rm': nl.get('e1rm'),
                'action': nl.get('action'),          # advance / hold / calibrate
                'next_load': nl.get('load'),
                'reason': nl.get('reason'),
                'stalled': st.get('stalled'),
                'stall_reason': st.get('reason'),
                'suggest_load': st.get('suggest_load'),
                'last': {'weight': hist[0].get('weight'), 'reps': hist[0].get('reps'),
                         'rpe': hist[0].get('rpe')},
                'advice': nl.get('reason'),
            })

        if op == 'weigh':
            # 记录体重（一天一条，重复则覆盖）
            day = (d.get('d') or __import__('datetime').date.today().isoformat())[:10]
            kg = float(d.get('kg') or 0)
            if kg < 25 or kg > 300:
                conn.close()
                return self._send(400, {'ok': False, 'error': '体重数值不合理'})
            conn.execute(
                '''INSERT INTO fit_weight(user_id,d,kg,ts) VALUES(?,?,?,?)
                   ON CONFLICT(user_id,d) DO UPDATE SET kg=excluded.kg, ts=excluded.ts''',
                (uid, day, kg, now_ts()))
            conn.commit()
            conn.close()
            return self._send(200, {'ok': True, 'd': day, 'kg': kg})

        if op == 'eat':
            # 记录当天摄入
            day = (d.get('d') or __import__('datetime').date.today().isoformat())[:10]
            kcal = float(d.get('kcal') or 0)
            if kcal <= 0 or kcal > 8000:
                conn.close()
                return self._send(400, {'ok': False, 'error': '热量数值不合理'})
            conn.execute(
                '''INSERT INTO fit_intake(user_id,d,kcal,protein,ts) VALUES(?,?,?,?,?)
                   ON CONFLICT(user_id,d) DO UPDATE SET
                     kcal=excluded.kcal, protein=excluded.protein, ts=excluded.ts''',
                (uid, day, kcal, float(d.get('protein') or 0), now_ts()))
            conn.commit()
            conn.close()
            return self._send(200, {'ok': True, 'd': day, 'kcal': kcal})

        if op == 'nutrition':
            # 完整饮食看板
            wrows = conn.execute(
                'SELECT d,kg FROM fit_weight WHERE user_id=? ORDER BY d', (uid,)).fetchall()
            irows = conn.execute(
                'SELECT d,kcal,protein FROM fit_intake WHERE user_id=? ORDER BY d', (uid,)).fetchall()
            fr = conn.execute('SELECT profile FROM fitness WHERE user_id=?', (uid,)).fetchone()
            conn.close()
            if not fr:
                return self._send(200, {'ok': True, 'exists': False})
            prof = json.loads(fr['profile'] or '{}')
            weeks = int(d.get('deficit_weeks') or 0)
            dash = ne.nutrition_dashboard(
                prof, [dict(r) for r in wrows], [dict(r) for r in irows], weeks)
            dash['ok'] = True
            dash['exists'] = True
            return self._send(200, dash)

        if op == 'readiness':
            conn.close()
            res = fa.readiness_score(d.get('resting_hr'), d.get('baseline_rhr'),
                                     d.get('sleep_hours'), d.get('soreness'), d.get('stress'))
            return self._send(200, {'ok': True, **res})

        if op == 'history':
            rows = conn.execute(
                'SELECT exercise,weight,reps,rpe,ts FROM fitness_log WHERE user_id=? '
                'ORDER BY ts DESC LIMIT 60', (uid,)).fetchall()
            conn.close()
            return self._send(200, {'ok': True, 'logs': [dict(r) for r in rows]})

        # op == 'gen'：生成/重新生成自己的计划
        if not DEEPSEEK_KEY:
            conn.close()
            return self._send(503, {'ok': False, 'error': '未配置 AI Key'})
        profile = d.get('profile') or {}
        res = fa.generate_plan(profile, DEEPSEEK_KEY)
        if not res.get('ok'):
            conn.close()
            return self._send(500, {'ok': False, 'error': res.get('error', '生成失败')})

        plan = res['plan']
        nick = (d.get('nickname') or profile.get('name') or '').strip()[:20]
        conn.execute(
            '''INSERT INTO fitness(user_id,nickname,profile,plan,ts) VALUES(?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 nickname=excluded.nickname, profile=excluded.profile,
                 plan=excluded.plan, ts=excluded.ts''',
            (uid, nick, json.dumps(profile, ensure_ascii=False),
             json.dumps(plan, ensure_ascii=False), now_ts()))
        conn.commit()
        conn.close()
        return self._send(200, {'ok': True, 'plan': plan})

    def _settings(self):
        """读写个人设置（每日新卡目标）。"""
        uid = self._uid()
        if not uid:
            return self._send(401, {'ok': False, 'error': '请先登录'})
        conn = db()
        ensure_user_cols(conn)
        if self.command == 'POST':
            d = self._json()
            n = int(d.get('daily_new') or 0)
            if not (1 <= n <= 500):
                conn.close()
                return self._send(400, {'ok': False, 'error': '目标需在 1-500 之间'})
            conn.execute('UPDATE users SET daily_new=? WHERE id=?', (n, uid))
            conn.commit()
        row = conn.execute('SELECT daily_new FROM users WHERE id=?', (uid,)).fetchone()
        val = (row['daily_new'] if row and row['daily_new'] else 20)
        conn.close()
        return self._send(200, {'ok': True, 'daily_new': val})

    def _tts(self, qs):
        """朗读：返回 mp3 音频流（带磁盘缓存）。"""
        text = (qs.get('text') or [''])[0].strip()
        if not text:
            return self._send(400, {'ok': False, 'error': 'text required'})
        text = text[:600]
        voice = (qs.get('voice') or [''])[0]
        rate = (qs.get('rate') or ['+0%'])[0]
        # 语速：连续可调 -50% ~ +50%
        try:
            rv = int(str(rate).replace('%', '').replace('+', '').strip())
            if not (-50 <= rv <= 50):
                rv = -8
        except Exception:
            rv = -8
        rate = f'{rv:+d}%'
        data = tts_get(text, voice, rate)
        if not data:
            return self._send(503, {'ok': False, 'error': '语音服务暂不可用'})
        self.send_response(200)
        self.send_header('Content-Type', 'audio/mpeg')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'public, max-age=604800')
        self.end_headers()
        self.wfile.write(data)

    def _meta(self):
        conn = db()
        n_points = conn.execute('SELECT COUNT(*) c FROM points').fetchone()['c']
        n_atoms = conn.execute('SELECT COUNT(*) c FROM atoms').fetchone()['c']
        n_q = conn.execute('SELECT COUNT(*) c FROM questions').fetchone()['c']
        years = [r['year'] for r in conn.execute('SELECT DISTINCT year FROM questions ORDER BY year').fetchall()]
        raw_sources = [r['source'] for r in conn.execute(
            "SELECT DISTINCT source FROM questions WHERE source LIKE '苏%' OR source LIKE '徐%'").fetchall()]
        sources, seen = [], set()
        for s in raw_sources:
            key = '徐涛10套卷*' if s.startswith('徐涛10套卷') else s
            if key not in seen:
                seen.add(key)
                sources.append(key)
        subjects = [r['subject'] for r in conn.execute('SELECT subject, COUNT(*) c FROM points GROUP BY subject ORDER BY c DESC').fetchall()]
        conn.close()
        return self._send(200, {'ok': True, 'points': n_points, 'atoms': n_atoms, 'questions': n_q,
                                'years': years, 'subjects': subjects, 'sources': sources,
                                'daily_new': DAILY_NEW})

    def _register(self):
        d = self._json()
        nick = (d.get('nick') or '').strip()
        pw = d.get('pw') or ''
        if not (2 <= len(nick) <= 12):
            return self._send(400, {'ok': False, 'error': '昵称需要 2-12 个字'})
        if len(pw) < 6:
            return self._send(400, {'ok': False, 'error': '密码至少 6 位'})
        salt = secrets.token_hex(16)
        conn = db()
        try:
            cur = conn.execute('INSERT INTO users(nick,pw_hash,salt,created_at) VALUES(?,?,?,?)',
                               (nick, hash_pw(pw, salt), salt, now_ts()))
            conn.commit()
            uid = cur.lastrowid
        except sqlite3.IntegrityError:
            conn.close()
            return self._send(400, {'ok': False, 'error': '这个昵称已被用了'})
        conn.close()
        return self._send(200, {'ok': True, 'token': make_token(uid), 'nick': nick, 'uid': uid})

    def _login(self):
        d = self._json()
        nick = (d.get('nick') or '').strip()
        pw = d.get('pw') or ''
        conn = db()
        row = conn.execute('SELECT * FROM users WHERE nick=?', (nick,)).fetchone()
        conn.close()
        if not row or not hmac.compare_digest(row['pw_hash'], hash_pw(pw, row['salt'])):
            return self._send(400, {'ok': False, 'error': '昵称或密码不对'})
        return self._send(200, {'ok': True, 'token': make_token(row['id']), 'nick': row['nick'], 'uid': row['id']})

    def _today(self, qs=None):
        uid = self._uid()
        if not uid:
            return self._send(401, {'ok': False, 'error': '请先登录'})
        conn = db()
        day = today_str()
        ts = now_ts()
        daily = conn.execute('SELECT * FROM daily WHERE user_id=? AND day=?', (uid, day)).fetchone()
        new_done = daily['new_done'] if daily else 0
        rev_done = daily['review_done'] if daily else 0
        ensure_user_cols(conn)
        urow = conn.execute('SELECT daily_new FROM users WHERE id=?', (uid,)).fetchone()
        goal = (urow['daily_new'] if urow and urow['daily_new'] else DAILY_NEW)

        subj = ((qs or {}).get('subject') or [''])[0]
        sw = ' AND a.subject=?' if subj else ''
        sa = [subj] if subj else []

        due = conn.execute(f"""
            SELECT a.id,a.subject,a.chapter,a.front,a.back,a.src_point,r.interval,r.reps
            FROM atom_reviews r JOIN atoms a ON a.id=r.atom_id
            WHERE r.user_id=? AND r.due<=? AND r.interval>0{sw}
            ORDER BY r.due LIMIT ?
        """, (uid, ts, *sa, DAILY_REVIEW_MAX)).fetchall()

        new_needed = max(0, goal - new_done)
        new = conn.execute(f"""
            SELECT a.id,a.subject,a.chapter,a.front,a.back,a.src_point
            FROM atoms a
            WHERE a.id NOT IN (SELECT atom_id FROM atom_reviews WHERE user_id=?){sw}
            ORDER BY a.id LIMIT ?
        """, (uid, *sa, new_needed)).fetchall()

        again = conn.execute(f"""
            SELECT a.id,a.subject,a.chapter,a.front,a.back,a.src_point
            FROM atom_reviews r JOIN atoms a ON a.id=r.atom_id
            WHERE r.user_id=? AND r.interval=0 AND r.due<=?{sw}
            ORDER BY r.due LIMIT 40
        """, (uid, ts, *sa)).fetchall()

        def pack(rows, kind):
            return [{'id': r['id'], 'subject': r['subject'], 'chapter': r['chapter'],
                     'front': r['front'], 'back': r['back'], 'kind': kind, 'src_point': r['src_point'],
                     'atom': True} for r in rows]

        mode = ((qs or {}).get('mode') or ['all'])[0]
        if mode == 'counts':
            cards = []
        elif mode == 'review':
            cards = pack(again, 'again') + pack(due, 'review')
        elif mode == 'new':
            cards = pack(new, 'new')
        else:
            cards = pack(again, 'again') + pack(due, 'review') + pack(new, 'new')
        for c in cards:
            if c.get('subject') in ('选择题方法', '分析题', '时政', '大题模板', '曲艺模板'):
                c['read'] = True          # 方法论/模板/时政：只阅读，不出题
            else:
                c.update(build_atom_quiz(conn, c))
            # 附带该知识点原文里的易错判断
            sp = c.get('src_point')
            row = conn.execute('SELECT back FROM points WHERE id=?', (sp,)).fetchone() if sp else None
            if row:
                js = extract_judgements(row['back'])
                if js:
                    c['judge'] = js
        conn.close()
        return self._send(200, {'ok': True, 'cards': cards, 'mode': mode,
                                'goal': goal, 'counts': {'new': len(new), 'review': len(due),
                                           'again': len(again), 'done_today': new_done + rev_done,
                                           'new_done': new_done, 'rev_done': rev_done}})

    def _learned(self, qs):
        """卡片检索：scope=learned 只看学过的，scope=all 搜全库。"""
        uid = self._uid()
        if not uid:
            return self._send(401, {'ok': False, 'error': '请先登录'})
        subject = (qs.get('subject') or [''])[0]
        kw = (qs.get('q') or [''])[0].strip()
        scope = (qs.get('scope') or ['learned'])[0]
        conn = db()

        if scope == 'all':
            sql = """SELECT p.id,p.subject,p.chapter,p.front,p.back,
                            r.interval,r.reps,r.lapses,r.last_grade,r.due,
                            CASE WHEN r.user_id IS NULL THEN 0 ELSE 1 END AS learned
                     FROM atoms p
                     LEFT JOIN atom_reviews r ON r.atom_id=p.id AND r.user_id=?
                     WHERE 1=1"""
            args = [uid]
            if subject:
                sql += ' AND p.subject=?'
                args.append(subject)
            if kw:
                sql += ' AND (p.front LIKE ? OR p.back LIKE ?)'
                args += [f'%{kw}%', f'%{kw}%']
            sql += ' ORDER BY learned DESC, p.subject, p.ord LIMIT 400'
            rows = conn.execute(sql, args).fetchall()
        else:
            sql = """SELECT p.id,p.subject,p.chapter,p.front,p.back,
                            r.interval,r.reps,r.lapses,r.last_grade,r.due, 1 AS learned
                     FROM atom_reviews r JOIN atoms p ON p.id=r.atom_id
                     WHERE r.user_id=?"""
            args = [uid]
            if subject:
                sql += ' AND p.subject=?'
                args.append(subject)
            if kw:
                sql += ' AND (p.front LIKE ? OR p.back LIKE ?)'
                args += [f'%{kw}%', f'%{kw}%']
            sql += ' ORDER BY r.due DESC LIMIT 400'
            rows = conn.execute(sql, args).fetchall()

        total_learned = conn.execute('SELECT COUNT(*) c FROM atom_reviews WHERE user_id=?', (uid,)).fetchone()['c']
        conn.close()
        out = [dict(r) for r in rows]
        now = now_ts()
        for r in out:
            if r.get('learned'):
                remain = (r['due'] - now) / 86400
                r['days'] = -1 if remain <= 0 else round(remain, 1)
            else:
                r['days'] = None
                r.setdefault('interval', 0)
                r.setdefault('reps', 0)
                r.setdefault('lapses', 0)
        return self._send(200, {'ok': True, 'points': out, 'total': total_learned, 'scope': scope})

    def _points(self, qs):
        subject = (qs.get('subject') or [''])[0]
        conn = db()
        if subject:
            rows = conn.execute('SELECT * FROM points WHERE subject=? ORDER BY ord,id', (subject,)).fetchall()
        else:
            rows = conn.execute('SELECT * FROM points ORDER BY subject,ord,id LIMIT 500').fetchall()
        conn.close()
        return self._send(200, {'ok': True, 'points': [dict(r) for r in rows]})

    def _questions(self, qs):
        """题库分页列表（EXAM-MASTER 式题库页）。"""
        kw = (qs.get('q') or [''])[0].strip()
        qtype = (qs.get('type') or [''])[0]
        source = (qs.get('source') or [''])[0]
        year = (qs.get('year') or [''])[0]
        page = max(1, int((qs.get('page') or ['1'])[0]))
        size = min(50, max(5, int((qs.get('size') or ['20'])[0])))
        where, args = [], []
        if kw:
            where.append('(stem LIKE ? OR options LIKE ?)')
            args += [f'%{kw}%', f'%{kw}%']
        if qtype:
            where.append('qtype=?')
            args.append(qtype)
        if source:
            if source.endswith('*'):
                where.append('source LIKE ?')
                args.append(source[:-1] + '%')
            else:
                where.append('source=?')
                args.append(source)
        if year:
            where.append('year=?')
            args.append(int(year))
        w = (' WHERE ' + ' AND '.join(where)) if where else ''
        conn = db()
        total = conn.execute('SELECT COUNT(*) c FROM questions' + w, args).fetchone()['c']
        rows = conn.execute(
            f'SELECT id,year,num,qtype,subject,stem,source FROM questions{w} ORDER BY year DESC, id LIMIT ? OFFSET ?',
            args + [size, (page - 1) * size]).fetchall()
        conn.close()
        out = [{'id': r['id'], 'year': r['year'], 'num': r['num'], 'qtype': r['qtype'],
                'subject': r['subject'], 'stem': r['stem'][:110],
                'source': r['source'] or f"{r['year']} 年真题"} for r in rows]
        return self._send(200, {'ok': True, 'total': total, 'page': page,
                                'pages': (total + size - 1) // size, 'size': size,
                                'questions': out})

    def _quiz(self, qs):
        ids = (qs.get('ids') or [''])[0]
        if ids:
            idlist = [int(x) for x in ids.split(',') if x.strip().isdigit()][:60]
            if not idlist:
                return self._send(200, {'ok': True, 'questions': []})
            conn = db()
            rows = conn.execute(
                f'SELECT * FROM questions WHERE id IN ({",".join("?" * len(idlist))})', idlist).fetchall()
            conn.close()
            by_id = {r['id']: r for r in rows}
            ordered = [by_id[i] for i in idlist if i in by_id]
            out = []
            for r in ordered:
                out.append(shuffle_question({
                    'id': r['id'], 'year': r['year'], 'num': r['num'], 'qtype': r['qtype'],
                    'subject': r['subject'], 'stem': r['stem'],
                    'options': json.loads(r['options']), 'answer': r['answer']}))
            return self._send(200, {'ok': True, 'questions': out})

        year = (qs.get('year') or [''])[0]
        subject = (qs.get('subject') or [''])[0]
        qtype = (qs.get('type') or [''])[0]
        source = (qs.get('source') or [''])[0]
        n = min(50, int((qs.get('n') or ['20'])[0]))
        sql = 'SELECT * FROM questions WHERE 1=1'
        args = []
        if source:
            if source.endswith('*'):
                sql += ' AND source LIKE ?'
                args.append(source[:-1] + '%')
            else:
                sql += ' AND source=?'
                args.append(source)
        if year:
            sql += ' AND year=?'
            args.append(int(year))
        if subject:
            sql += ' AND subject=?'
            args.append(subject)
        if qtype:
            sql += ' AND qtype=?'
            args.append(qtype)
        sql += ' ORDER BY RANDOM() LIMIT ?'
        args.append(n)
        conn = db()
        rows = conn.execute(sql, args).fetchall()
        conn.close()
        out = []
        for r in rows:
            out.append(shuffle_question({
                'id': r['id'], 'year': r['year'], 'num': r['num'], 'qtype': r['qtype'],
                'subject': r['subject'], 'stem': r['stem'],
                'options': json.loads(r['options']), 'answer': r['answer']}))
        return self._send(200, {'ok': True, 'questions': out})

    def _review(self):
        uid = self._uid()
        if not uid:
            return self._send(401, {'ok': False, 'error': '请先登录'})
        d = self._json()
        pid = int(d.get('point_id') or 0)
        grade = int(d.get('grade') or 0)
        if grade not in (0, 1, 2):
            return self._send(400, {'ok': False, 'error': 'grade 只能是 0/1/2'})
        conn = db()
        row = conn.execute('SELECT * FROM atom_reviews WHERE user_id=? AND atom_id=?', (uid, pid)).fetchone()
        if row:
            ease, interval = schedule(grade, row['ease'], row['interval'])
            reps = row['reps'] + 1
            lapses = row['lapses'] + (1 if grade == 0 else 0)
        else:
            ease, interval = schedule(grade, 2.5, 0)
            reps, lapses = 1, (1 if grade == 0 else 0)
        due = now_ts() + (int(interval * 86400) if interval > 0 else 300)
        conn.execute("""INSERT INTO atom_reviews(user_id,atom_id,ease,interval,due,reps,lapses,last_grade,last_ts)
                        VALUES(?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(user_id,atom_id) DO UPDATE SET
                          ease=excluded.ease, interval=excluded.interval, due=excluded.due,
                          reps=excluded.reps, lapses=excluded.lapses, last_grade=excluded.last_grade,
                          last_ts=excluded.last_ts""",
                     (uid, pid, ease, interval, due, reps, lapses, grade, now_ts()))
        # 统计口径：同一张卡当天只计一次，避免小测重考把数字刷高
        import datetime as _dt
        day_start = int(_dt.datetime.combine(_dt.date.today(), _dt.time.min).timestamp())
        already = False
        if row is not None:
            try:
                already = (row['last_ts'] or 0) >= day_start
            except (IndexError, KeyError):
                already = False
        if not already:
            self._bump_daily(conn, uid, 'review_done' if row else 'new_done')
        conn.commit()
        conn.close()
        return self._send(200, {'ok': True, 'next_due_days': round(interval, 2)})

    def _answer(self):
        uid = self._uid()
        if not uid:
            return self._send(401, {'ok': False, 'error': '请先登录'})
        d = self._json()
        qid = int(d.get('question_id') or 0)
        chosen = ''.join(sorted(set((d.get('chosen') or '').upper().replace(',', ''))))
        conn = db()
        q = conn.execute('SELECT * FROM questions WHERE id=?', (qid,)).fetchone()
        if not q:
            conn.close()
            return self._send(404, {'ok': False, 'error': '题目不存在'})
        correct = ''.join(sorted(set(q['answer'].upper())))
        ok = chosen == correct
        conn.execute('INSERT INTO attempts(user_id,question_id,chosen,correct,ts) VALUES(?,?,?,?,?)',
                     (uid, qid, chosen, 1 if ok else 0, now_ts()))
        conn.commit()
        related = match_points(conn, q['stem'])
        conn.close()
        parsed = split_analysis(q['analysis'])
        return self._send(200, {'ok': True, 'correct': ok, 'answer': q['answer'],
                                'analysis': q['analysis'], 'chosen': chosen,
                                'main': parsed['main'], 'items': parsed['items'],
                                'fixed': match_fixed(q['stem']),
                                'related': related})

    def _reason(self):
        """记录错题归因：base 基础不牢 / trans 看不懂换说法 / match 材料没对应。"""
        uid = self._uid()
        if not uid:
            return self._send(401, {'ok': False, 'error': '请先登录'})
        d = self._json()
        qid = int(d.get('question_id') or 0)
        reason = (d.get('reason') or '').strip()
        if reason not in ('base', 'trans', 'match'):
            return self._send(400, {'ok': False, 'error': 'reason 不合法'})
        conn = db()
        conn.execute('''UPDATE attempts SET reason=? WHERE id=(
                        SELECT id FROM attempts WHERE user_id=? AND question_id=?
                        ORDER BY ts DESC LIMIT 1)''', (reason, uid, qid))
        conn.commit()
        conn.close()
        return self._send(200, {'ok': True})

    def _issue_post(self):
        """报错：记录 + 立刻让 AI 判断并修复。"""
        uid = self._uid()
        if not uid:
            return self._send(401, {'ok': False, 'error': '请先登录'})
        d = self._json()
        kind = (d.get('kind') or 'card').strip()[:20]
        ref_id = int(d.get('ref_id') or 0)
        ref_text = (d.get('ref_text') or '').strip()[:600]
        note = (d.get('note') or '').strip()[:600]
        if not note and not ref_text:
            return self._send(400, {'ok': False, 'error': '请描述问题'})

        conn = db()
        cur = conn.execute(
            'INSERT INTO issues(user_id,kind,ref_id,ref_text,note,status,ts) VALUES(?,?,?,?,?,?,?)',
            (uid, kind, ref_id, ref_text, note, 'open', now_ts()))
        iid = cur.lastrowid
        conn.commit()

        # 找到这张卡
        card = conn.execute('SELECT id,subject,front,back FROM atoms WHERE id=?', (ref_id,)).fetchone()
        result = {'action': 'none', 'reason': '卡片不存在'}
        if card:
            result = ai_fix_card(card['front'], card['back'], note)
            act = (result.get('action') or 'none').lower()
            if act == 'fix' and (result.get('correct') or '').strip():
                conn.execute('UPDATE atoms SET back=? WHERE id=?',
                             (result['correct'].strip(), card['id']))
                conn.execute("UPDATE issues SET status='fixed', fix=? WHERE id=?",
                             (result['correct'].strip(), iid))
            elif act == 'drop':
                conn.execute('DELETE FROM atoms WHERE id=?', (card['id'],))
                conn.execute("UPDATE issues SET status='dropped', fix='已删除' WHERE id=?", (iid,))
            elif act == 'ok':
                conn.execute("UPDATE issues SET status='checked', fix=? WHERE id=?",
                             (result.get('reason', '内容正确'), iid))
            # none → 保持 open，等人工处理
            conn.commit()
        n = conn.execute("SELECT COUNT(*) c FROM issues WHERE status='open'").fetchone()['c']
        conn.close()
        return self._send(200, {'ok': True, 'open_total': n,
                                'action': result.get('action', 'none'),
                                'reason': result.get('reason', ''),
                                'correct': result.get('correct', '')})

    def _dati_wrong(self):
        """大题错题集。"""
        uid = self._uid()
        if not uid:
            return self._send(401, {'ok': False, 'error': '请先登录'})
        conn = db()
        rows = conn.execute(
            'SELECT * FROM dati_wrong WHERE user_id=? ORDER BY ts DESC LIMIT 200', (uid,)).fetchall()
        conn.close()
        return self._send(200, {'ok': True, 'items': [dict(r) for r in rows]})

    def _dati_wrong_post(self):
        """记录/删除大题错句。"""
        uid = self._uid()
        if not uid:
            return self._send(401, {'ok': False, 'error': '请先登录'})
        d = self._json()
        op = (d.get('op') or 'add').strip()
        pid = int(d.get('pid') or 0)
        sentence = (d.get('sentence') or '').strip()[:400]
        if not pid or not sentence:
            return self._send(400, {'ok': False, 'error': 'pid/sentence required'})
        conn = db()
        if op == 'del':
            conn.execute('DELETE FROM dati_wrong WHERE user_id=? AND pid=? AND sentence=?',
                         (uid, pid, sentence))
        else:
            conn.execute(
                '''INSERT INTO dati_wrong(user_id,pid,sentence,stem,title,chapter,ts)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(user_id,pid,sentence) DO UPDATE SET ts=excluded.ts''',
                (uid, pid, sentence, (d.get('stem') or '')[:800],
                 (d.get('title') or '')[:120], (d.get('chapter') or '')[:60], now_ts()))
        conn.commit()
        n = conn.execute('SELECT COUNT(*) c FROM dati_wrong WHERE user_id=?', (uid,)).fetchone()['c']
        conn.close()
        return self._send(200, {'ok': True, 'total': n})

    def _dev(self):
        """开发助手：网页里下指令，AI 直接改代码。"""
        uid = self._uid()
        if not uid:
            return self._send(401, {'ok': False, 'error': '请先登录'})
        d = self._json()
        op = (d.get('op') or 'edit').strip()
        import sys as _sys
        _sys.path.insert(0, BASE)
        try:
            import dev_agent as da
        except Exception as e:
            return self._send(500, {'ok': False, 'error': f'加载失败 {e}'})

        if op == 'rollback':
            rel = (d.get('file') or '').strip()
            ok = da.rollback(rel)
            if ok:
                da.restart()
            return self._send(200, {'ok': ok, 'msg': '已回滚' if ok else '没有备份'})

        instruction = (d.get('instruction') or '').strip()
        if len(instruction) < 4:
            return self._send(400, {'ok': False, 'error': '指令太短'})
        if not DEEPSEEK_KEY:
            return self._send(503, {'ok': False, 'error': '未配置 DEEPSEEK_KEY'})

        try:
            plan = da.ai_edits(instruction, DEEPSEEK_KEY)
        except Exception as e:
            return self._send(500, {'ok': False, 'error': f'AI 失败: {e}'[:300]})

        edits = plan.get('edits') or []
        explain = plan.get('explain') or ''
        if not edits:
            return self._send(200, {'ok': True, 'applied': [], 'explain': explain,
                                    'need_restart': False, 'msg': '没有需要改的地方'})

        log = da.apply(edits, instruction)
        ok_any = any(x.get('ok') for x in log)
        need_restart = bool(plan.get('restart')) and ok_any
        if need_restart:
            da.restart()
        return self._send(200, {'ok': True, 'applied': log, 'explain': explain,
                                'need_restart': need_restart,
                                'msg': '已应用 %d 处修改' % sum(1 for x in log if x.get('ok'))})

    def _dati_edit(self):
        """直接编辑模板正文（用户自己改，或 AI 改）。"""
        uid = self._uid()
        if not uid:
            return self._send(401, {'ok': False, 'error': '请先登录'})
        d = self._json()
        pid = int(d.get('pid') or 0)
        back = (d.get('back') or '').strip()
        if not pid or len(back) < 10:
            return self._send(400, {'ok': False, 'error': 'pid 或内容不合法'})
        conn = db()
        row = conn.execute('SELECT id FROM points WHERE id=?', (pid,)).fetchone()
        if not row:
            conn.close()
            return self._send(404, {'ok': False, 'error': '模板不存在'})
        conn.execute('UPDATE points SET back=? WHERE id=?', (back[:4000], pid))
        conn.execute('UPDATE atoms SET back=? WHERE src_point=?', (back[:4000], pid))
        conn.commit()
        conn.close()
        return self._send(200, {'ok': True, 'back': back[:4000]})

    def _dati(self, qs):
        """大题交替挖空刷题队列。"""
        uid = self._uid()
        if not uid:
            return self._send(401, {'ok': False, 'error': '请先登录'})
        subj = (qs.get('subject') or ['曲艺模板'])[0]
        n = min(60, max(5, int((qs.get('n') or ['20'])[0])))
        conn = db()
        quizzes = build_dati_quiz(conn, subj, limit=n)
        total = conn.execute('SELECT COUNT(*) c FROM points WHERE subject=?', (subj,)).fetchone()['c']
        conn.close()
        for q in quizzes:
            q.pop('full', None)   # 整段太长，前端用不上
        return self._send(200, {'ok': True, 'quiz': quizzes, 'total_cards': total, 'subject': subj})

    def _issues(self, qs):
        """查看待处理报错（管理用）。"""
        conn = db()
        status = (qs.get('status') or ['open'])[0]
        rows = conn.execute(
            'SELECT * FROM issues WHERE status=? ORDER BY ts DESC LIMIT 200', (status,)).fetchall()
        conn.close()
        return self._send(200, {'ok': True, 'issues': [dict(r) for r in rows]})

    def _fav(self):
        """收藏 / 取消收藏题目。"""
        uid = self._uid()
        if not uid:
            return self._send(401, {'ok': False, 'error': '请先登录'})
        d = self._json()
        qid = int(d.get('question_id') or 0)
        if not qid:
            return self._send(400, {'ok': False, 'error': 'question_id required'})
        conn = db()
        exists = conn.execute('SELECT 1 FROM favorites WHERE user_id=? AND question_id=?',
                              (uid, qid)).fetchone()
        if exists:
            conn.execute('DELETE FROM favorites WHERE user_id=? AND question_id=?', (uid, qid))
            on = False
        else:
            conn.execute('INSERT INTO favorites(user_id,question_id,ts) VALUES(?,?,?)',
                         (uid, qid, now_ts()))
            on = True
        conn.commit()
        conn.close()
        return self._send(200, {'ok': True, 'on': on})

    def _bump_daily(self, conn, uid, field):
        day = today_str()
        conn.execute('INSERT OR IGNORE INTO daily(user_id,day) VALUES(?,?)', (uid, day))
        conn.execute(f'UPDATE daily SET {field}={field}+1 WHERE user_id=? AND day=?', (uid, day))

    def _stats(self):
        uid = self._uid()
        if not uid:
            return self._send(401, {'ok': False, 'error': '请先登录'})
        conn = db()
        learned = conn.execute('SELECT COUNT(*) c FROM atom_reviews WHERE user_id=?', (uid,)).fetchone()['c']
        mastered = conn.execute('SELECT COUNT(*) c FROM atom_reviews WHERE user_id=? AND interval>=21', (uid,)).fetchone()['c']
        total = conn.execute('SELECT COUNT(*) c FROM questions').fetchone()['c']
        done = conn.execute('SELECT COUNT(*) c FROM attempts WHERE user_id=?', (uid,)).fetchone()['c']
        right = conn.execute('SELECT COUNT(*) c FROM attempts WHERE user_id=? AND correct=1', (uid,)).fetchone()['c']
        by_sub = conn.execute("""SELECT a.subject, COUNT(*) total, SUM(CASE WHEN r.interval>=21 THEN 1 ELSE 0 END) mastered
                                 FROM atom_reviews r JOIN atoms a ON a.id=r.atom_id
                                 WHERE r.user_id=? GROUP BY a.subject""", (uid,)).fetchall()
        conn.close()
        return self._send(200, {'ok': True, 'learned': learned, 'mastered': mastered,
                                'questions_total': total, 'questions_done': done,
                                'accuracy': round(right / done * 100, 1) if done else 0,
                                'by_subject': [dict(r) for r in by_sub]})

    def _curve(self):
        """记忆曲线数据：未来 7 天复习量 + 记忆强度分布 + 保持率 + 连续天数。"""
        uid = self._uid()
        if not uid:
            return self._send(401, {'ok': False, 'error': '请先登录'})
        import math
        conn = db()
        now = now_ts()
        rows = conn.execute(
            'SELECT interval, due, reps, lapses FROM atom_reviews WHERE user_id=?', (uid,)).fetchall()

        # 未来 7 天到期量（逾期全部计入今天）
        upcoming = []
        for d in range(7):
            lo = now + d * 86400
            hi = lo + 86400
            if d == 0:
                c = sum(1 for r in rows if r['due'] <= hi)
            else:
                c = sum(1 for r in rows if lo < r['due'] <= hi)
            upcoming.append(c)

        # 记忆强度分布
        def bucket(iv):
            if iv < 1:
                return 0
            if iv < 3:
                return 1
            if iv < 7:
                return 2
            if iv < 21:
                return 3
            return 4
        labels = ['今天', '1-3天', '3-7天', '7-21天', '21天+']
        dist = [0] * 5
        for r in rows:
            dist[bucket(r['interval'])] += 1

        # 保持率：对每张卡按已过时间估算记忆留存
        keep = 0.0
        for r in rows:
            iv = max(r['interval'], 0.5)
            last = r['due'] - iv * 86400
            t = max(0.0, (now - last) / 86400)
            keep += math.exp(-t / (iv * 2.2))
        retention = round(keep / len(rows) * 100, 1) if rows else 0

        # 连续天数
        days = [r['day'] for r in conn.execute(
            'SELECT day FROM daily WHERE user_id=? ORDER BY day DESC LIMIT 90', (uid,)).fetchall()]
        streak = 0
        cur = dt.date.today()
        have = set(days)
        while cur.isoformat() in have:
            streak += 1
            cur -= dt.timedelta(days=1)

        total_points = conn.execute('SELECT COUNT(*) c FROM points').fetchone()['c']
        conn.close()
        return self._send(200, {
            'ok': True, 'upcoming': upcoming, 'labels': labels, 'dist': dist,
            'retention': retention, 'streak': streak,
            'learned': len(rows), 'total': total_points,
        })

    def _point_get(self, qs):
        """单张知识点/模板详情。"""
        uid = self._uid()
        if not uid:
            return self._send(401, {'ok': False, 'error': '请先登录'})
        pid = int((qs.get('id') or ['0'])[0])
        if not pid:
            return self._send(400, {'ok': False, 'error': 'id required'})
        conn = db()
        r = conn.execute('SELECT id,subject,chapter,front,back FROM points WHERE id=?', (pid,)).fetchone()
        conn.close()
        if not r:
            return self._send(404, {'ok': False, 'error': '不存在'})
        return self._send(200, {'ok': True, 'point': dict(r)})

    def _point_questions(self, qs):
        """某个知识点考过的题（含来源卷子与题号）。"""
        pid = int((qs.get('point_id') or ['0'])[0] or 0)
        if not pid:
            return self._send(400, {'ok': False, 'error': 'point_id required'})
        conn = db()
        rows = conn.execute('''
            SELECT q.id,q.year,q.num,q.qtype,q.subject,q.stem,q.options,q.answer,q.analysis,q.source,
                   l.score
            FROM q_links l JOIN questions q ON q.id=l.question_id
            WHERE l.point_id=? ORDER BY l.score DESC LIMIT 10''', (pid,)).fetchall()
        conn.close()
        out = []
        for r in rows:
            parsed = split_analysis(r['analysis'])
            out.append({'id': r['id'], 'year': r['year'], 'num': r['num'], 'qtype': r['qtype'],
                        'subject': r['subject'], 'stem': r['stem'],
                        'options': json.loads(r['options']), 'answer': r['answer'],
                        'analysis': r['analysis'], 'main': parsed['main'], 'items': parsed['items'],
                        'source': r['source'] or f"{r['year']} 年真题"})
        return self._send(200, {'ok': True, 'questions': out})

    def _favorites(self):
        """收藏的题目列表。"""
        uid = self._uid()
        if not uid:
            return self._send(401, {'ok': False, 'error': '请先登录'})
        conn = db()
        rows = conn.execute('''
            SELECT q.id,q.year,q.num,q.qtype,q.subject,q.stem,q.options,q.answer,q.analysis,q.source
            FROM favorites f JOIN questions q ON q.id=f.question_id
            WHERE f.user_id=? ORDER BY f.ts DESC LIMIT 300''', (uid,)).fetchall()
        conn.close()
        out = []
        for r in rows:
            out.append(shuffle_question({
                'id': r['id'], 'year': r['year'], 'num': r['num'], 'qtype': r['qtype'],
                'subject': r['subject'], 'stem': r['stem'],
                'options': json.loads(r['options']), 'answer': r['answer'],
                'analysis': r['analysis'], 'source': r['source'] or f"{r['year']} 年真题"}))
        return self._send(200, {'ok': True, 'questions': out})

    def _wrong(self):
        uid = self._uid()
        if not uid:
            return self._send(401, {'ok': False, 'error': '请先登录'})
        conn = db()
        rows = conn.execute("""
            SELECT q.*, MAX(a.ts) last_ts FROM attempts a JOIN questions q ON q.id=a.question_id
            WHERE a.user_id=? AND a.correct=0 GROUP BY q.id ORDER BY last_ts DESC LIMIT 100
        """, (uid,)).fetchall()
        conn.close()
        out = []
        for r in rows:
            out.append(shuffle_question({
                'id': r['id'], 'year': r['year'], 'num': r['num'], 'qtype': r['qtype'],
                'subject': r['subject'], 'stem': r['stem'], 'options': json.loads(r['options']),
                'answer': r['answer'], 'analysis': r['analysis'],
                'source': r['source'] or f"{r['year']} 年真题"}))
        return self._send(200, {'ok': True, 'questions': out})


def main():
    init_db()
    os.makedirs(STATIC, exist_ok=True)
    srv = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    print(f'KaoyanWords listening on http://0.0.0.0:{PORT}')
    srv.serve_forever()


if __name__ == '__main__':
    main()
