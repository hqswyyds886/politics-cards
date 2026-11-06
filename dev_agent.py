# -*- coding: utf-8 -*-
"""开发助手：网页里下指令 → AI 生成精确替换 → 直接改代码 → 重启服务。

设计要点：
- 只做 old/new 字符串替换，不整文件重写（避免把 110KB 的前端搞坏）
- 每次改前自动备份 .bak，出错可回滚
- 前端改动不用重启，后端改动自动 systemctl restart
- 检索相关代码块送进 prompt，控制 token
"""
import json
import os
import re
import subprocess
import urllib.request

ROOT = os.environ.get('APP_ROOT') or os.path.dirname(os.path.abspath(__file__))
ALLOW = ['server.py', 'static/index.html']
DEEPSEEK_URL = 'https://api.deepseek.com/chat/completions'


def read(rel):
    try:
        with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
            return f.read()
    except Exception:
        return ''


def html_blocks(text):
    parts = re.split(r'/\* -+ ([^-]+?) -+ \*/', text)
    out = [{'name': 'header', 'body': parts[0]}]
    for i in range(1, len(parts) - 1, 2):
        out.append({'name': parts[i].strip(), 'body': parts[i + 1]})
    return out


def py_blocks(text):
    lines = text.split('\n')
    blocks, cur, name = [], [], 'header'
    for ln in lines:
        m = re.match(r'^(def |class )(\w+)', ln)
        if m and cur:
            blocks.append({'name': name, 'body': '\n'.join(cur)})
            cur, name = [ln], m.group(2)
        elif m:
            cur, name = [ln], m.group(2)
        else:
            cur.append(ln)
    if cur:
        blocks.append({'name': name, 'body': '\n'.join(cur)})
    return blocks


def context_for(instruction, kmax=7):
    words = [w for w in re.split(r'[\s，。、,.!！?？：]+', instruction) if len(w) >= 2]

    def score(b):
        return sum(b['body'].count(w) * 4 for w in words)

    ctx = []
    for b in sorted(html_blocks(read('static/index.html')), key=score, reverse=True)[:kmax]:
        if score(b) > 0:
            ctx.append('### static/index.html · ' + b['name'] + '\n```html\n' + b['body'][:3500] + '\n```')
    for b in sorted(py_blocks(read('server.py')), key=score, reverse=True)[:4]:
        if score(b) > 0:
            ctx.append('### server.py · ' + b['name'] + '\n```python\n' + b['body'][:3500] + '\n```')
    return '\n\n'.join(ctx)


def ai_edits(instruction, api_key):
    meta = {'server.py': len(read('server.py')),
            'static/index.html': len(read('static/index.html'))}

    # 1) 找出最相关的 2 个区块，给全文（保证 AI 能逐字复制）
    words = [w for w in re.split(r'[\s，。、,.!！?？：]+', instruction) if len(w) >= 2]

    def score(b):
        return sum(b['body'].count(w) for w in words)

    blocks = sorted(html_blocks(read('static/index.html')), key=score, reverse=True)
    full_blocks = [b for b in blocks if score(b) > 0][:2]
    body_txt = '\n\n'.join(
        f"### static/index.html · {b['name']}\n```\n{b['body'][:6000]}\n```" for b in full_blocks)

    # 2) 再给所有区块的名字清单（让 AI 知道有哪些地方可改）
    names = ' / '.join(b['name'] for b in html_blocks(read('static/index.html')) if b['name'] != 'header')

    prompt = f"""你是这个考研刷题站点的开发者。按用户指令改代码。

项目目录：本项目根目录
文件：server.py（Python3 标准库）、static/index.html（单页应用，HTML+CSS+JS 全在一起）
规模：{json.dumps(meta, ensure_ascii=False)}

用户指令：{instruction}

【前端现有区块】{names}

【与指令最相关的区块原文】
{body_txt or '（未匹配到，见下方 server.py 片段）'}

{('### server.py 相关片段\n```python\n' + context_for(instruction, kmax=1) + '\n```') if 'server' in instruction or not body_txt else ''}

输出严格 JSON（不要任何其它文字）：
{{"edits":[{{"file":"static/index.html","old":"原文","new":"新内容"}}],"restart":false,"explain":"说明"}}

硬性要求：
1. old 必须从上文代码块里【逐字复制】，一个字符都不能改（空格、引号、缩进都要一致）
2. old 长度 10–100 字符，选一句在文件里独一无二的文本
3. 只有 1 条 edit
4. 改 server.py → restart=true；只改前端 → restart=false
5. 如果指令是提问，返回 edits 为空并在 explain 里回答"""
    body = json.dumps({'model': 'deepseek-chat',
                       'messages': [{'role': 'user', 'content': prompt}],
                       'temperature': 0, 'max_tokens': 3000}).encode()
    req = urllib.request.Request(DEEPSEEK_URL, data=body, headers={
        'Authorization': 'Bearer ' + api_key, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=150) as r:
        d = json.loads(r.read().decode())
    txt = d['choices'][0]['message']['content'].strip()
    txt = re.sub(r'^```(?:json)?|```$', '', txt, flags=re.M).strip()
    m = re.search(r'\{[\s\S]*\}', txt)
    if not m:
        raise RuntimeError('AI 未返回 JSON: ' + txt[:200])
    return json.loads(m.group(0))
    body = json.dumps({'model': 'deepseek-chat',
                       'messages': [{'role': 'user', 'content': prompt}],
                       'temperature': 0, 'max_tokens': 3000}).encode()
    req = urllib.request.Request(DEEPSEEK_URL, data=body, headers={
        'Authorization': 'Bearer ' + api_key, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=150) as r:
        d = json.loads(r.read().decode())
    txt = d['choices'][0]['message']['content'].strip()
    txt = re.sub(r'^```(?:json)?|```$', '', txt, flags=re.M).strip()
    m = re.search(r'\{[\s\S]*\}', txt)
    if not m:
        raise RuntimeError('AI 未返回 JSON: ' + txt[:200])
    return json.loads(m.group(0))
    body = json.dumps({'model': 'deepseek-chat',
                       'messages': [{'role': 'user', 'content': prompt}],
                       'temperature': 0.1, 'max_tokens': 4000}).encode()
    req = urllib.request.Request(DEEPSEEK_URL, data=body, headers={
        'Authorization': 'Bearer ' + api_key, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=150) as r:
        d = json.loads(r.read().decode())
    txt = d['choices'][0]['message']['content'].strip()
    txt = re.sub(r'^```(?:json)?|```$', '', txt, flags=re.M).strip()
    m = re.search(r'\{[\s\S]*\}', txt)
    if not m:
        raise RuntimeError('AI 未返回 JSON: ' + txt[:200])
    return json.loads(m.group(0))


def _norm(s):
    return re.sub(r'\s+', '', s or '')


def find_fuzzy(text, old):
    """容错查找：忽略所有空白差异，返回 (start, end) 或 None。"""
    if not old:
        return None
    # 1) 精确
    i = text.find(old)
    if i >= 0:
        return (i, i + len(old))
    # 2) 忽略空白
    target = _norm(old)
    if not target:
        return None
    # 建立去掉空白后的索引映射
    idx_map = []
    buf = []
    for pos, ch in enumerate(text):
        if not ch.isspace():
            buf.append(ch)
            idx_map.append(pos)
    flat = ''.join(buf)
    j = flat.find(target)
    if j < 0:
        return None
    start = idx_map[j]
    end = idx_map[j + len(target) - 1] + 1
    return (start, end)


def repair_edit(rel, old, new, instruction):
    """AI 给的 old 匹配不上时，用文件里真实存在的近似片段兜底。"""
    text = read(rel)
    if not text or not old:
        return None
    # 用 old 去掉标签后的纯文本，在文件里找最长公共子串所在行
    plain = re.sub(r'<[^>]+>', '', old).strip()
    if len(plain) < 2:
        return None
    # 找出所有包含这段纯文本的行，取最短的一行作为锚点
    cands = [ln for ln in text.split('\n') if plain in ln and len(ln) < 400]
    if not cands:
        # 退一步：用前 6 个字找
        head = plain[:6]
        cands = [ln for ln in text.split('\n') if head in ln and len(ln) < 400]
    if not cands:
        return None
    anchor = min(cands, key=len).rstrip()
    if text.count(anchor) != 1:
        return None
    # 把 anchor 里出现的 plain 替换成 new 去掉标签后的文本
    new_plain = re.sub(r'<[^>]+>', '', new).strip() or new.strip()
    if plain and plain in anchor:
        replaced = anchor.replace(plain, new_plain)
    else:
        replaced = new_plain
    return anchor, replaced


def apply(edits, instruction=''):
    log = []
    for e in edits:
        rel = (e.get('file') or '').strip()
        if rel not in ALLOW:
            log.append({'file': rel, 'ok': False, 'msg': '不允许的文件'})
            continue
        text = read(rel)
        old = e.get('old') or ''
        new = e.get('new') or ''
        if not old:
            log.append({'file': rel, 'ok': False, 'msg': 'old 为空'})
            continue

        span = find_fuzzy(text, old)
        if span is None:
            fixed = repair_edit(rel, old, new, instruction)
            if fixed:
                anchor, replaced = fixed
                log.append({'file': rel, 'ok': True,
                            'msg': f'AI 片段未匹配，已用真实原文兜底替换（{len(anchor)} 字符）'})
                path = os.path.join(ROOT, rel)
                with open(path + '.bak', 'w', encoding='utf-8') as f:
                    f.write(text)
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(text.replace(anchor, replaced, 1))
                continue
            log.append({'file': rel, 'ok': False,
                        'msg': '匹配失败。AI 给的片段：' + old[:50].replace('\n', '⏎')})
            continue

        start, end = span
        actual = text[start:end]
        if text.count(actual) > 1:
            fixed = expand_unique(text, actual, new)
            if fixed is None:
                log.append({'file': rel, 'ok': False,
                            'msg': f'片段出现 {text.count(actual)} 次，无法消歧'})
                continue
            actual, new = fixed
            start, end = None, None

        path = os.path.join(ROOT, rel)
        with open(path + '.bak', 'w', encoding='utf-8') as f:
            f.write(text)
        if start is None:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(text.replace(actual, new, 1))
        else:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(text[:start] + new + text[end:])
        fuzzy = '（容错匹配）' if actual != old else ''
        log.append({'file': rel, 'ok': True,
                    'msg': f'已替换 {len(actual)}→{len(new)} 字符{fuzzy}'})
    return log


def expand_unique(text, old, new, radius=60):
    """old 不唯一时，向前后各扩 radius 字符，取能唯一匹配的那一段。"""
    positions = []
    start = 0
    while True:
        i = text.find(old, start)
        if i < 0:
            break
        positions.append(i)
        start = i + 1
    if not positions:
        return None
    for pos in positions:
        for r in (radius, radius * 2, radius * 4, 200):
            lo = max(0, pos - r)
            hi = min(len(text), pos + len(old) + r)
            cand = text[lo:hi]
            if text.count(cand) == 1:
                # 把 old 在 cand 中的部分替换成 new
                rel_start = pos - lo
                rebuilt = cand[:rel_start] + new + cand[rel_start + len(old):]
                return cand, rebuilt
    return None


def restart():
    try:
        svc = os.environ.get('APP_SERVICE', 'politics-cards')
        subprocess.run(['systemctl', 'restart', svc], timeout=60, check=False)
        return True
    except Exception:
        return False


def rollback(rel):
    """从 .bak 回滚。"""
    path = os.path.join(ROOT, rel)
    bak = path + '.bak'
    if not os.path.exists(bak):
        return False
    with open(bak, encoding='utf-8') as f:
        data = f.read()
    with open(path, 'w', encoding='utf-8') as f:
        f.write(data)
    return True
