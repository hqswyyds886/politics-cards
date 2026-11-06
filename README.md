# 政治卡片 · Politics Cards

一个**零依赖、单机可跑**的考研政治刷题站。后端只有 `server.py` 一个文件（Python 标准库 `http.server` + `sqlite3`），前端只有 `static/index.html` 一个文件（原生 JS，无框架、无构建）。

覆盖：**知识点挖空复习 + 历年真题刷题 + 背大题挖空 + 错题本 + 间隔重复调度**。

```
题库规模（种子库）
  知识点卡片 atoms      741 张
  复习条目  points      372 条
  真题      questions   1736 题（单选 902 / 多选 834，2022–2027）
  题卡关联  q_links     1692 条
```

---

## 为什么是这个形态

| 取舍 | 选择 | 原因 |
|---|---|---|
| 后端框架 | 标准库 `http.server` | 一台 2C2G 小机器就能跑，`pip install` 完全不需要 |
| 数据库 | SQLite 单文件 | 备份 = 复制 `data/kaoyan.db`；不需要运维数据库 |
| 前端 | 单个 HTML | 不用 Node、不用打包，改完刷新即生效 |
| 调度算法 | SM-2 简化版 | 按 `ease / interval / due` 三列自己算，不依赖外部服务 |

---

## 快速开始

### 本地跑（3 条命令）

```bash
git clone <你的仓库地址> politics-cards
cd politics-cards
python3 server.py            # 默认 8001 端口
```

打开 <http://127.0.0.1:8001/>，注册一个账号即可开始。

> 首次启动会自动建 `data/kaoyan.db`。想直接用自带题库，先执行：
> ```bash
> mkdir -p data && cp seed/kaoyan.db data/kaoyan.db
> ```
> `seed/kaoyan.db` 是**已清空用户数据**的题库种子（账号/答题记录/错题全为空）。

### 部署到云服务器（一条命令）

```bash
sudo APP_DIR=/opt/politics-cards PORT=8001 bash deploy/install.sh
```

脚本做四件事：拷源码 → 装 systemd 单元 `politics-cards` → 放行 ufw 端口 → 启动并自检 `/api/meta`。

想挂 80 端口走域名，再用 `deploy/nginx.conf.example`：

```bash
sudo cp deploy/nginx.conf.example /etc/nginx/sites-available/politics-cards
sudo ln -s /etc/nginx/sites-available/politics-cards /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

---

## 目录结构

```
politics-cards/
├── server.py               # 后端全部逻辑（路由 / 鉴权 / 调度 / 静态托管）
├── fitness_agent.py        # 可选：AI 生成健身计划
├── nutrition_engine.py     # 可选：饮食热量/蛋白核算
├── dev_agent.py            # 可选：网页里下指令让 AI 改自己的代码
├── traffic_guard.py        # 可选：按流量计费机器的日流量护栏
├── static/index.html       # 前端全部（单文件，原生 JS）
├── seed/kaoyan.db          # 题库种子（无用户数据）
├── deploy/
│   ├── install.sh              # 一键安装
│   ├── politics-cards.service  # systemd 单元
│   └── nginx.conf.example     # Nginx 反代示例
└── docs/
    ├── API.md              # 接口清单
    └── DESIGN.md           # 数据结构与调度算法说明
```

---

## 功能一览

**底部四个 tab**

| Tab | 内容 |
|---|---|
| 📖 书桌 | 今日计划（新学 20 张 + 复习上限 120 张）、翻卡复习、背面挖空、遗忘曲线与未来 7 天到期柱状图 |
| 📝 真题 | 按年份/学科/题型筛选，单题作答、批量作答、交卷回顾 |
| 🔁 错题 | 答错的题自动入库，可按原因归类（基础不牢 / 看不懂换说法 / 材料没对应） |
| 👤 我的 | 学习统计、正确率、学科分布、连续天数、主题设置 |

**背大题**：独立模块，按句切分、每轮 20 句、每次只挖一句，三层匹配兜底，绝不输出未挖空的题。

**卡片工具箱**：每张卡底部有 `🚩这张卡有问题` / `✏️直接改` / `⚡改代码` 三个入口，改完可回滚。

---

## 配置（全部走环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `KAOYAN_PORT` | `8001` | 监听端口 |
| `KAOYAN_DAILY_NEW` | `20` | 每天新学卡片数 |
| `KAOYAN_DAILY_REVIEW` | `120` | 每天复习上限 |
| `TRAFFIC_STATE` | `data/traffic.json` | 流量计数落盘位置 |
| `DEEPSEEK_KEY` | 空 | **可选**。留空则 AI 相关功能自动降级关闭 |
| `TTS_PROVIDER` | `edge` | `edge` / `xfyun` / `azure`，语音朗读卡片 |
| `AZURE_TTS_KEY` / `AZURE_TTS_REGION` | 空 | Azure 语音 |
| `XF_APPID` / `XF_APIKEY` / `XF_APISECRET` | 空 | 讯飞语音 |
| `APP_ROOT` | 脚本所在目录 | dev_agent 检索代码的根目录 |
| `APP_SERVICE` | `politics-cards` | dev_agent 重启时用的 systemd 服务名 |

**不配任何 API Key，站点也能完整使用**——刷题、复习、统计、错题全在本地 SQLite 里跑。AI 只是可选的锦上添花。

---

## 数据与备份

所有状态都在 `data/kaoyan.db` 一个文件里。

```bash
# 备份
cp data/kaoyan.db data/kaoyan.db.bak.$(date +%Y%m%d)
# 恢复
cp data/kaoyan.db.bak.20260101 data/kaoyan.db && systemctl restart politics-cards
```

`data/` 整个目录已在 `.gitignore` 中，不会进版本库。

### 从旧库平滑升级

`server.py` 的 `init_db()` 用 `CREATE TABLE IF NOT EXISTS`，并对老库做 `ALTER TABLE ... ADD COLUMN` 兜底（`questions.num`、`attempts.reason`）。直接换 `server.py` 重启即可，无需迁移脚本。

---

## 安全说明

- 密码用 `pbkdf2_hmac('sha256', pw, salt, 120000)` 存储，不存明文。
- 会话 token = `base64(uid.ts + hmac_sha256(secret, uid.ts)[:32])`，密钥在首次启动时随机生成到 `data/.secret`（权限 600，不进版本库）。token 有效期 60 天。
- 静态文件做了目录穿越检查（`abspath` 前缀校验）。
- 内置按 IP 的日流量护栏：单 IP 超 100 MB 拒绝、全站超 1 GB 进入硬限流。这是给**按流量计费**的机器兜底用的，不想用直接把 `traffic_guard.py` 删掉，`server.py` 会自动降级成一个空实现。

---

## 关于题库内容

`seed/kaoyan.db` 里的题目整理自公开的考研政治历年真题与常见复习资料，仅用于**个人学习**。如果其中包含你认为不应以 MIT 协议分发的内容，请开 issue 指出具体条目，我会从种子库中移除。

代码本身（`server.py` / `static/index.html` / agents）是 MIT。

---

## License

[MIT](LICENSE)
