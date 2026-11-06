# 设计说明

## 1. 为什么是「单文件后端 + 单文件前端」

这个项目的目标机是一台 **2 vCPU / 2 GiB、按流量计费**的小云主机。在这个约束下：

- 不引入 Web 框架 → 省掉依赖安装、虚拟环境、版本冲突
- 不引入 ORM → SQLite 直连，SQL 就写在 handler 里
- 不引入构建工具 → `index.html` 改完刷新即生效，不需要 `npm run build`
- 不引入 Redis/消息队列 → 所有状态就是 `data/kaoyan.db` 一个文件

代价是耦合度高、单文件偏大（`server.py` ≈ 2300 行，`index.html` ≈ 3300 行）。对一个人的学习工具来说，这个取舍是划算的：**依赖越少，三年后还跑得起来的概率越高**。

---

## 2. 数据模型

七张核心表 + 若干功能表。核心关系：

```
points (知识点/复习条目)
  └─ atoms (卡片：points 按段落切成更小的背记单元，src_point 指回 points)
        └─ atom_reviews (每张卡 × 每个用户的 SM-2 调度状态)

questions (真题)
  └─ q_links (题 ↔ 知识点 的多对多关联，带 score 权重)
  └─ attempts (每用户每次作答记录)

users
  ├─ daily (每日新学/复习计数)
  ├─ favorites (收藏的真题)
  ├─ dati_wrong (大题挖空答错的句子)
  └─ issues (卡片报障)
```

### 一次内容演进的痕迹

`points` 是最早的结构（一张卡 = 一条知识点），后来发现「一张卡太长、背不住」，于是有了 `atoms`：把 `points.back` 按句切成多条短卡，`src_point` 指回原条目。

现在前端**主要用 atoms**，`points` 退化为「大题模板」的载体（`曲艺模板`、`大题模板`、`分析题` 这些 subject 走 `points` + `/api/dati`）。

`reviews` 和 `atom_reviews` 并存也是这个原因：前者是 points 时代的调度表，后者按 atoms 走。新代码只写 `atom_reviews`。

---

## 3. 间隔重复调度

SM-2 的简化版，只用三列：`ease`（难度因子）、`interval`（间隔天数）、`due`（下次到期时间戳）。

```python
def schedule(grade, ease, interval):
    """grade: 0=忘了 1=模糊 2=记得。返回 (新ease, 新interval天数)。"""
    ease = max(1.3, min(3.0, ease))
    if grade == 0:
        return max(1.3, ease * 0.80), 0.0           # 立即排回待复习
    if grade == 1:
        new_i = max(1.0, interval * 1.2)
        return ease, min(new_i, 120)
    new_i = 1.0 if interval < 1 else interval * ease
    return min(3.0, ease + 0.05), min(new_i, 180)
```

要点：

- **三档打分**而不是 SM-2 原版六档。手机上点六档太累，三档（忘了/模糊/记住）的交互成本足够低，精度损失对个人学习场景可接受。
- `ease` 的夹紧区间是 `[1.3, 3.0]`，保证间隔衰减不会失控，也不会增长过快。
- **答对时 `interval` 有上限 180 天，答模糊时 120 天**。没有这个上限，`ease` 连乘几十次后间隔会飘到几年，卡片实际上就「消失」了。
- 新卡 `interval < 1` 时，第一次答对直接给 1 天（`new_i = 1.0`），而不是 `0 * ease = 0`——这是个容易踩的坑，会让新卡永远停在 0 天。
- `grade=0` 时 `interval` 归零，`due` 会被设成「当前时间 + 300 秒」，也就是 5 分钟后重新出现，而不是立刻：给用户一个喘息的间隔。
- **统计口径**：同一张卡当天只计一次（用 `atom_reviews.last_ts` 判断是否当天）。否则小测重考会把「今日已学」的数字刷高。

### 记忆留存估算

`/api/curve` 里用指数衰减估算当前记忆强度：

```python
keep = exp(-t / (interval * 2.2))
```

`t` 是「距上次复习过去多少天」，`interval * 2.2` 是衰减时间常数。这是 Ebbinghaus 曲线的工程近似，不是精确模型，但足以驱动那条曲线图。

---

## 4. 大题挖空怎么保证「绝不输出未挖空的题」

大题模板是一整段标准答案，学习方式是**逐句挖空**：整段作为上下文，只挖掉其中一句，让你对着上下文回忆那句原话。

`build_dati_quiz()` 在 `points.back` 上操作：

1. `split_sentences()` 按 `。；
` 切句，句子数 < 2 的模板直接跳过
2. 长度 < 16 字的句子不作为挖空目标（太短没有区分度）
3. 对每个目标句调用 `blank_out(full, target)`，在原文里把这一句替换成 `＿＿＿＿＿＿＿＿`

`blank_out()` 是**三层匹配兜底**，因为原文和目标句可能因为切分方式产生细微差异：

| 层 | 做法 | 失败后的含义 |
|---|---|---|
| ① 直接 `str.find(target)` | 原样匹配 | — |
| ② 去标点后匹配 | 把原文和目标都剥掉 `空格。；，,！？、`，在「去标点版」里找位置，再用索引映射回原文下标 | 目标去标点后 < 8 字则放弃 |
| ③ 取前 20 字匹配 | 用 `core[:20]` 再找一次 | 三层都找不到 → 返回 `None` |

**关键的守卫**：

```python
stem = blank_out(r['back'] or '', target)
if not stem or BLANK not in stem:
    continue          # 挖不出来就跳过，绝不放出没挖空的题
```

即使 `blank_out` 出了意料之外的 bug 返回了一个不含空位的字符串，第二重 `BLANK not in stem` 也会把这道题挡掉。**宁可少一道题，也不给用户看一道没挖空的题**——这就是「绝不输出未挖空题」在代码里的落点。

### 干扰项怎么来

不是人工写的，是从**句子池**里捞：

- 池子 = 所有模板的所有句子
- 优选长度接近的（`abs(len(s) - len(target)) <= max(10, len(target) // 3)`）
- 洗完牌取 3 个凑成 4 选项；不够就放宽到全池长度 ≥ 12 的句子
- 连 3 个选项都凑不齐（`len(opts) < 3`）→ 跳过这道题

正确答案的位置 `answers` 是在洗牌后重新计算下标得到的，所以选项顺序每次刷新都不同。

## 5. 鉴权

无状态 token，不用 session 表：

```
token = base64url( f"{uid}.{ts}.{hmac_sha256(secret, f'{uid}.{ts}')[:32]}" )
```

- `secret` 首次启动随机生成 32 字节写到 `data/.secret`（权限 600）
- 校验用 `hmac.compare_digest`（常量时间，防时序侧信道）
- 有效期 60 天，过期即失效
- **无刷新机制**：60 天后重新登录。对个人工具够用，省掉 refresh token 的复杂度

密码：`pbkdf2_hmac('sha256', pw, salt, 120000)`，每用户独立随机 salt。

---

## 6. 流量护栏

按流量计费的机器最怕被刷。`traffic_guard.py` 的阈值：

| 阈值 | 值 | 行为 |
|---|---|---|
| `DAILY_WARN` | 200 MB | 仅记录 |
| `DAILY_LIMIT` | 500 MB | 进入限流态 |
| `DAILY_HARD` | 1 GB | 直接拒绝非白名单 |

外加单 IP 超过 100 MB 直接封。

**降级设计**：`server.py` 里用 `try: from traffic_guard import guard / except: 用空实现`。删掉这个文件服务照样跑，只是没有护栏。这是刻意的——护栏是给特定部署环境用的，不该成为硬依赖。

状态落在 `data/traffic.json`，每日滚动清零。

---

## 7. 前端组织

单文件，但内部按 `/* ---------- 区块名 ---------- */` 分段：

```
header        → 常量、状态 S、工具函数 el()/api()/esc()
登录          → renderLogin()
书桌          → renderStudyView() / renderFlipCard()
学习          → renderLearn() / learnBar()
真题          → renderQuestion() / renderExam()
错题          → renderWrong()
我的          → renderCurve() / 统计
健身饮食      → openFitness() / openNutrition()
大题          → renderDatiQuiz() / renderDatiWrongList()
卡片工具箱    → 报障/改卡/改代码
```

这个分段格式不是随手写的——`dev_agent.py` 的 `html_blocks()` 正是按这个正则切块，用来在 AI 改代码时只把**相关区块**塞进 prompt，控制 token 消耗。所以改前端时**保留 `/* ---------- xxx ---------- */` 注释**，否则 AI 改代码功能会变差。

状态全在全局对象 `S` 上（`S.token` / `S.queue` / `S.qi` / `S.picked` ...），没有状态管理库。路由靠 `data-tab` 属性切换，没有前端路由表。

---

## 8. 可选 AI 能力

三个模块都依赖 `DEEPSEEK_KEY`，**不配就降级关闭**：

| 模块 | 功能 | 未配置时 |
|---|---|---|
| `fitness_agent.py` | 根据身体数据生成训练计划 | `/api/fitness` 返回 503「未配置 AI Key」 |
| `nutrition_engine.py` | 饮食热量/蛋白核算（本地计算为主） | 可独立使用 |
| `dev_agent.py` | 网页里下指令让 AI 改自己的代码 | `/api/dev` 返回 503 |

`dev_agent.py` 的设计值得单独说：它**只做 old/new 字符串替换，不整文件重写**。因为 `index.html` 有 100 KB+，让模型整文件重写一定会把代码搞坏。改前自动写 `.bak`，出错可 `op=rollback` 回滚。

---

## 9. 已知取舍与后续方向

**当前没做的**：

- 没有多用户隔离的权限体系（只有「登录了/没登录」两态，不做管理员角色）
- 没有数据库迁移框架，靠 `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE` 兜底
- 没有测试套件（验证靠手工跑接口）
- 前端没有组件化，`index.html` 会继续变大

**如果重做会改的**：

1. 把 `points` / `atoms` 合并成一张 `cards` 表，少一层间接
2. 后端拆成几个模块（auth / db / handlers / agents），2300 行单文件读起来累
3. 前端引入轻量构建（esbuild 就够），把 3300 行拆成真正的组件
4. 补一套接口层测试

这些改动都不影响当前可用性，纯粹是可维护性问题。**一个能跑的个人工具，比一个架构漂亮但没写完的项目有价值**——这是当初选择这个形态的理由，现在依然成立。
