# -*- coding: utf-8 -*-
"""科学饮食引擎：趋势体重反推真实 TDEE + 速率护栏 + 宏量分配。

核心方法（区别于普通 AI 的「查公式给数」）：
1. 趋势体重：用 7 天滚动均值滤掉水分波动，不看单日体重。
2. 自适应 TDEE：用「实际摄入」和「体重变化」反算真实维持热量，
   而不是照 Mifflin 公式给你一个可能差 400 kcal 的数字。
3. 速率护栏：每周体重变化必须落在区间内，超了就是吃多了/掉肌肉。
4. 宏量分配带地板：脂肪不低于 0.6 g/kg（保激素），蛋白按每餐阈值分配。
5. 饮食休息期：长期缺口必须安排维持期，否则代谢适应 + 依从性崩溃。
"""

KCAL_PER_KG_FAT = 7700.0


# ============================================================
# 一、趋势体重（滤掉水分波动）
# ============================================================

def trend_weight(entries, window=7):
    """entries: [{'d':'2026-01-01','kg':70.2}, ...] 按日期升序。
    返回每日趋势体重（指数加权 + 窗口均值）。
    """
    if not entries:
        return []
    pts = sorted([e for e in entries if e.get('kg')], key=lambda x: x['d'])
    out = []
    for i, p in enumerate(pts):
        lo = max(0, i - window + 1)
        chunk = [x['kg'] for x in pts[lo:i + 1]]
        avg = sum(chunk) / len(chunk)
        out.append({'d': p['d'], 'kg': round(p['kg'], 2), 'trend': round(avg, 2)})
    return out


def weekly_rate(entries, window_days=14):
    """最近 window_days 的周均变化率（kg/周）与占体重百分比。
    用首尾趋势值算，避免单日噪声。
    """
    if not entries or len(entries) < 2:
        return {'kg_per_week': None, 'pct_per_week': None, 'days': 0}
    pts = sorted([e for e in entries if e.get('kg')], key=lambda x: x['d'])
    if len(pts) < 2:
        return {'kg_per_week': None, 'pct_per_week': None, 'days': 0}
    tail = pts[-window_days:] if len(pts) >= window_days else pts
    first, last = tail[0], tail[-1]
    from datetime import date
    try:
        d0 = date.fromisoformat(first['d'])
        d1 = date.fromisoformat(last['d'])
        days = (d1 - d0).days
    except Exception:
        days = len(tail) - 1
    if days < 3:
        return {'kg_per_week': None, 'pct_per_week': None, 'days': days}
    # 首尾取 3 日均值，抗噪
    head_avg = sum(x['kg'] for x in tail[:3]) / min(3, len(tail))
    tail_avg = sum(x['kg'] for x in tail[-3:]) / min(3, len(tail))
    kg_per_week = (tail_avg - head_avg) / days * 7.0
    pct = kg_per_week / tail_avg * 100.0
    return {'kg_per_week': round(kg_per_week, 2),
            'pct_per_week': round(pct, 2), 'days': days,
            'from_kg': round(head_avg, 2), 'to_kg': round(tail_avg, 2)}


# ============================================================
# 二、自适应 TDEE（用真实数据反推）
# ============================================================

def adaptive_tdee(intake_entries, weight_entries, window_days=14):
    """用「平均摄入」和「同期体重变化」反算真实维持热量。

    TDEE = 平均摄入 - (体重变化kg × 7700) / 天数

    这是 MacroFactor / RP 用的方法，比公式准得多，
    因为它自动吸收了 NEAT 变化、食物热效应个体差异、代谢适应。
    """
    if not intake_entries or not weight_entries:
        return {'ok': False, 'reason': '需要同时有饮食记录和体重记录'}
    ins = sorted([e for e in intake_entries if e.get('kcal')], key=lambda x: x['d'])
    ws = sorted([e for e in weight_entries if e.get('kg')], key=lambda x: x['d'])
    if len(ins) < 5 or len(ws) < 3:
        return {'ok': False, 'reason': '记录太少（至少 5 天饮食 + 3 次体重）'}

    ins = ins[-window_days:]
    ws = ws[-window_days:]

    from datetime import date
    try:
        d_start = max(date.fromisoformat(ins[0]['d']), date.fromisoformat(ws[0]['d']))
        d_end = min(date.fromisoformat(ins[-1]['d']), date.fromisoformat(ws[-1]['d']))
        span = (d_end - d_start).days
    except Exception:
        return {'ok': False, 'reason': '日期解析失败'}
    if span < 7:
        return {'ok': False, 'reason': '重叠记录不足 7 天'}

    avg_intake = sum(e['kcal'] for e in ins) / len(ins)
    head = [w for w in ws if w['d'] <= ins[len(ins) // 4]['d']] or ws[:2]
    tail = [w for w in ws if w['d'] >= ins[-len(ins) // 4]['d']] or ws[-2:]
    w0 = sum(x['kg'] for x in head) / len(head)
    w1 = sum(x['kg'] for x in tail) / len(tail)
    delta_kg = w1 - w0

    tdee = avg_intake - (delta_kg * KCAL_PER_KG_FAT) / span
    return {
        'ok': True,
        'tdee': int(round(tdee)),
        'avg_intake': int(round(avg_intake)),
        'weight_change': round(delta_kg, 2),
        'span_days': span,
        'confidence': 'high' if span >= 21 else 'medium' if span >= 14 else 'low',
        'note': '用 %d 天数据反推：平均吃 %d kcal，体重变化 %+.2f kg → 真实维持约 %d kcal'
                % (span, round(avg_intake), delta_kg, round(tdee)),
    }


# ============================================================
# 三、速率护栏（超了就说明吃错了）
# ============================================================

RATE_GUARD = {
    '增肌': {'ideal': (0.25, 0.5), 'max': 1.0,
             'label': '精瘦增肌每周 +0.25~0.5% 体重，超过 1% 基本是长脂肪'},
    '减脂': {'ideal': (-1.0, -0.5), 'max': 1.2,
             'label': '减脂每周 -0.5~1% 体重，超过 1.2% 容易掉肌肉'},
    '力量提升': {'ideal': (0.0, 0.3), 'max': 0.8,
                 'label': '力量期体重小幅上升即可'},
    '体态改善与健康': {'ideal': (-0.5, 0.25), 'max': 0.8,
                       'label': '体态期以稳定为主'},
}


def guardrail_check(pct_per_week, goal):
    """判断速率是否在护栏内。"""
    key = next((k for k in RATE_GUARD if k in (goal or '')), '增肌')
    g = RATE_GUARD[key]
    lo, hi = g['ideal']
    if pct_per_week is None:
        return {'status': 'unknown', 'action': '多测几次体重才能判断',
                'goal_key': key, 'range': (lo, hi), 'label': g['label']}
    p = pct_per_week
    if p > g['max']:
        return {'status': 'too_fast', 'goal_key': key, 'range': (lo, hi),
                'label': g['label'],
                'action': '涨太快了（每周 %+.2f%%），减 %d kcal，否则多出来的基本都是脂肪'
                          % (p, 300)}
    if key == '减脂' and p < -g['max']:
        return {'status': 'too_fast', 'goal_key': key, 'range': (lo, hi),
                'label': g['label'],
                'action': '掉太快了（每周 %+.2f%%），加 %d kcal，再快就掉肌肉' % (p, 300)}
    if lo <= p <= hi:
        return {'status': 'perfect', 'goal_key': key, 'range': (lo, hi),
                'label': g['label'],
                'action': '速率刚好（每周 %+.2f%%），保持当前热量不变' % p}
    if p < lo:
        return {'status': 'too_slow', 'goal_key': key, 'range': (lo, hi),
                'label': g['label'],
                'action': '偏慢（每周 %+.2f%%），%s %d kcal'
                          % (p, '减' if key == '减脂' else '加', 200)}
    return {'status': 'too_fast', 'goal_key': key, 'range': (lo, hi),
            'label': g['label'],
            'action': '偏快（每周 %+.2f%%），减 %d kcal' % (p, 200)}


# ============================================================
# 四、宏量分配（带地板，不瞎分）
# ============================================================

def macro_split(calories, weight, goal, bodyfat=None):
    """蛋白/碳水/脂肪分配，带科学地板。

    - 蛋白：增肌 1.6–2.2，减脂 2.0–2.4 g/kg（减脂期更高以保肌）
    - 脂肪：不低于 0.6 g/kg（保激素），一般 0.8–1.0 g/kg
    - 碳水：剩余热量全给碳水（训练燃料）
    """
    try:
        w = float(weight or 0)
        cal = float(calories or 0)
    except Exception:
        return None
    if w <= 0 or cal <= 0:
        return None

    if '减脂' in goal:
        p_lo, p_hi = 2.0, 2.4
    elif '增肌' in goal:
        p_lo, p_hi = 1.6, 2.2
    else:
        p_lo, p_hi = 1.6, 2.0

    protein = round(w * ((p_lo + p_hi) / 2), 0)
    # 脂肪地板
    fat_floor = round(w * 0.6, 0)
    fat_target = round(w * 0.9, 0)
    fat = max(fat_floor, fat_target)

    p_kcal = protein * 4
    f_kcal = fat * 9
    carb_kcal = cal - p_kcal - f_kcal
    if carb_kcal < 0:
        # 热量太低，先保蛋白和脂肪地板，碳水清零
        carb_kcal = 0
        fat = max(fat_floor, round((cal - p_kcal) / 9, 0))
    carb = round(carb_kcal / 4, 0)

    return {
        'protein_g': int(protein), 'fat_g': int(fat), 'carb_g': int(carb),
        'protein_lo': int(w * p_lo), 'protein_hi': int(w * p_hi),
        'fat_floor': int(fat_floor),
        'p_kcal': int(p_kcal), 'f_kcal': int(f_kcal), 'c_kcal': int(max(carb_kcal, 0)),
        'per_meal_protein': int(round(protein / 4)),
        'floor_ok': carb_kcal >= 0,
    }


def fiber_target(calories):
    """膳食纤维：每 1000 kcal 摄入 14 g。"""
    try:
        return int(round(float(calories) / 1000.0 * 14))
    except Exception:
        return None


def hydration_target(weight):
    """饮水：体重 × 30–35 ml，训练日再加 500ml。"""
    try:
        w = float(weight)
        return {'lo_ml': int(w * 30), 'hi_ml': int(w * 35),
                'train_day_ml': int(w * 35 + 500)}
    except Exception:
        return None


def meal_distribution(protein_g, meals=4):
    """按每餐 0.4–0.55 g/kg 分配，并给亮氨酸阈值提示。"""
    try:
        p = float(protein_g)
    except Exception:
        return None
    per = round(p / meals, 0)
    return {'meals': meals, 'per_meal_g': int(per),
            'note': '每餐 %.0f g 蛋白，分 %d 餐。单餐 25–40 g 能最大化肌肉蛋白合成'
                    % (per, meals)}


# ============================================================
# 五、饮食休息期与调整
# ============================================================

def diet_break_plan(deficit_weeks, goal):
    """长期缺口必须安排维持期（diet break）。"""
    if '减脂' not in (goal or ''):
        return None
    try:
        wk = int(deficit_weeks or 0)
    except Exception:
        wk = 0
    if wk < 6:
        return {'needed': False,
                'note': '已连续减脂 %d 周，第 6–8 周起建议安排 1–2 周维持期' % wk}
    return {'needed': True,
            'note': '已连续减脂 %d 周，建议立刻安排 1–2 周维持期（吃到 TDEE，蛋白不变），'
                    '纠正代谢适应和依从性疲劳，之后再继续缺口' % wk,
            'weeks': 2}


def calorie_adjustment(current_kcal, rate_status, goal):
    """根据速率护栏给热量调整建议。"""
    try:
        cur = int(current_kcal)
    except Exception:
        return None
    table = {
        'perfect': (0, '保持不变，别乱调'),
        'too_fast': (-300 if '增肌' in (goal or '') else -300,
                     '减 300 kcal'),
        'too_slow': (+200, '加 200 kcal'),
        'unknown': (0, '数据不足，先测满 14 天'),
    }
    delta, txt = table.get(rate_status, (0, '保持不变'))
    return {'from': cur, 'to': cur + delta, 'delta': delta, 'action': txt}


def bodyfat_estimate(bmi_v, age, gender, waist=None):
    """Deurenberg 公式估体脂（没有体脂率时的兜底）。"""
    try:
        b = float(bmi_v)
        a = float(age)
    except Exception:
        return None
    sex = 1 if gender == '男' else 0
    bf = 1.20 * b + 0.23 * a - 10.8 * sex - 5.4
    return max(3.0, min(55.0, round(bf, 1)))


def lean_mass(weight, bodyfat):
    try:
        w, bf = float(weight), float(bodyfat)
        return {'fat_kg': round(w * bf / 100.0, 1),
                'lean_kg': round(w * (1 - bf / 100.0), 1)}
    except Exception:
        return None


# ============================================================
# 六、完整饮食看板
# ============================================================

def nutrition_dashboard(profile, weight_entries, intake_entries, deficit_weeks=0):
    """算出一整套饮食方案。"""
    goal = profile.get('goal') or '增肌'
    weight = profile.get('weight')
    height = profile.get('height')
    age = profile.get('age')
    gender = profile.get('gender') or '男'
    bodyfat = profile.get('bodyfat')

    b = bmi(weight, height) or {'value': None, 'tag': '未知'}
    # 没体脂率就用 Deurenberg 估
    bf_val = bodyfat
    bf_src = 'self'
    if not bf_val and b['value']:
        est = bodyfat_estimate(b['value'], age, gender)
        if est:
            bf_val = est
            bf_src = 'estimated'

    base_cal = calorie_target(weight, height, age, gender, bf_val, goal) \
        or {'bmr': None, 'tdee': None, 'target': None}

    # 有真实数据就覆盖公式 TDEE
    adapt = adaptive_tdee(intake_entries, weight_entries)
    if adapt.get('ok'):
        maintenance = adapt['tdee']
        if '减脂' in goal:
            target = maintenance - 400
        elif '增肌' in goal:
            target = maintenance + 250
        else:
            target = maintenance
        cal_source = 'adaptive'
    else:
        maintenance = base_cal['tdee']
        target = base_cal['target']
        cal_source = 'formula'

    macros = macro_split(target, weight, goal, bf_val)
    rate = weekly_rate(weight_entries)
    guard = guardrail_check(rate['pct_per_week'], goal)
    adj = calorie_adjustment(target, guard['status'], goal)
    lm = lean_mass(weight, bf_val) if bf_val else None

    return {
        'bmi': b,
        'bodyfat': {'value': bf_val, 'source': bf_src},
        'lean': lm,
        'calories': {
            'bmr': base_cal['bmr'], 'formula_tdee': base_cal['tdee'],
            'maintenance': maintenance, 'target': target, 'source': cal_source,
        },
        'adaptive': adapt,
        'macros': macros,
        'fiber': fiber_target(target),
        'hydration': hydration_target(weight),
        'rate': rate,
        'guardrail': guard,
        'adjustment': adj,
        'meals': meal_distribution(macros['protein_g'] if macros else None),
        'diet_break': diet_break_plan(deficit_weeks, goal),
        'protein_target': protein_target(weight, goal),
    }


# 供 fitness_agent 内部复用（避免循环 import）
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


def calorie_target(weight, height, age, gender, bodyfat, goal):
    try:
        w, h, a = float(weight), float(height), float(age)
    except Exception:
        return None
    if w <= 0 or h <= 0 or a <= 0:
        return None
    bmr = (10 * w + 6.25 * h - 5 * a - 161) if gender == '女' else (10 * w + 6.25 * h - 5 * a + 5)
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
        target = tdee - 400
    elif '增肌' in goal:
        target = tdee + 250
    else:
        target = tdee
    return {'bmr': int(bmr), 'tdee': int(tdee), 'target': int(target)}


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
