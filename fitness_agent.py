# -*- coding: utf-8 -*-
"""顶级力量教练引擎 v3。

确定性算法来源（GitHub theimmortal68/IronLog-V2，Python 自适应力量训练引擎）：
- .specs/02-load-ratchet.md     负荷棘轮：下次处方永不低于本次实际举起
- .specs/49-ht-clean-advance-gating.md  clean advance：全部工作组达标才允许进阶
- .specs/21-daily-readiness-model.md    每日准备度（静息心率/睡眠/主观）
- ironlog/engine/stall.py        停滞检测
- ironlog/engine/progression.py  RPE8 标准推进
- ironlog/engine/readiness.py    准备度评分

设计哲学（顶级教练 vs 普通 AI 的分水岭）：
1. 先算数，后写文案。LLM 只负责组织语言，不许编数字。
2. 晋级要有门槛：clean session 才加重量，脏 session 原地不动。
3. 棘轮不可逆：实际举起的重量就是新地板。
4. 停滞有定义：连续 N 次不达标就减载，不靠感觉。
"""
import json
import os
import re
import urllib.request

ROOT = os.environ.get('APP_ROOT') or os.path.dirname(os.path.abspath(__file__))
DEEPSEEK_URL = 'https://api.deepseek.com/chat/completions'

# 顶级的 RPE → 可完成次数对照（Reps in Reserve 表）
RPE_RIR = {
    10.0: 0, 9.5: 0.5, 9.0: 1, 8.5: 1.5, 8.0: 2,
    7.5: 2.5, 7.0: 3, 6.5: 3.5, 6.0: 4,
}
# Epley 系数的精细版（次数越高越不准，用 Brzycki 修正）
def _brzycki(weight, reps):
    try:
        w, r = float(weight), int(reps)
    except Exception:
        return None
    if w <= 0 or r <= 0:
        return None
    if r >= 37:
        return None
    return w * 36.0 / (37 - r)


def _epley(weight, reps):
    try:
        w, r = float(weight), int(reps)
    except Exception:
        return None
    if w <= 0 or r <= 0:
        return None
    if r == 1:
        return w
    return w * (1 + r / 30.0)


def e1rm(weight, reps, rpe=None):
    """估算 1RM。取两种公式均值，有 RPE 时按余力修正次数。"""
    try:
        r = float(reps or 0)
    except Exception:
        r = 0
    if rpe:
        rir = RPE_RIR.get(float(rpe))
        if rir is not None:
            r = r + rir        # 实际可完成次数 = 完成次数 + 余力
    a = _epley(weight, r)
    b = _brzycki(weight, r)
    vals = [v for v in (a, b) if v]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 1)


def load_step(current_load, lift_category='lower'):
    """加重幅度：下肢 5kg，上肢 2.5kg，小肌群 1.25kg。轻重量时减半。"""
    try:
        w = float(current_load or 0)
    except Exception:
        w = 0
    if w <= 0:
        return 2.5
    if w < 20:
        return 1.25
    if lift_category == 'lower':
        return 5.0 if w >= 60 else 2.5
    if lift_category == 'upper':
        return 2.5 if w >= 30 else 1.25
    return 1.25


def performed_floor_delta(current_load, performed_loads):
    """【负荷棘轮】下次处方不得低于本次实际举起的最大重量。"""
    if current_load is None or not performed_loads:
        return 0.0
    return max(max(performed_loads) - current_load, 0.0)


def is_clean_session(sets, target_reps, max_rpe=8.0):
    """【clean advance gating】是否「干净通过」。

    条件：所有工作组都达到目标次数，且 RPE ≤ 8（即至少留 2 次余力）。
    这是顶级教练和瞎加重的核心区别。
    sets: [{'reps':8,'rpe':8}, ...]
    """
    if not sets:
        return False, '没有训练记录'
    for i, s in enumerate(sets, 1):
        try:
            reps = int(s.get('reps') or 0)
            rpe = float(s.get('rpe') or 10)
        except Exception:
            return False, '第 %d 组数据不完整' % i
        if reps < target_reps:
            return False, '第 %d 组只做了 %d 次（目标 %d），未达标' % (i, reps, target_reps)
        if rpe > max_rpe:
            return False, '第 %d 组 RPE %.1f 超过 %.0f，强度过高' % (i, rpe, max_rpe)
    return True, '全部 %d 组达标且 RPE≤%.0f，允许加重' % (len(sets), max_rpe)


def next_load(history, target_reps, category='lower', max_rpe=8.0):
    """综合负荷棘轮 + clean gate，算出下次该练多重。

    history: 按时间倒序 [{weight,reps,rpe}, ...]
    返回 {load, action, reason, e1rm}
    """
    if not history:
        return {'load': None, 'action': 'calibrate',
                'reason': '没有历史记录，先做一次校准：用能完成目标次数、末组留 2-3 次余力的重量',
                'e1rm': None}

    last_weight = history[0].get('weight') or 0
    heaviest = max((h.get('weight') or 0) for h in history)
    est = e1rm(heaviest,
               max((h.get('reps') or 0) for h in history if (h.get('weight') or 0) == heaviest))

    # 本次这组（最近一次训练的同重量全部组）
    last_sets = [h for h in history if (h.get('weight') or 0) == last_weight]
    clean, why = is_clean_session(last_sets, target_reps, max_rpe)

    # 棘轮：地板 = 历史最重
    floor = heaviest

    if clean:
        step = load_step(floor, category)
        proposed = floor + step
        return {'load': proposed, 'action': 'advance',
                'reason': '%s → 加重 %g kg，下次用 %g kg（次数回到区间下限）' % (why, step, proposed),
                'e1rm': est, 'floor': floor}
    else:
        return {'load': floor, 'action': 'hold',
                'reason': '%s → 重量保持 %g kg，先把次数和动作质量补齐' % (why, floor),
                'e1rm': est, 'floor': floor}


def stall_check(history, target_reps, limit=3):
    """【停滞检测】连续 limit 次训练都没达到目标次数 → 需要减载。"""
    if len(history) < limit:
        return {'stalled': False, 'reason': '记录不足 %d 次，暂不判断' % limit}
    recent = history[:limit]
    fails = sum(1 for h in recent if (h.get('reps') or 0) < target_reps)
    if fails >= limit:
        return {'stalled': True,
                'reason': '最近 %d 次训练都没达到 %d 次，判定停滞' % (limit, target_reps),
                'action': '减重 10%%，用 %g kg 重新爬升，或安排减载周',
                'suggest_load': round((max((h.get('weight') or 0) for h in recent)) * 0.9, 1)}
    return {'stalled': False, 'reason': '最近 %d 次中有 %d 次未达标，尚未停滞' % (limit, fails)}


def weekly_volume_target(level, weeks_trained=0):
    """每大肌群每周有效组数。"""
    table = {'纯新手': (8, 12), '新手': (8, 12), '初中级': (10, 16),
             '练过几个月': (10, 16), '中级': (12, 18), '高级': (16, 22)}
    lo, hi = table.get(level, (10, 16))
    if weeks_trained < 4:
        return lo
    return int(round((lo + hi) / 2))


def rep_range(goal):
    if '力量' in goal:
        return {'reps': '3-5', 'target': 5, 'rir': '2-3', 'rest': 240, 'pct': '85-92%'}
    if '减脂' in goal:
        return {'reps': '12-15', 'target': 12, 'rir': '2-3', 'rest': 60, 'pct': '60-70%'}
    if '体态' in goal or '健康' in goal:
        return {'reps': '10-15', 'target': 10, 'rir': '3', 'rest': 75, 'pct': '60-70%'}
    return {'reps': '6-10', 'target': 8, 'rir': '2-3', 'rest': 120, 'pct': '70-80%'}


def protein_target(weight, goal):
    try:
        w = float(weight or 0)
    except Exception:
        return None
    if w <= 0:
        return None
    if '减脂' in goal:
        lo, hi = 2.0, 2.4
    elif '增肌' in goal:
        lo, hi = 1.6, 2.2
    else:
        lo, hi = 1.4, 2.0
    return {'lo': int(w * lo), 'hi': int(w * hi), 'per_meal': int(w * lo / 4)}


def calorie_target(weight, height, age, gender, bodyfat, goal):
    try:
        w, h, a = float(weight), float(height), float(age)
    except Exception:
        return None
    if w <= 0 or h <= 0 or a <= 0:
        return None
    bmr = (10 * w + 6.25 * h - 5 * a - 161) if gender == '女' else (10 * w + 6.25 * h - 5 * a + 5)
    # Katch-McArdle 更准（有体脂率时）
    try:
        bf = float(bodyfat)
        if 5 < bf < 50:
            lbm = w * (1 - bf / 100.0)
            katch = 370 + 21.6 * lbm
            bmr = (bmr + katch) / 2.0
    except Exception:
        pass
    tdee = bmr * 1.55
    if '减脂' in goal:
        cal = tdee - 400
    elif '增肌' in goal:
        cal = tdee + 250
    else:
        cal = tdee
    return {'bmr': int(bmr), 'tdee': int(tdee), 'target': int(cal)}


def bmi(weight, height):
    try:
        w, h = float(weight), float(height) / 100.0
        if w <= 0 or h <= 0:
            return None
        v = w / (h * h)
        tag = '偏瘦' if v < 18.5 else '正常' if v < 24 else '超重' if v < 28 else '肥胖'
        return {'value': round(v, 1), 'tag': tag}
    except Exception:
        return None


def readiness_score(resting_hr, baseline_rhr, sleep_hours, soreness, stress):
    """0-100 分，照 IronLog readiness 的信号加权。"""
    score, notes = 100.0, []
    try:
        if resting_hr and baseline_rhr:
            d = float(resting_hr) - float(baseline_rhr)
            if d >= 7:
                score -= 25
                notes.append('静息心率比基线高 %.0f 次，恢复不足' % d)
            elif d >= 4:
                score -= 12
                notes.append('静息心率略高 %.0f 次' % d)
    except Exception:
        pass
    try:
        sh = float(sleep_hours)
        if sh < 6:
            score -= 25
            notes.append('睡眠不足 6 小时')
        elif sh < 7:
            score -= 12
            notes.append('睡眠偏少')
        elif sh >= 8:
            score += 5
    except Exception:
        pass
    try:
        s = int(soreness)
        if s >= 8:
            score -= 25
            notes.append('肌肉酸痛严重')
        elif s >= 6:
            score -= 12
            notes.append('酸痛偏高')
    except Exception:
        pass
    try:
        st = int(stress)
        if st >= 8:
            score -= 15
            notes.append('生活压力大')
        elif st >= 6:
            score -= 8
    except Exception:
        pass
    score = max(0, min(100, round(score)))
    if score >= 85:
        action = '按计划上强度，可以冲 PR'
    elif score >= 70:
        action = '正常训练，重量不变，别追力竭'
    elif score >= 50:
        action = '降量 20%，重量降 5%，RIR 留 3-4 次'
    else:
        action = '今天别练大重量，做技术练习或主动恢复'
    return {'score': score, 'action': action, 'notes': notes}


THEORY = """【顶级教练的判断标准 —— 必须内化】
一、渐进超负荷不是「每次加重」，而是「达标才加重」
   clean session 定义：当天所有工作组都达到目标次数，且 RPE≤8（留 2 次余力）。
   只有 clean 才加重量；不 clean 就原地重复，先把次数和质量补齐。
   绝大多数人练不大，就是长期在「重量涨了但次数掉了」的脏进阶里打转。

二、负荷棘轮（不可逆）
   只要学员实际举起了比处方更重的重量且动作标准，下次处方绝不能低于它。

三、训练量与强度分层
   每个大肌群每周 10–20 个有效组；新手取下限，进阶取上限，超过 22 组恢复跟不上。
   复合动作 RIR 2–3，力竭只放在孤立动作最后一组。
   次数区间决定刺激类型：1–5 力量、6–12 增肌、15–20 耐力。

四、周期化与减载
   连续 4–6 周后安排 1 周减载：**组数降到 60%，重量保持不变**，RIR 放宽到 3–4。

五、停滞有明确定义
   同一动作连续 3 次达不到目标次数 → 减重 10% 重新爬升，或提前减载。
   不靠感觉判断，靠次数判定。

六、恢复优先于计划
   静息心率、睡眠、酸痛三项恶化时，宁可降量也不要硬推。
   一次练废掉的状态，要用 3–4 天才能补回来。

七、伤病是约束条件，不是借口
   必须给替代动作，且写明为什么替代。例如膝伤用髋主导动作替代膝主导动作。

八、动作质量 > 重量
   新手前 4 周以动作模式学习为主。深蹲/硬拉/卧推/划船/推举必须先练对。
"""


def build_prompt(p, calc):
    return f"""你是一位带过国家级运动员、也带过几百个普通人的顶级体能教练。
你的特点是：**不追花哨，只追可持续的进步**；你会明确告诉学员什么时候不该加重。

【学员档案】
{p.get('name')}｜{p.get('gender')}｜{p.get('age')} 岁｜{p.get('height')} cm｜{p.get('weight')} kg
体脂 {p.get('bodyfat') or '未测'}｜经验 {p.get('level')}｜目标 {p.get('goal')}
每周 {p.get('days')} 练｜单次 {p.get('duration')} 分钟｜器械 {p.get('equipment')}
伤病：{p.get('injuries') or '无'}｜其他：{p.get('notes') or '无'}

【已算好的硬数据 —— 原样引用，不许改，也不许自己另算】
- BMI {calc['bmi']['value']}（{calc['bmi']['tag']}）
- BMR {calc['cal']['bmr']} kcal｜TDEE {calc['cal']['tdee']} kcal｜目标 {calc['cal']['target']} kcal/天
- 蛋白质 {calc['protein']['lo']}–{calc['protein']['hi']} g/天（每餐约 {calc['protein']['per_meal']} g）
- 每大肌群每周 {calc['volume']} 个有效组
- 次数区间 {calc['rep']['reps']}｜RIR {calc['rep']['rir']}｜组间 {calc['rep']['rest']} 秒｜强度 {calc['rep']['pct']} 1RM
- 加重幅度：下肢 +{calc['step_lower']}kg，上肢 +{calc['step_upper']}kg

{THEORY}

【输出要求】
严格返回 JSON，不要 markdown 代码块，不要解释文字。
动作数：60 分钟 5–6 个，90 分钟 7–8 个。同一肌群间隔 ≥48 小时。

{{
  "summary": "一句话：体型水平判断 + 核心策略（说人话）",
  "split": "分化方式 + 为什么这样分",
  "weekly": [
    {{"day":"第 1 天","focus":"下肢（股四头主导）","warmup":"具体热身动作与组次",
      "exercises":[
        {{"name":"杠铃深蹲","sets":4,"reps":"{calc['rep']['reps']}","rir":"RIR {calc['rep']['rir']}","rest":"{calc['rep']['rest']} 秒",
          "weight_hint":"起始重量怎么定（用能做满次数且留 2-3 次余力的重量）",
          "progression_rule":"clean 后怎么加",
          "cue":"动作要点一句话",
          "note":"替代动作（如有伤病）"}}
      ]}}
  ],
  "ratchet": "负荷棘轮在这份计划里怎么执行",
  "progression": "clean session 的判定标准 + 加重步骤（要具体到次数和 RPE）",
  "deload": "第几周减载、组数怎么降、重量为什么不降",
  "stall_rule": "什么情况判定停滞、怎么处理",
  "nutrition": "围绕上面算出的热量与蛋白给具体吃法",
  "warnings": ["针对他伤病/水平的提醒，要具体"],
  "checkpoints": "第 4 周 / 第 8 周各测什么、判断标准是什么"
}}"""


def _repair_json(txt):
    """AI 输出被 max_tokens 截断时，尽量补全成合法 JSON。"""
    txt = txt.strip()
    txt = re.sub(r'^```(?:json)?|```$', '', txt, flags=re.M).strip()
    i = txt.find('{')
    if i < 0:
        return None
    txt = txt[i:]
    # 如果已经能解析，直接返回
    try:
        return json.loads(txt)
    except Exception:
        pass
    # 逐层闭合：丢掉尾部残缺片段，补全括号
    for cut in range(len(txt), max(len(txt) - 1200, 0), -1):
        frag = txt[:cut].rstrip()
        # 去掉尾部不完整的 key/value
        frag = re.sub(r'[,\s]*"[^"]*$', '', frag)
        frag = re.sub(r'[,\s]*$', '', frag)
        # 统计括号
        opens = []
        in_str = False
        esc = False
        for ch in frag:
            if esc:
                esc = False
                continue
            if ch == '\\':
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch in '{[':
                opens.append(ch)
            elif ch in '}]':
                if opens:
                    opens.pop()
        if in_str:
            frag += '"'
        close = ''.join('}' if o == '{' else ']' for o in reversed(opens))
        try:
            obj = json.loads(frag + close)
            if isinstance(obj, dict) and obj.get('weekly'):
                return obj
        except Exception:
            continue
    return None


def generate_plan(profile, api_key):
    """算 → 调 LLM → 返回计划。"""
    if not api_key:
        return {'ok': False, 'error': '未配置 AI Key'}

    calc = {
        'bmi': bmi(profile.get('weight'), profile.get('height')) or {'value': '?', 'tag': '未知'},
        'cal': calorie_target(profile.get('weight'), profile.get('height'), profile.get('age'),
                              profile.get('gender'), profile.get('bodyfat'),
                              profile.get('goal') or '增肌') or {'bmr': '?', 'tdee': '?', 'target': '?'},
        'protein': protein_target(profile.get('weight'), profile.get('goal') or '增肌')
                   or {'lo': '?', 'hi': '?', 'per_meal': '?'},
        'volume': weekly_volume_target(profile.get('level') or '新手'),
        'rep': rep_range(profile.get('goal') or '增肌'),
        'step_lower': load_step(60, 'lower'),
        'step_upper': load_step(30, 'upper'),
    }

    body = json.dumps({
        'model': 'deepseek-chat',
        'messages': [{'role': 'user', 'content': build_prompt(profile, calc)}],
        'temperature': 0.25,
        'max_tokens': 8000,
    }).encode()
    req = urllib.request.Request(DEEPSEEK_URL, data=body, headers={
        'Authorization': 'Bearer ' + api_key,
        'Content-Type': 'application/json',
    })
    try:
        with urllib.request.urlopen(req, timeout=280) as r:
            d = json.loads(r.read().decode())
        txt = d['choices'][0]['message']['content']
        plan = _repair_json(txt)
        if not plan:
            return {'ok': False, 'error': 'AI 返回无法解析：' + txt[:200]}
        # 补齐缺失字段，防止前端崩
        for k, dv in [('summary', ''), ('split', ''), ('weekly', []), ('ratchet', ''),
                      ('progression', ''), ('deload', ''), ('stall_rule', ''),
                      ('nutrition', ''), ('warnings', []), ('checkpoints', '')]:
            plan.setdefault(k, dv)
        plan['_calc'] = {
            'bmi': calc['bmi'], 'calories': calc['cal'], 'protein': calc['protein'],
            'volume': calc['volume'], 'rep': calc['rep'],
            'step': {'lower': calc['step_lower'], 'upper': calc['step_upper']},
        }
        return {'ok': True, 'plan': plan}
    except Exception as e:
        return {'ok': False, 'error': '%s: %s' % (type(e).__name__, str(e)[:200])}
