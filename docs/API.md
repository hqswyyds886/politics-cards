# API 接口清单

所有接口以 `http://<host>:8001` 为前缀。返回统一为 JSON（`Content-Type: application/json`）。

- 需要登录的接口从 `Authorization: Bearer <token>` 取身份。
- token 由 `/api/register` 或 `/api/login` 返回，有效期 60 天。
- 出错时返回 `{"ok": false, "error": "..."}`，HTTP 状态码 400/403/404/500。

---

## 页面

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` · `/index.html` | 返回 `static/index.html` |
| GET | `/static/<file>` | 静态文件（做了目录穿越校验） |

---

## GET 接口

### `/api/meta`
站点概要，无需登录。

```json
{"ok": true, "points": 372, "atoms": 741, "questions": 1736,
 "years": [2022, 2023, 2024, 2025, 2026, 2027],
 "subjects": ["思修", "选择题方法", "马原", "史纲", "固定搭配", "毛中特", "当代", "曲艺模板", "表格重排", "大题模板", "分析题", "时政"],
 "sources": ["苏一300题", "徐涛10套卷*", "苏一800题"],
 "daily_new": 20}
```

### `/api/settings`
读取当前用户设置。**需登录**。

```json
{"ok": true, "daily_new": 20}
```

### `/api/today`
今日学习队列：应复习的卡片 + 新卡。**需登录**。

```json
{"ok": true, "cards": [{"id": 4334, "subject": "马原", "chapter": "第一部分 · 背诵部分",
                        "front": "基本立场是什么？", "back": "以人民为中心"}]}
```

### `/api/learned`
已学卡片列表。**需登录**。参数：`subject`、`scope`、`q`（搜索关键词）。

### `/api/points`
知识点列表。参数：`subject`（可选，缺省=全部）。

```json
{"ok": true, "points": [{"id": 366, "subject": "马原",
                         "chapter": "第一部分 · 背诵部分",
                         "front": "马克思主义的基本立场、基本观点和基本方法是什么？", "back": "..."}]}
```

### `/api/quiz`
组一套练习题。参数：`subject` / `year` / `type`（`single`|`multi`）/ `source` / `n`（题数）/ `ids`（逗号分隔的题目 id，直接指定）。

```json
{"ok": true, "questions": [{"id": 1090, "year": 2027, "num": 4, "qtype": "single",
                            "subject": "马原", "stem": "...", "options": {...}, "answer": "..."}]}
```

### `/api/questions`
分页浏览真题。参数：`year` / `type` / `source` / `q` / `page`（1 起）/ `size`（默认 20）。

```json
{"ok": true, "total": 30, "page": 1, "pages": 2, "size": 20, "questions": [...]}
```

### `/api/stats`
学习统计。**需登录**。

```json
{"ok": true, "learned": 0, "mastered": 0, "questions_total": 1736,
 "questions_done": 0, "accuracy": 0, "by_subject": []}
```

### `/api/curve`
遗忘曲线 + 未来 7 天到期分布。**需登录**。

```json
{"ok": true, "upcoming": [0,0,0,0,0,0,0],
 "labels": ["今天", "1-3天", "3-7天", "7-21天", "21天+"],
 "dist": [0,0,0,0,0], "retention": 0, "streak": 0}
```

### `/api/point_questions`
某个知识点关联的真题。参数：`point_id`。

### `/api/point`
单个知识点详情。参数：`id`。

### `/api/wrong`
错题本。**需登录**。

### `/api/dati`
背大题：取一轮挖空题。参数：`n`（每轮句数，默认 20）、`subject`。

```json
{"ok": true, "quiz": [{"pid": 887, "chapter": "35 题 · 新思想",
                       "title": "十五五规划专题模板", "stem": "制定和实施五年规划..."}]}
```

### `/api/dati_wrong`
大题错句本。**需登录**。

### `/api/favorites`
收藏的真题。**需登录**。

### `/api/issues`
卡片报障列表。参数：`status`（默认 `open`）。

### `/api/tts`
语音朗读（Edge / 讯飞 / Azure）。参数：`text`、`voice`、`rate`。返回 `audio/mpeg` 流。未配置对应 provider 时返回 503。

---

## POST 接口

请求体均为 `application/json`。

### `/api/register`
```json
{"nick": "your_name", "pw": "your_password"}
```
→ `{"ok": true, "token": "...", "nick": "your_name", "uid": 1}`

### `/api/login`
```json
{"nick": "your_name", "pw": "your_password"}
```
→ 同 register。

### `/api/review`
卡片复习打分（SM-2）。**需登录**。
```json
{"point_id": 366, "grade": 3}
```
`grade` 取 0–5，<3 视为遗忘（lapses+1）。

### `/api/answer`
提交真题答案。**需登录**。
```json
{"question_id": 1090, "chosen": "C"}
```
→ 返回是否正确，以及正确答案与解析。

### `/api/reason`
标记错题原因。**需登录**。
```json
{"question_id": 1090, "reason": "base"}
```
`reason` 枚举：`base`（基础不牢）/ `trans`（看不懂换说法）/ `match`（材料没对应）。

### `/api/fav`
收藏 / 取消收藏。**需登录**。
```json
{"question_id": 1090}
```

### `/api/issue`
卡片报障。**需登录**。
```json
{"kind": "card", "ref_id": 4334, "ref_text": "...", "note": "答案疑似有误"}
```

### `/api/dati_wrong`
大题错句登记 / 移除。**需登录**。
```json
{"op": "add", "pid": 887, "sentence": "...", "stem": "...", "title": "...", "chapter": "..."}
```

### `/api/dati_edit`
直接改大题答案。**需登录**。
```json
{"pid": 887, "back": "新的答案文本"}
```

### `/api/settings`
保存每日新学数量。**需登录**。
```json
{"daily_new": 30}
```

### `/api/fitness`
健身模块总入口，按 `op` 分派。**需登录**。需要配置 `DEEPSEEK_KEY`，否则返回 503。

| op | 主要字段 |
|---|---|
| `profile` | `nickname`、`category`、`weight`、`kg` |
| `plan` | `profile`（档案 JSON）|
| `log` | `exercise`、`weight`、`reps`、`sets`、`rpe` |
| `weight` | `d`、`kg` |
| `intake` | `d`、`kcal`、`protein` |

### `/api/dev`
网页内让 AI 改自己的代码。**需登录**，需要 `DEEPSEEK_KEY`。

```json
{"op": "edit", "instruction": "把底部导航第二个 tab 文案改成「刷题」"}
{"op": "rollback", "file": "static/index.html"}
```

`op` 缺省为 `edit`。改动前自动写 `.bak`，后端改动会自动 `systemctl restart $APP_SERVICE`。
