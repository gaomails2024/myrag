# MyRag · 个人知识库

在微信里读到好文章、刷到不错的视频，**转发给 WorkBuddy，说一声 `MyRag`** —— 它会自动抓正文与配图、
判类归档。之后你在本地工作台里能按意思搜到它、按分类浏览、点开跳回原文。

不联网上传、不要账号、不要订阅：正文、配图、向量、数据库全部在你自己的机器上。

> **一句话：微信是入口，Agent 是大脑，本地是仓库。**

---

## 1. 它是什么，以及它不是什么

### 它是「Agent 原生」应用，不是独立的网站

```
微信转发（公众号文章 / 视频号）
      ↓
WorkBuddy（Agent）+ MyRag Skill          ← 入口与调度都在这
      ↓
本地后端（FastAPI + SQLite + BGE-M3）      ← 存储、切段、向量化、检索
      ↓
工作台 http://127.0.0.1:8765/             ← 取阅端：搜索 / 浏览 / 改类 / 看详情
```

**入口在微信，不在浏览器。** 所以你**必须先有一个能接收微信转发的 Agent 客户端**
（当前是 WorkBuddy，微信（4.1.13以上）分享面板里能直接选到）。这是产品形态，不是安装细节——没有它就没有入口。
<img width="1648" height="1524" alt="image" src="https://github.com/user-attachments/assets/bff9ab8e-fbc2-4ab3-a322-db31711ff9b1" />
<img width="2852" height="1486" alt="image" src="https://github.com/user-attachments/assets/1d7f48d5-b0d5-4af5-b588-3afb565ae2cf" />

### 它不是什么

| 不是 | 说明 |
|---|---|
| 不是纯网页收藏站 | 工作台是**取阅端**；网页里的「手工补录」是兜底，不是主路径 |
| 不是云服务 | 无账号、无同步、无多端。数据只在本机 |
| 不是通用抓取器 | 只处理微信公众号文章与视频号 |
| 不是开箱即用的 AI 产品 | 判类/摘要/概念抽取依赖 Agent 的 LLM。**不接 Agent，它就是一个带语义检索的收藏夹** |

---

## 2. 前置条件

| 项 | 要求 |
|---|---|
| **Agent 客户端** | **WorkBuddy**（必需），没有它就没有微信入口 |
| 微信 | 4.1.13 以上的桌面客户端，可转发到应用 |
| Python | 3.11+（开发环境 3.12） |
| 磁盘 | 约 5 GB（其中 BGE-M3 权重占 2–4 GB） |
| 网络 | 首次需能访问 ModelScope 下载权重；之后可完全离线运行 |
| 视频号（可选） | 需要腾讯元宝账号，见 §5 |

---

## 3. 安装

```bash
git clone <仓库地址> ~/MyRag
cd ~/MyRag
bash scripts/install.sh
```

`install.sh` 会做四件事：

1. 检查 Python 版本（要 3.11+）
2. 建 `.venv` 并安装依赖（torch 首次下载较慢）
3. 把仓库里的 `skill/` 部署到 `~/.workbuddy/skills/MyRag/`，**并把项目实际路径写进 SKILL.md**
4. 生成 `.env`（已存在则不动）

> 想装到别的位置也行：`git clone` 到任意目录，脚本会自己推导项目根。
> 想只更新 Skill：`bash scripts/install.sh --skill-only`

### 启动与停止

```bash
bash scripts/start.sh     # 启动（按需启动，不会开机自启）
bash scripts/status.sh    # 看状态
bash scripts/stop.sh      # 停止
```

启动后打开 **http://127.0.0.1:8765/**

> 首次启动会自动下载 BGE-M3 权重（2–4 GB），日志在 `data/server.log`。

---

## 4. 怎么用

### 主路径：微信转发

1. 在微信里点开文章（或视频号）→ **转发 → 选 WorkBuddy**
2. 对它说 **`MyRag`**

Agent 会依次：算规范化链接查重 → 抓正文与配图 → 判类 → 生成摘要/标签/概念 → 入库 → 给你一份逐条结果清单。

### 备用路径：批量粘贴

一次把一堆链接贴给 WorkBuddy，同样说 `MyRag`，会批量处理（已存在的会自动跳过，不重复抓取）。

### 工作台里能做什么

| 模块 | 用途 |
|---|---|
| 概览 | 各分类计数、近 7 日入库趋势、库体量 |
| 分类页签 | 按类浏览；点整行进详情（详情内有「打开原文」） |
| 待复核 | 判类没把握、或需要复评的条目，可一键改类 |
| 检索 | 按意思搜（语义）/ 关键词搜，两路融合 |
| **分类设置** | 改分类名、增删分类、改判类标准、改检索方式（详见 §6.2） |
| 手工补录 | 主路径失败时，粘贴正文兜底入库 |

---

## 5. 视频号（可选，但很重要）

**视频号没有公开接口**，转写要走腾讯元宝这个窗口，所以这条链路躲不开。它依赖一个第三方 Skill：

```
视频号链接（weixin.qq.com/sph/...）
    ↓
video-transcript Skill（第三方，本地 FunASR 转写）
    ├─ sph_resolver.py  → 读元宝登录态：~/.workbuddy/credentials/yuanbao_state.json
    └─ 本地 ASR         → 产出 转写稿 / 预整理稿
    ↓
回到 MyRag 的入库流程（标记为「视频转录」，进待复核）
```

### 首次配置

```bash
# 1. 安装上游 Skill（自带 install.sh）
#    见 video-transcript 项目文档
# 2. 扫码登录元宝（只需一次）
python3 <video-transcript>/scripts/sph_resolver.py --login
```

### 登录态失效怎么办

元宝登录态会过期。转写报错时先检查：

```bash
ls -la ~/.workbuddy/credentials/yuanbao_state.json
```

失效就重新执行 `sph_resolver.py --login` 扫码。

> 上游还提到一个 `video-download`（下载原视频用）。本机未安装它；若上游流程提示缺它，按上游文档补装。

---

## 6. 配置

### 6.1 环境变量与 `.env`

复制 `.env.example` 为 `.env` 后按需修改（不改也能跑，全都有默认值）。
优先级：**环境变量 > `.env` > 代码默认值**。

| 变量 | 默认 | 说明 |
|---|---|---|
| `MYRAG_HOST` | `127.0.0.1` | 监听地址。改 `0.0.0.0` 可局域网访问（**无鉴权，别暴露公网**） |
| `MYRAG_PORT` | `8765` | 端口。被占用时改这里，前端会自动跟随（同源） |
| `MYRAG_DATA_DIR` | `<项目根>/data` | 数据总目录（库 + 原文 + 配图） |
| `MYRAG_DB_PATH` | `<数据目录>/wxk.db` | 数据库文件 |
| `MYRAG_STAGE_DIR` | `<数据目录>/_stage` | 抓取落地区 |
| `MYRAG_MODEL_PATH` | 自动探测 | BGE-M3 权重目录（手动指定优先级最高） |
| `MYRAG_MODEL_SOURCE` | `modelscope` | 权重下载源 |
| `MYRAG_FETCH_SCRIPT` | 自动探测 | `wx_fetch.py` 路径（一般不用填） |
| `GITHUB_TOKEN` | 无 | 可选，只为提高 GitHub 核查的 API 限额 |

临时覆盖不用改文件：

```bash
MYRAG_PORT=9000 bash scripts/start.sh
```

### 6.2 分类完全可配置

分类**不写死在代码里**，存数据库、在工作台「分类设置」里改。每类可配：

| 项 | 说明 |
|---|---|
| 显示名 | 界面与 Agent 回报都用它 |
| 编号前缀 | 只影响**之后**生成的新编号，已有 id 不变 |
| 划分标准 | 注入 Skill，Agent 据此判类——**你想怎么分就怎么写** |
| 检索方式 | `走 RAG`（切段+向量化）/ `直接全文`（不建索引，省算力，点开读全文） |
| 能力项 | GitHub 核查 / 概念互链 |
| 默认分类 | 判不准时落哪一类 |
| 顺序 | 页签顺序 |

出厂默认三类：**开源项目 / 底层技术 / 理念思考**。数量、名字都可改。

> 分类下还挂着文章时不允许删除（不会静默替你迁移数据）。
> 改检索方式/能力项只影响**之后**入库的文章；要让存量跟着变，跑 `scripts/reindex.py`。

---

## 7. 目录结构

```
MyRag/
├── PRD.md                 # 设计文档（要改架构/数据模型时先看它）
├── README.md
├── requirements.txt
├── .env.example
├── server/                # 后端
│   ├── app.py             # FastAPI 路由
│   ├── config.py          # 配置（环境变量 / .env）
│   ├── schema.sql         # 表结构
│   ├── db.py              # 连接与通用查询
│   ├── ingest.py          # 入库：落盘 + 切段 + 向量化
│   ├── embed.py           # BGE-M3 封装
│   ├── search.py          # 两路检索 + RRF 融合
│   └── verify.py          # GitHub 核查
├── web/index.html         # 工作台（单文件，无构建）
├── skill/                 # Skill 包（安装脚本部署到客户端）
│   ├── SKILL.md           # 给 Agent 的指令（含 {{MYRAG_HOME}} 占位符）
│   └── scripts/wx_fetch.py# 抓取脚本，零第三方依赖
├── scripts/
│   ├── install.sh         # 安装
│   ├── start.sh / stop.sh / status.sh
│   └── reindex.py         # 重建分段与索引
└── data/                  # 运行数据（不进版本库）
    ├── wxk.db             # SQLite
    ├── raw/               # 原文 Markdown（只写不改）
    ├── media/             # 配图
    └── _stage/            # 抓取落地区（入库后清理）
```

---

## 8. 常见问题

**工作台打不开**
```bash
bash scripts/status.sh     # 看后端在不在
bash scripts/start.sh      # 不在就起
```
服务是**按需启动**的，重启电脑后不会自动恢复——这是有意设计（单人低频工具，不做系统级常驻）。

**端口被占**：改 `.env` 里的 `MYRAG_PORT`，然后 `start.sh`。前端同源，不用改任何代码。

**首次启动等很久**：在下 BGE-M3 权重，看 `data/server.log` 确认进度。

**权重下不来**：手动下载后用 `MYRAG_MODEL_PATH` 指向权重目录，即可完全离线运行。

**视频号转写报错**：先看元宝登录态是否失效（§5）。

**判类不准**：去「分类设置」改那一类的**划分标准**——Agent 判类读的就是它。

**想换分类名/增删分类**：同上，「分类设置」里改，改完页签和 Agent 判类同步生效。

**数据在哪 / 怎么备份**：全在 `data/`。备份就是拷这个目录（数据库用 `sqlite3 wxk.db ".backup ..."` 更稳）。

**怎么重来**：删掉 `data/` 就是一个全新的库（分类会在下次启动时重新播种）。

---

## 9. 设计文档

`PRD.md` 是完整的实现规格：数据模型、切段规则、检索融合、API 契约、验收标准、
以及各项设计取舍的原因（比如「抓取为什么放在 Skill 侧」）。要改架构先读它。

---

## 10. 许可证

[MIT](LICENSE) © 2026 JL

用到的第三方组件各自遵循其原许可证，例如：

- 抓取脚本 `skill/scripts/wx_fetch.py`：本项目原创，仅用 Python 标准库
- 视频号转写依赖的 `video-transcript`：**独立第三方项目**，按其自身许可证使用（见该仓库 LICENSE）
- BGE-M3 权重：遵循其模型许可（ModelScope / BAAI）
