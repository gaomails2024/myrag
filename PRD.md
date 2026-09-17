# PRD · 个人知识库

| 项 | 内容 |
|---|---|
| 版本 | v2.0 |
| 日期 | 2026-09-17 |
| 状态 | 一期已实现并通过验收（§13 共 18 条） |
| 实现方 | CodeBuddy |
| 使用者 | 单人（JL），本机使用 |
| v2.0 变更 | **从「能用」走向「可验证」，并补上配图治理**：① 新增 **§8.5 检索评估体系** —— 评测集 + IR 指标（Recall@k / MRR / nDCG）+ RAGAS 复核，作为后续所有「要不要改」的共用标尺（阈值标定、效果验证都依赖它）；② 新增 **§8.4 Reranker 精排**（`bge-reranker-v2-m3`，可开关）—— 补上 bi-encoder「语义像 ≠ 答到点上」的判断缺口；③ 新增 **§6.4 配图治理** —— 入库时按规则 + OCR 过滤无效图（小图标 / 广告 / 动图 / 无文字照片），并把 OCR 文字写入正文**让图片内容可检索**；④ 新增 **§5.6 遗忘机制** —— 明确「遗忘 = 降级（弃载体、保语义）」，与「删除 = 放弃」区分；⑤ §12.1 补 reranker 与 OCR 依赖，§12.2 补 `eval/` 目录；⑥ §15 已知风险补两条实测出来的：**向量为 sqlite-vec 暴力 KNN（无 ANN）**、**README 曾欠实现细节导致读者只能靠猜** |
| v1.7 变更 | **分类可配置（为开源分发）+ 抓取扩面**：① 分类从代码常量改为 `categories` 表驱动，新增 §5.5；出厂默认三类改名为**开源项目 / 底层技术 / 理念思考**；② 每类可配：名字、编号前缀、划分标准（注入 Skill 供判类）、**检索方式**（`rag` 走 RAG 检索 / `fulltext` 直接全文、不建索引）、能力项（`repo_card` GitHub 核查 / `concepts` 概念互链）、是否默认分类；③ 新增 `GET`/`POST /api/categories` 与 `PATCH`/`DELETE /api/categories/{key}`，工作台新增「分类设置」页；④ 入库 / 检索 / 核查 / 前端页签全部改为读配置，写死的 `config.CATEGORIES`、`config.PREFIX`、前端 `CAT` 字典已删除；⑤ **取消「理念类整行跳原文」** —— 所有分类统一「点整行进详情」（§10.2）；⑥ **抓取扩面到三类链接**（新增 §6.2.1）：微信文章 / 微信图片合辑 / 其他网页，文件直链（pdf/zip/mp4…）明确拒收并说清原因；⑦ **修 URL 归一化隐患**：原实现对任何链接都兜底成 `mp.weixin.qq.com/s`，非微信链接会被*改写成微信域名*，既查不了重也抓不到（§5.2 第 6 条）；⑧ **修验证页误判**：原按「HTML < 20KB」判定，导致图片合辑等非文章页被误报成验证页、用户往反爬方向白折腾（实测踩到）—— 改为看特征词，失败原因细分为 6 种；⑨ 工作台「值得试」结论改为**实心绿徽章 + 卡片左侧色条**（原引用已被删除的 `--tech-soft` 变量，底色一直是透明的，见 §10.2） |
| v1.6 变更 | **撤销开机自启，回到「按需启动」**（v1.5 的常驻方案已废弃）：① 删除 `scripts/autostart.sh` 与 §11.4，`status.sh` / `stop.sh` 去掉 launchd 分支，回到纯 `nohup`；② 服务**不随登录启动**，用户要看工作台时手跑一次 `scripts/start.sh` 即可；③ 唯一需要「自动拉起」的场景（调 `MyRag` Skill 时后端必须在跑）由 Skill 第 0 步承担（§11.2 第 2 条）；④ 取向说明见 §11.3 —— 单人低频工具，不为偶尔打开的服务引入系统级常驻配置 |
| v1.4 变更 | **修三处实测偏差，均不改变设计意图**：① §7.1 权重体积 `约 2.2 GB` → **实测约 4.3 GB**，并写明 `FlagEmbedding` 与 torch **本机非预装**、模型本地缓存绝对路径、`resolve_model_path()` 的四级探测顺序；② §7.2 补「微信正文标题形态」实现注记 —— 实测三篇**零个 `##`**，章节标题是 `# 01` 与整行加粗，切段实现必须认「首个标题作文章名不进链，其余各级标题（含 H1）+ 整行加粗伪标题都进链」；③ 新增 `scripts/reindex.py`（切段规则或模型变更后，按新规则重算既有文章的分段，原文与配图只读） |
| v1.3 变更 | **修三处实现阻塞**：① 系统根定为 `~/MyRag/`，§12.2 目录树与路径常量统一（此前三处写成不存在的 `微信收藏库/`）；② 辅路径由「粘贴链接」改为「粘贴正文文本」并新增 `POST /api/ingest-text`（后端不抓取，拿到链接变不出正文）；③ **编号改由后端分配**（§5.3），`/api/ingest` 请求体移除 `id`、响应返回 `id`，补幂等约定（重复 ingest 与 `url_canon` 冲突均返 `dup:true` + HTTP 200，不返 409）；连带：`/api/check-urls` 改收 `url_canon`，抓取脚本新增 `--canon-only` 模式（canon 必须能在抓取前算出），§13 验收补 16–18 条 |
| v1.2 变更 | **Embedding 定为 BGE-M3**：§7.1 由 `fastembed` + `BAAI/bge-small-zh-v1.5`（512 维）改为 `FlagEmbedding` + `BAAI/bge-m3`（1024 维，dense + sparse 双输出）；检索层由「向量 + jieba + FTS5 双组件」简化为「单模型两路 + RRF」；§5.1 新增 `chunk_sparse` 表、`chunk_vecs` 维度 512 → 1024；§7.2 切段上限 512 → 1024 token；§8.1、§12.1、§15 相应同步 |
| v1.1 变更 | **抓取从后端移到 Skill 侧脚本**：删除 `POST /api/fetch`，新增 `POST /api/check-urls`；`/api/ingest` 改为接 `stage_token` + 批注，正文与配图走 staging 磁盘副本。附 7 条实测数据与修正后的字段提取规范（§6.2） |

---

## 0. 一句话

把自己从微信里看到、转过来的文章，**判类 → 抽取 → 入库**，之后能**按意思搜到、按类别浏览、点开跳回原文**；需要核实的项目类内容，在工作台上现查现判。

---

## 1. 背景与问题

使用者每天从微信获取十几篇 AI 相关文章，长期无系统地保存。存量约 2000+ 篇（半年），增量每天十几篇。

**核心痛点（按优先级）**：

1. **存了用不上**。以前的收藏是链接孤岛，转给 AI 也没有通道，等于没存。
2. **散落在文件夹的 Markdown 没有表现力**。用户明确拒绝"一堆散落在文件夹里的 MD 文档"这种形态。
3. **三类内容的价值形态完全不同**，用一套方式存是错的。

**成功的样子**：用户贴一条链接进来，几十秒后能在工作台上按意思搜到它、按类看到它、点开跳回原文。

---

## 2. 范围

### 2.1 做

| 项 | 说明 |
|---|---|
| 输入源 | 微信文章（`mp.weixin.qq.com/s/...`）、**微信图片合辑**（分享卡片）、**其他网页**；文件直链（pdf/zip/mp4…）明确不收（§6.2.1） |
| 入库方式 | 在对话中把链接发给 Agent（主路径）；工作台粘贴**正文文本**（辅路径，手工兜底，见 §10.3） |
| 三类处理 | 应用类 / 技术类 / 理念类，三类各自有专属产物与展示形态 |
| 存储 | 本地 SQLite 单文件 + 本地原文与配图文件 |
| 检索 | 语义（dense）+ 词法（sparse），均由 BGE-M3 单模型产出，RRF 融合 |
| 界面 | 单文件 HTML 工作台，通过本地后端 API 读写 |
| 核查 | 应用类支持拉取 GitHub 仓库客观数据（star / license / 最近提交 / 发版） |

### 2.2 不做（明确排除）

| 项 | 原因 |
|---|---|
| PPT / 文档类素材 | 用户明确排除：「那个事情比这复杂多了，我不会把简单的事情放在你这个库里来处理」 |
| 知识图谱 / GraphRAG | 用户明确：「微信文章不需要用图 rag」。GraphRAG 另作储备，留给实体密集场景 |
| 云端同步 / 多端 | 本地优先。手机或外网访问不在一期范围 |
| 全自动判类无人工介入 | 判类必然有误，保留人工修正入口（见 §5.4） |
| 视频 | 用户提到「视频也有可能」，但微信是否支持转发视频未确认 → **列为二期待定** |
| RAG 问答式生成 | 一期只做检索与呈现，不做"基于库生成答案" |

---

## 3. 核心模型：两个时刻

整个系统的设计锚点——**判断与执行发生在两个不同的时刻**。

```
入库时刻（一次性，由 Skill 脚本 + Agent 完成）
  微信链接 → 抓全文+配图（Skill 脚本抓，不经过后端）→ 判类 → 抽取该类产物 → 落库（系统）

使用时刻（随时，由工作台承载）
  应用类 → 工作台，现场核查 → 反馈结论
  技术类 → 可查询文章库，带链接，点开跳转
  理念类 → 可查询文章库，带链接，点开跳转
```

**判类只在入库时刻发生一次**，此后永不重判（除非人工修正）。**检索与核查在使用时刻发生**，读同一份数据。

---

## 4. 系统架构

```
┌─ 入库侧（Skill 脚本抓取 + Agent 判断） ─────────────┐
│  微信链接 → 抓正文+配图（Skill 侧脚本，直连微信）     │
│           → 判类 + 生成摘要/标签/抽取物（Agent）      │
│           → 切段 → 向量化 → 写库（系统）             │
└───────────────────────────────────────────────────┘
                        ↓
┌─ 存储层 ──────────────────────────────────────────┐
│  SQLite 单文件（wxk.db）                           │
│    · articles      文章主表（含三类通用字段）        │
│    · chunks        分段表                          │
│    · chunk_vecs    sqlite-vec 稠密向量（1024 维）    │
│    · chunk_sparse  sparse 词权重（BGE-M3 输出）     │
│    · app_cards     应用类专属字段                  │
│    · concepts      技术类概念                      │
│  + data/raw/       原文 Markdown（底本，只写不改）   │
│  + data/media/     配图原图                        │
└───────────────────────────────────────────────────┘
                        ↕
┌─ 服务层 ──────────────────────────────────────────┐
│  FastAPI 本地后端  http://127.0.0.1:8765          │
│    · 入库接口  · 检索接口  · 核查接口  · 修正接口    │
└───────────────────────────────────────────────────┘
                        ↕
┌─ 界面层 ──────────────────────────────────────────┐
│  web/index.html  单文件工作台                      │
│    应用核查 · 分类浏览 · 语义搜索 · 单条入库         │
└───────────────────────────────────────────────────┘
```

**关键原则**

- **原文永远是底本**。向量、词权重、抽取物全部可由原文重建；原文与配图不可丢。
- **后端不抓取、不调 LLM**。抓取由 **Skill 侧脚本**完成（Agent 侧已验证可直连微信，7/7 成功）；后端只做确定性工作（落盘、切段、向量化、检索、GitHub 核查）。
- **Skill 抓、系统存**。抓取产出的原文与配图先落到系统的 staging 目录，再由后端 ingest 归位；**Agent 读的是副本，入库用的是磁盘副本**，避免长文经过上下文时被截断。
- **本地优先**。默认监听 `127.0.0.1`，不暴露公网。

---

## 5. 数据模型

### 5.1 表结构（`server/schema.sql`）

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 文章主表：各分类通用字段
CREATE TABLE IF NOT EXISTS articles (
  id            TEXT PRIMARY KEY,          -- <分类前缀>-YYYYMMDD-NN（NN 为两位序号）
  category      TEXT NOT NULL,             -- → categories.key（分类可配置，见 §5.5）
  title         TEXT NOT NULL,
  source        TEXT,                      -- 公众号名
  author        TEXT,
  published_at  TEXT,                      -- ISO8601，来源 var ct 时间戳
  collected_at  TEXT NOT NULL,             -- ISO8601，入库时刻
  url           TEXT NOT NULL,             -- 用户贴进来的原始链接（含 mpshare/scene/srcid 等追踪参数）
  url_canon     TEXT NOT NULL UNIQUE,      -- 规范化链接：去 #fragment 与追踪参数，**唯一去重依据**
  summary       TEXT,                      -- 一句话摘要（≤60字）
  tags          TEXT DEFAULT '[]',         -- JSON array
  raw_path      TEXT,                      -- 相对 data/ 的原文 md 路径
  attachments   TEXT DEFAULT '[]',         -- JSON array，相对路径
  review_flag   INTEGER DEFAULT 0,         -- 1 = 判类待复核
  review_due    TEXT,                      -- 复评日期（带 repo_card 能力的分类必填，默认 +90 天）
  status        TEXT DEFAULT 'ok',         -- ok | failed | pending
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_articles_cat ON articles(category, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_review ON articles(review_flag, review_due);

-- 分段表
CREATE TABLE IF NOT EXISTS chunks (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  article_id   TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
  ordinal      INTEGER NOT NULL,
  heading_path TEXT,                       -- 如 "三、架构设计 > 3.2 切段策略"
  text         TEXT NOT NULL,              -- 原文片段（保留 Markdown）
  token_count  INTEGER,
  UNIQUE(article_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_chunks_article ON chunks(article_id, ordinal);

-- 稠密向量表（维度随 embedding 模型，见 §7.1；BGE-M3 = 1024）
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vecs USING vec0(
  chunk_id  INTEGER PRIMARY KEY,
  embedding FLOAT[1024]
);

-- 稀疏词权重（BGE-M3 的 sparse 输出，取代 FTS5 + jieba）
CREATE TABLE IF NOT EXISTS chunk_sparse (
  chunk_id   INTEGER PRIMARY KEY,
  article_id TEXT NOT NULL,
  weights    TEXT NOT NULL                 -- {"<token_id>": <weight>, ...}
);
CREATE INDEX IF NOT EXISTS idx_sparse_article ON chunk_sparse(article_id);

-- 评估卡（features 含 repo_card 的分类用）
CREATE TABLE IF NOT EXISTS app_cards (
  article_id    TEXT PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
  repo_url      TEXT,
  license       TEXT,                      -- SPDX id，如 Apache-2.0
  language      TEXT,
  stars         INTEGER,
  last_commit   TEXT,                      -- ISO8601
  last_release  TEXT,
  created_date  TEXT,                      -- 仓库创建日期，用于判断"年龄"
  boundary      TEXT,                      -- 能力边界（与宣传的差距）
  deploy_note   TEXT,                      -- 部署条件（能否自托管、依赖）
  verdict       TEXT,                      -- try | watch | dead
  evidence      TEXT DEFAULT '{}',         -- JSON：核实记录（含来源与时间）
  verified_at   TEXT
);

-- 概念（features 含 concepts 的分类用）
CREATE TABLE IF NOT EXISTS concepts (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL UNIQUE,
  definition  TEXT,
  article_id  TEXT REFERENCES articles(id) ON DELETE CASCADE,
  related     TEXT DEFAULT '[]'            -- JSON array，关联概念名（互链）
);

-- 入库日志（可追溯）
CREATE TABLE IF NOT EXISTS ingest_log (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  url        TEXT,
  article_id TEXT,
  action     TEXT,                         -- ingest | skip_dup | fail
  detail     TEXT,
  at         TEXT NOT NULL
);
```

### 5.2 URL 规范化

**为什么需要**：微信分享链接带 `mpshare` / `scene` / `srcid` / `sharer_shareinfo` / `#rd` 等追踪参数。同一篇文章被两个人转发，URL 字符串不同——**直接拿原始链接去重会漏，同一篇会入库两次**。

**算法**（Skill 抓取脚本执行，产出 `url_canon`）：

1. 丢弃 `#` 及其后全部内容
2. 只保留识别参数：`__biz`、`mid`、`idx`、`sn`
3. 丢弃 `mpshare` / `scene` / `srcid` / `sharer_shareinfo*` / `chksm` 及其余全部参数
4. 固定参数顺序拼回：`https://mp.weixin.qq.com/s?__biz=..&mid=..&idx=..&sn=..`
5. 短链形态 `/s/<sn>`：保留路径，只去追踪参数
6. **非微信站点**：保留原路径（去尾斜杠），只剔除 `utm_*` / `spm` / `from` / `share_*` / `fbclid` 等追踪参数

⚠️ **第 6 条是修正**：早期实现对**任何** URL 都兜底成 `mp.weixin.qq.com/s`，
非微信链接会被**改写成微信域名** —— 既查不了重也抓不到东西（v1.7 修）。

**去重一律用 `url_canon`**；`url` 只保留原始链接供用户回溯。

**计算时机（关键）**：`url_canon` 必须**在抓取之前**就能算出——否则"先查重、避免白抓"是死循环（查重在抓取前，而 canon 若依赖抓取结果就永远等不到）。因此：

- 脚本 `wx_fetch.py` 提供 `--canon-only` 模式：只读 urls 文件、纯字符串算 canon、输出 `[{url, url_canon}]`，**不联网、不抓取**
- Skill 第 1 步先用它算 canon → 提交 `/api/check-urls` → 剔除已存在的 → 再抓剩余的（§6.1）
- 抓取完成后 canon 会再算一次写进 `payload.json`；**两处必须同一函数、同一代码路径**，否则查重与入库会各用一套口径

### 5.3 编号规范

- 格式：`<前缀>-<YYYYMMDD>-<NN>`
- 前缀：由该分类的配置决定（出厂默认 `A` = 开源项目、`T` = 底层技术、`I` = 理念思考，见 §5.5）。
  **改前缀只影响之后生成的新编号**，已有 `id` 不变
- `YYYYMMDD` 取**文章发布日**（抓取时从 `var ct` 取得），不是入库日
- `NN` 为该前缀该日的两位序号，从 `01` 起
- 同一编号在三处一致：数据库 `articles.id`、原文文件名、`data/media/<id>/` 目录名

**编号由后端分配，不在 Skill 侧生成。**

原因：`NN` = 「该前缀该日已有条数 + 1」——**必须查库才能算准**。Skill 看不到库，自己编必然撞号（重跑、并发、同日连收多篇都会撞）。分配算法：

```
在写 articles 的同一事务内：
  n = SELECT COUNT(*) FROM articles
      WHERE id LIKE '<前缀>-<YYYYMMDD>-%'
  候选 = <前缀>-<YYYYMMDD>-<n+1 补零两位>
  若候选已存在（并发 / 历史缺口）→ n++ 重试，最多 50 次
  仍冲突 → 报 500，不静默改写编号
```

Skill 调 `/api/ingest` 时**不传 `id`**，只传 `category` 与 `published_at`；后端定前缀、定日期、分配序号，在响应里返回 `id`（§9）。

### 5.4 判类与兜底

**判类规则不写死在本文档**：每类的判据存在它自己的 `criteria` 字段里（§5.5），
Agent 入库时现读。开源分发后每个人的分类与判据都不同，所以规则必须跟着配置走。

| 步骤 | 谁做 | 做什么 |
|---|---|---|
| 1 | Skill | `GET /api/categories` 取全部分类的 `key` / `name` / `criteria` / `is_default` |
| 2 | Skill | 读 stage 正文，照 `criteria` 判定，把命中的 `key` 作为 `category` 提交 |
| 3 | 后端 | 校验 `key` 是否存在；不存在或未提供 → 落 `is_default=1` 的分类 |

**拿不准时的口径**（与 SKILL.md 第 3 步、Skill 侧行为保持一致）：

| 情况 | 做法 |
|---|---|
| 能与某一类 `criteria` 对上（把握不大也算） | 判给它 + `review_flag = 1`，进待复核队列等人工确认 |
| 与所有 `criteria` 都对不上 | 落 `is_default = 1` 那一类 + `review_flag = 1` |

一句话：**「拿不准」不等于「随便塞进默认类」** —— 能判就按最接近的判，标 `review_flag`
让人工兜底；否则待复核队列会被模糊内容淹没、失去筛选意义。
入库不阻塞；判错可在工作台改类，或直接点「确认无误」放行（§10.2）。

**人工修正**：工作台可改 `category` / `tags` / `summary`，改动写回库并清 `review_flag`。修正产物**不做反向迁移**（不删除已生成的详情页），只在 `ingest_log` 记录。

### 5.5 分类配置（`categories` 表）

**为什么要有这张表**：分类是使用者的个人选择，不是产品的固有设计。要开源分发，就不能把
「三类 + 三个名字 + 三套判据 + 各自的展示形态」写死在代码里。本表是分类的唯一事实来源，
工作台「分类设置」页可增删改；首次启动由 `config.DEFAULT_CATEGORIES` 播种
（**已存在则一字不动**，绝不覆盖用户改过的配置）。

| 字段 | 说明 |
|---|---|
| `key` | 稳定标识，写进 `articles.category`。**入库后不建议改**（改 = 迁移数据） |
| `name` | 显示名，如「开源项目」。只影响界面与 Skill 回报 |
| `prefix` | 编号前缀，全局唯一，**只影响之后生成的新编号** |
| `criteria` | 划分标准，注入 Skill 供 Agent 判类（§5.4） |
| `retrieval` | `rag` = 切段 + 向量化，进语义/词法检索；`fulltext` = **不建索引**（省算力），按时间浏览、点开读全文，检索时只按标题/摘要/标签做关键词兜底 |
| `features` | JSON 数组：`repo_card`（入库后可调 GitHub 核查，§6.3）、`concepts`（抽概念并互链） |
| `is_default` | `1` = 判不准时落到这一类（全局最多一个） |
| `sort_order` | 页签顺序 |

**约束与保护**：

- 格式有校验：`key` 为 2–16 位小写字母/数字/下划线且以字母开头；`prefix` 为 1–3 位字母或数字，且不可重复。
- **有文章挂着的分类不允许删除** —— 不做静默迁移（那等于替用户改数据归属），报错会说明还剩几篇。
- 至少保留一个分类。
- 改 `retrieval` / `features` **只影响之后入库的文章**；要让存量文章跟着变，需重建索引（`scripts/reindex.py`）。

**出厂默认**（仅播种用，用户可随意改）：

| key | name | prefix | retrieval | features | 判据（criteria 摘要） |
|---|---|---|---|---|---|
| `app` | 开源项目 | A | rag | repo_card | 具体的开源项目 / 产品 / 工具，或技术方案的落地实现 |
| `tech` | 底层技术 | T | rag | concepts | 底层原理 / 架构 / 方法论，不绑定具体产品（**默认分类**） |
| `idea` | 理念思考 | I | rag | — | 观点 / 趋势 / 商业判断 |

### 5.6 遗忘与删除（v2.0）—— 两个不同的动作

**这是使用者明确区分的两个概念，实现时不可混为一谈**：

| 动作 | 含义 | 后果 |
|---|---|---|
| **删除** | 彻底不要了 | 文章、正文、配图、索引全部移除 |
| **遗忘** | **降级** —— 弃载体、保语义 | 保留正文与索引，只移除**占空间的原始载体**（主要是配图） |

**为什么遗忘值得单独做**：实测配图占全库 **96%** 体积（§6.4），而图片本身
**不进检索**（能进检索的是 OCR 出的文字）。所以「删图、留文字」几乎不损失可检索性，
却能释放绝大部分空间 —— 这是**性价比最高的降级动作**。

**遗忘的判据与顺序**：

1. **只对「文字已提取成功」的图执行** —— 没提出文字的不许遗忘（否则等于真丢内容）
2. **正文里留痕**：`> 原图已遗忘（YYYY-MM-DD），文字见上`，可追溯"这里曾有过图"
3. **不可逆性要讲清**：原件删除后无法恢复，所以：
   - 首选**走系统废纸篓**（`~/.Trash/`）—— 空间立即释放、且可反悔
   - 而非原地删除、或移到项目内目录（后者磁盘并未释放）

**与状态字段的关系**：`articles.status` 现为 `ok | failed | pending`。

- `failed`（抓取失败残留）**可自动清理** —— 本来就没入库成功
- 正文**永不自动删除** —— 误删代价远高于占地方

> **三条边界**：可以自动清（failed 残留）、可以手动清（用户选定）、
> **不可以自动删正文**。

---

## 6. 入库流水线

### 6.1 步骤

职责划分：**抓取归 Skill（脚本执行），判断与编排归 Skill（Agent），落盘与切段归系统。**

```
【Skill·脚本】1. 算 canon + 查重，剔除已存在的
                · wx_fetch.py --canon-only → url_canon 列表（纯字符串，不联网）
                · POST /api/check-urls {url_canons} → 命中的记 skip_dup，不抓
             2. 对剩余链接逐条抓取（scripts/wx_fetch.py，纯 stdlib，不经后端）：
                · 正文   桌面 UA → #js_content → Markdown
                · 元数据 og:title / id="js_name" / og:article:author / var ct
                · 配图   data-src → 下载（必须带 Referer）
                · 落盘   data/_stage/<token>/payload.json + content.md + img/NN.png
             3. stdout 只回传简报：标题 / 公众号 / 发布日 / 正文字数 / 配图数
                / token / fail_reason。正文不打印到 stdout，避免撑爆上下文。

【Skill·Agent】4. 读 data/_stage/<token>/content.md 判类（§5.4）
              5. 生成 summary + tags（技术类另出 concepts；应用类准备核查）
              6. 批注写入 data/_stage/<token>/annotations.json

【系统】       7. 接收 POST /api/ingest {stage_token, category, ...} —— **不含 id**
              8. 读 stage 的 payload.json（磁盘副本＝唯一真相源）
              9. 事务内分配 id（§5.3）→ 落原文 data/raw/<年>/<id>.md（只写不改）
             10. 落配图 data/media/<id>/NN.png，正文图链改本地相对路径
             11. 切段 → 向量化 → 写库 → 清理该 stage 目录
             12. 响应 {ok, dup, id}

【Skill·Agent】13. 应用类：调 POST /api/articles/{id}/verify，然后**自行读仓库核对**
             14. 输出结果清单：入库 N / 跳过 N / 失败 N（附原因）
```

**为什么正文用磁盘副本而不是上下文副本**：Agent 判类必须读正文（长文可达 11000 字），但入库写入的必须是脚本落盘的原始文件。两件事分开，长文就不会因为经过上下文而被截断或改写。

### 6.2 抓取硬约束（已实测，由 Skill 侧 `scripts/wx_fetch.py` 实现）

**实测基准**：7 条真实微信链接，**7/7 抓取成功**；正文 1128–11156 字；配图 1–19 张；单篇耗时 1.4–34 s；配图下载 200 OK 真 PNG。**抓取不需要特殊通道**——微信公开文章对带正常 UA 的普通 HTTP 请求即开放。

| 项 | 值 | 备注 |
|---|---|---|
| UA | 桌面 Chrome UA | 实测通过 |
| 正文容器 | `#js_content` | 三级正则回退 |
| 标题 | `<meta property="og:title">`，回退 `var msg_title` | 实测两者一致 |
| **公众号** | **`id="js_name"` 标签内文本**，回退 `data-nickname="..."` 属性 | ⚠️ **没有 `var nickname` 这个变量**，按变量名提取会得到空值（实测踩过） |
| 作者 | `<meta property="og:article:author">` | ⚠️ 可能是截断名（实测某公众号名被截成前半段），**公众号名一律以 `js_name` 为准** |
| 发布时间 | `var ct`（秒级时间戳） | 实测可正确换算出发布日 |
| 配图属性 | `data-src`（不是 `src`） | 只扫 `#js_content` 内部 |
| **内嵌视频 / 卡片** | `<iframe class="video_iframe">` 与 `<mp-*>` 标签 | **不下载，但必须留痕**：视频转成正文里一行 `> [视频] <链接>`，卡片类型记入 `payload.embedded`。排版辅助标签（`mp-style-type`、`mp-common-profile`）忽略 |
| **配图下载** | **必须带 `Referer: https://mp.weixin.qq.com/`** | 否则 403 |
| 失败判据 | **看特征词，不看体积**（v1.7 修） | 见下表 |

### 6.2.1 链接类型分派（v1.7）

脚本先判链接类型再决定怎么解析，结果里的 `kind` 说明走了哪条路：

| 链接 | `kind` | 处理 |
|---|---|---|
| `mp.weixin.qq.com/s/...` 文章 | `wechat` | 解析 `#js_content`（同 §6.2） |
| **微信图片合辑**（分享卡片） | `wx_album` | 该模板**没有正文容器**，文案在 `og:description`，**图片在页面内 JS 的 `picture_page_info_list` 里、不在 HTML 标签中**；每个元素有 2 个 `cdn_url`（原图 + `watermark_info` 水印版），按元素配对切块、每块取第一个，否则图片翻倍 |
| 其他网页 | `web` | 通用正文定位：`<article>` → `<main>` → `role=main` → 纯文本最长的块（尽力而为，取不到就判失败，**不硬凑导航/页脚**） |
| 文件直链（`.pdf/.zip/.mp4`…） | — | `unsupported_file` 拒收，提示改存网页版或直接下载 |

**图片合辑的文案要清洗**：`og:description` 是**被转义过的 JS 片段**，形如
`\x0a\x26lt;a class=\x26quot;wx_topic_link\x26quot;…\x26gt;#融资成功\x26lt;/a\x26gt;`
（一层 `\xHH` 转义 + 一到两层 HTML 实体）。需逐层还原后去标签，只留纯文本。

**失败原因枚举**（用户看到的应是「怎么办」而非枚举值）：

| `fail_reason` | 含义 |
|---|---|
| `verify_page` | 真·验证/异常页（反爬）。重试或换网络，**不是链接错了** |
| `network` | 网络或 HTTP 错误 |
| `bad_url` | 非 http/https（含视频号，走 §6.1 转写链路） |
| `unsupported_file` | 文件直链，改存网页版或直接下载 |
| `not_article` | 微信域名但非文章页、也没认出图片合辑 → 让用户「用浏览器打开」后重新复制链接 |
| `parse_failed` | 抓到页面但定位不到正文（需登录或结构特殊） |

⚠️ **为什么不能按体积判验证页**：图片合辑这类非文章页的 HTML 也可能很小，
按「HTML < 20KB」判会把它们**误报成验证页**，用户于是往反爬方向白折腾
（去重扫码、换网络），而真正的问题是页面类型不认识（v1.7 修，踩过）。

**脚本 CLI 契约**：

```bash
python3 ~/.workbuddy/skills/MyRag/scripts/wx_fetch.py \
  --urls-file <一行一条链接的文本文件> \
  --stage-root <系统 data/_stage 绝对路径> \
  [--sleep 2.0]

# stdout：一行 JSON 数组（唯一输出，不打印正文）
# stderr：进度日志
```

**零依赖**：只用 Python 标准库，系统自带 `python3` 即可运行（已用 3.9.6 裸跑验证）。

**脚本产出约定**：

```
data/_stage/<token>/
├── payload.json     # url / url_canon / title / source / author / published_at
│                    # + content_md + images[] + embedded[]
├── content.md       # 正文 Markdown（给 Agent 判类读）
└── img/NN.png       # 配图原件
```

简报字段：`idx / url / url_canon / ok / title / source / author / published_at / chars / images / embedded / token / fail_reason / elapsed`。**正文不打印。**

### 6.3 应用类核查（可再跑）

核查拉取**客观数据**，不做主观结论（结论由人填或 Agent 建议）：

| 字段 | 来源 |
|---|---|
| license / language / stars / open_issues | GitHub API `GET /repos/{owner}/{repo}` |
| last_commit | `pushed_at` |
| last_release | `GET /repos/{owner}/{repo}/releases/latest`（无则空） |
| created_date | `created_at` |
| boundary / deploy_note | Agent 读 README 摘要；标注"文章宣称"与"仓库实情"的差异 |

**核查必须记录 evidence**：每条结论标注来源 URL 与核实时间。运行时用 `verified_at` 判断是否需要复评（`review_due` 到期则在待复核队列提示）。

### 6.4 配图治理（v2.0）—— 入库时过滤，而非事后清理

**背景（实测）**：810 张配图 / 269 MB，占全库体积 **96%**（数据库才 9.3 MB、向量仅 3.2 MB）。
其中大量是**无信息载体**：小图标、广告图、"点击关注"引导图、分割线、动图、人物照片。
这类图**入库即永久占空间**，事后清理还得另做一层遗忘逻辑。

**结论：放到入库环节过滤**（源头治理），而不是等它进来再清。

**三层判据**：

| 层 | 判据 | 处置 | 成本 |
|---|---|---|---|
| **L1 硬规则** | 宽或高 < 100px；宽高比 > 1:8（分割线）；**GIF 动图** | 丢弃 | 零 |
| **L2 OCR** | OCR 文字 ≥ 10 字 | **保留 + 文字写入正文** | 每图几十 ms |
| | OCR 文字 < 10 字 | 丢弃 | —— |

**两个关键设计**：

1. **OCR 文字写入正文** —— 顺带解决「图片内容搜不到」
   ```markdown
   ![](img/03.png)

   > 图片文字：红杉资本 BP 结构 | 1.使命愿景 2.痛点 3.解决方案 4.市场规模 …
   ```
   图片里的信息**从此进入切段与索引**。此前图片合辑类文章因「正文 0 字」而完全搜不到。
   文字必须**标注来源**（`> 图片文字`），避免与正文混淆。

2. **误杀可接受**（使用者 2026-09-17 明确）—— 每篇文章都保留原始 `url`，
   图误删后**点链接即可回原文查看**。因此判据可以激进，不必为"可能的架构图"
   保留大量待定项。

**OCR 引擎选型**：抽象成 `ocr()` 接口 —— macOS 优先用系统 **Vision**（零依赖、中文准、快），
其他平台回退 PaddleOCR 或提示未配置。**不引入多模态大模型**：OCR 出文字后用现有
BGE-M3 算「图文字 ↔ 正文」相似度即可辅助判定，无需新增模型
（逻辑同 §8.4 的 reranker，但更轻——只需二分判断，不需精细排序）。

**OCR 的两个已知局限**（选型边界，不回避）：

| 局限 | 说明 | 缓解 |
|---|---|---|
| 只能处理文字 | 表情包、照片、插画无输出，无法判定 | 使用者判定这类本无保留价值（见上「误杀可接受」） |
| 一页图结构会乱 | 表格 / 流程图 / 层级关系被压成散落文字行 | 已有文字**仍可检索**，结构信息靠原文链接回溯；后续可接版面分析（§12.1 留接口） |

**历史数据回扫**：新规则只管新入库；存量 810 张按**同一套判据**回扫，避免新老标准不一致。

---

## 7. 切段与向量化

### 7.1 Embedding

| 项 | 值 |
|---|---|
| 库 | `FlagEmbedding`（BAAI 官方库，**依赖 torch**，需自行安装） |
| 模型 | `BAAI/bge-m3`（568M，MIT，**1024 维**） |
| 输出 | **dense**：1024 维归一化向量（语义路）<br>**sparse**：token 权重（词法路） |
| 归一化 | dense 是（便于余弦距离） |
| 维度配置 | 必须与 `chunk_vecs` 的 `FLOAT[1024]` 一致，改模型须重建表 |
| 备选 | `BAAI/bge-large-zh-v1.5`（中文专用，1024 维，**不含 sparse**，须退回 FTS5 + jieba 方案） |

**为什么选 BGE-M3**：它一个模型同时产出语义向量与词权重，**取代了原方案「向量库 + jieba + FTS5」的两组件结构**——中文分词（SQLite FTS5 上的老问题）整个消失。且 sparse 是学习出来的词权重，命中专有名词（模型名 / 仓库名）的能力强于 BM25。

**一期只启用两路（dense + sparse）**。BGE-M3 的 ColBERT 多向量输出**不启用**——存储与计算成本高、收益有限，列为二期可选。

**实现方注意（本机实测）**：HuggingFace 直连不通，模型优先从 **ModelScope** 拉取；权重实测约 **4.3 GB**（不是早期估的 2.2 GB，`FlagEmbedding` 与 torch 也未预装，需一并安装）。首次下载后缓存在 `~/.cache/modelscope/models/BAAI--bge-m3/snapshots/master`，之后完全离线可跑（§15）。后端 `config.resolve_model_path()` 探测顺序为「环境变量 `MYRAG_MODEL_PATH` → ModelScope 本地缓存 → 上述实测路径 → 联网拉取」，前三级不联网。

### 7.2 切段规则

```
按 Markdown 标题层级切（## → ###）：
  · 优先保证每段是一个完整论点
  · 单段长度上限 1024 token；超出则按段落（\n\n）继续切
  · 仍超长则按句子切（。！？）兜底
  · 相邻段重叠 15%（约 154 token），重叠取自上一段结尾
  · 代码块（```）与表格（|）整块保留，绝不切开
  · heading_path 记录完整标题链，如 "三、架构设计 > 3.2 切段策略"
```

**上限为何定 1024、而非用满模型上限 8192**：BGE-M3 能吃 8192 token，一篇微信文章几乎可整篇进——但那样**检索粒度会粗到命中即整篇**。切段是为了检索准，不是为了迁就模型。1024 是「论点完整」与「定位精确」的平衡点。

**为什么不能按固定字数切**：技术文章是论证链（前提→推导→结论）。硬切会把结论和前提分到两段，检索捞出来是半句话——这是 RAG 效果最差的一类失败。

**实现注记：微信正文的标题不是标准 Markdown 标题（实测三篇后补）**。上面的「按 `##` 切」是理想形态，实际抓下来的三篇里 `##` **一个都没有**——章节标题长这样：

| 原始形态 | 实例 | 处理 |
|---|---|---|
| H1 编号 + 下一行标题名 | `# 01` + 空行 + `缘起` | 合并为 `01 缘起` |
| 整行加粗编号 + 整行加粗标题 | `**01**` + `**大模型正在毁掉你的深度学习**` | 合并为 `01 大模型正在毁掉你的深度学习` |
| 整行加粗 + 前导编号 | `　** 2.1 五层记忆**` | 按编号定层级 → `02 怎么构建 > 2.1 五层记忆` |
| 整行加粗无编号 | `**项目简介**` | 与当前层级同级 |

因此切段实现**不能只认 `##` 及以下**，规则是：

- 文档**第一个**标题视为文章名，不进标题链；其余标题**含后续 H1** 都进链
- 「整行加粗」视为伪标题，判据是**整行恰好被 `**` 包裹、≤40 字、且不以句末标点收尾**——这条判据把 `**项目简介**`（标题）与 `**当业务人员懂技术……会产生质的飞跃。**`（强调句）、`**也许你还想看：**`（句末冒号）区分开
- 纯编号标题先暂存，与紧随的标题行合并；等不到标题名就用编号本身

若不这么做，整篇会退化成「单一标题段 + 按长度硬切」，切段边界正好落在论证中间——即本节开头要避免的那类失败。**切段规则改动后，已有文章需用 `scripts/reindex.py` 按新规则重算分段**（原文与配图是底本，只读不写）。

### 7.3 向量化流程

```
1. 切段产出 chunks
2. 逐段 BGE-M3 编码（batch=8）→ 同时得 dense 向量与 sparse 词权重
3. 写 chunk_vecs（dense）与 chunk_sparse（sparse）
```

---

## 8. 检索规范

### 8.1 两路融合

| 路 | 实现 | 权重 |
|---|---|---|
| 语义（dense） | `chunk_vecs` 向量近邻（查询同样用 BGE-M3 编码），取 top 30 | 见 RRF |
| 词法（sparse） | `chunk_sparse` 词权重内积，取 top 30 | 见 RRF |

**sparse 路的实现约束**：`chunk_sparse.weights` 存 `{token_id: weight}` 的 JSON。检索时在 Python 侧加载为 `scipy.sparse` 矩阵（常驻内存，启动时从 SQLite 构建）算内积——纯 Python 逐条算内积在万级段数下会有秒级延迟，稀疏矩阵是必要的。**查询侧同样走 BGE-M3 的 sparse 输出**，不做任何分词。

**RRF 融合**（Reciprocal Rank Fusion）：

```
score(chunk) = Σ_over_lists  1 / (k + rank_in_list)      k = 60
```

按融合分降序，取 top N（默认 20）→ 按 `article_id` 聚合去重 → 每个文章返回最高分片段作为摘要预览。

### 8.2 过滤与排序

可叠加过滤：`category`、`tag`、`date_from`、`date_to`。

### 8.3 检索返回结构

```json
{
  "query": "...",
  "total": 12,
  "hits": [
    {
      "article_id": "T-20260101-01",
      "title": "...",
      "category": "tech",
      "source": "示例公众号",
      "published_at": "2026-08-27",
      "url": "https://mp.weixin.qq.com/s/...",
      "score": 0.0312,
      "matched_chunks": [
        { "heading_path": "三、架构设计 > 3.2 切段策略", "snippet": "...", "rank_dense": 3, "rank_sparse": 1 }
      ]
    }
  ]
}
```

**每个命中必须带 `url`** —— 这是"点开即跳转"的数据基础。

### 8.4 Reranker 精排（v2.0）

**为什么需要**：BGE-M3 是 **bi-encoder**（问题与文档分别编码后比距离），只能回答
「这段话和问题语义**像不像**」，回答不了「这段话到底**答没答到点上**」。后果是初检
Top-N 里会混进「语义相近但无用」的噪声段；若原样交给 Agent 的 LLM，LLM 照单全收、
反被带偏。

**流程**：

```
两路各取 TOP_PER_ROUTE(30) → RRF 融合 → 取 RERANK_TOP_N(20) → cross-encoder 重排 → 取 limit
```

**配置**（`server/config.py`）：

| 项 | 默认 | 说明 |
|---|---|---|
| `RERANK_ENABLED` | `True` | **可开关** —— 这是 A/B 对比的前提（§8.5） |
| `RERANK_MODEL_ID` | `BAAI/bge-reranker-v2-m3` | 本地权重，离线可用 |
| `RERANK_TOP_N` | `20` | 进入精排的候选数（过大则延迟上升） |

**三条约束**：

1. **不改入库路径** —— 只作用于检索，**无需 reindex**
2. **懒加载**（同 `embed.py` 做法），首次调用时载入
3. **降级**：模型缺失 / 加载失败时**跳过重排并记日志**，绝不能因重排把检索搞挂

**实测（2026-09-17，本机 CPU / Apple Silicon / `bge-reranker-v2-m3`）**：

| 配置 | Recall@1 | MRR | nDCG@10 | P50 延迟 |
|---|---|---|---|---|
| 不开精排（基线） | 0.700 | 0.778 | 0.830 | 59 ms |
| 20 候选 · 320 字符 · fp32 | 1.000 | 1.000 | 1.000 | 2000 ms |
| 10 候选 · 320 字符 · fp32 | 0.900 | 0.911 | 0.930 | 1004 ms |
| 20 候选 · 160 字符 · fp32 | 0.800 | 0.900 | 0.926 | 1080 ms |
| **20 候选 · 320 字符 · fp16** | **1.000** | **1.000** | **1.000** | **614 ms** |

**三条从数据里得出的结论**（均已写进 `config.py` 注释，防止后人重踩）：

1. **不要为了省延迟砍 `RERANK_TOP_N`**。精排的价值恰恰在于「把 RRF 排错的捞回来」——
   评测集里 q05 的目标段本在 RRF 第 9 位附近，候选砍到 10 就捞不到它，MRR 掉回 0.911。
2. **也不要砍 `RERANK_MAX_CHARS` 到 320 以下**。160 字符时 MRR 掉到 0.900 —— 太短判断不准。
3. **要提速请开 fp16**：精度无损（同为满分），延迟从 2000 ms 降到 614 ms。
   即 `FlagReranker(..., use_fp16=True)`；Apple Silicon 有 fp16 单元。

模型体积 **2187 MB**。这正是 `scripts/fetch_rerank.py` 把下载做成**显式动作**的原因：
它绝不能发生在检索请求路径上（实测踩过 —— 一次检索挂到超时）。

**精排的净开销约 555 ms**（614 − 59），这是 CPU 上的物理成本；想再快需换更小模型或 GPU。

### 8.5 检索评估体系（v2.0）—— 后续所有决策的共用标尺

**为什么需要**：在此之前，「切段多大合适」「RRF_K 取多少」「加了 reranker 有没有变好」
全部只能靠**辩论**。没有基线，「改进」无法证伪。

**两层指标，分工不同**：

| 层 | 指标 | 成本 | 频率 |
|---|---|---|---|
| **IR 层** | `Recall@1/3/5/10`、`MRR`、`nDCG@10`、`P50/P95 延迟` | 零（纯本地计算） | **每次改动都可跑** |
| **RAGAS 层** | `context_precision`、`context_recall`（可选 `faithfulness`） | 需 LLM 评判 | **阶段性跑**（调参 / 发版前） |

**为什么是这两层、而不是只用 RAGAS**：MyRag 是**检索系统**，RAGAS 评生成的核心指标
（`faithfulness` / `answer_relevancy`）没有数据可算；但它评检索的两项
（`context_precision` / `context_recall`）**可直接使用** —— 让 LLM 基于检索结果现场
生成答案，看能不能撑住，即可判断"检索给的东西有没有用"。而
**「LLM 觉得对回答没提升」与「人觉得不相关」高度一致**，故 LLM 评判可作人的代理。

**评测集**（`eval/queries.yaml`）：

```yaml
- id: q01
  query: "怎么防止 Agent 乱改文件"      # 必须是「人会怎么问」，不是标题改写
  relevant: ["T-20260101-01"]          # 哪些文章才算回答了这个问题
  note: "口语化提问"
```

**构造纪律**（做错了后面全白费）：

1. query 必须**像人话**，不能是从标题反推的关键词堆
2. 先写 10 条，**经使用者确认问法**后再扩到 20–30 条
3. 标注「什么才算回答了这个问题」，而不只是「关键词命中」

**脚本**（`scripts/eval.py`）：

- 走 **HTTP 调 `/api/search`**（不 import 后端内部，测的是真实链路）
- 支持 `--mode hybrid|dense|sparse`、`--k`、`--sweep`（参数扫描）、`--ragas`
- **不碰生产代码**；报告输出 `eval/reports/<日期>-<标签>.{json,md}`

**关于 RRF 分数的常见误读**（实测澄清）：`score` 是 RRF 的排名融合值 `Σ 1/(K+rank)`，
`K=60`，**理论上限 ≈ 0.0328**（两路都排第 1）。所以界面上看到 `0.02–0.03`
**不是「相关率只有 2%–3%」**，而是正常值域。该分数**只在同一次查询内可比**，
跨查询无绝对含义 —— 判断相关性必须用 IR 指标。

> ℹ️ 本节同时是**配图相关性阈值**（§6.4）与**切段 / 融合参数**标定的唯一依据：
> 没有评测集，任何「调这个参数更好」的说法都不可验证。

---

## 9. 后端 API

Base：`http://127.0.0.1:8765`，全部 JSON。

**注意：本后端不提供抓取接口。** 抓取在 Skill 侧脚本完成（§6.2），后端只接收抓取结果。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/health` | 存活检测，返回 `{status, version, counts}` |
| POST | `/api/check-urls` | 批量查重：给 `url_canon` 列表 → 返回其中已存在的（含已有 `id`）。去重前置，避免白抓 |
| POST | `/api/ingest` | 提交 `stage_token` + 批注（**不含 `id`**）→ 读 staging 磁盘副本 → **分配 id** → 落原文与配图 → 切段+向量化+写库 |
| POST | `/api/ingest-text` | **辅路径**：直接提交正文文本（无 stage、不抓取、不判类）。用于抓取失败或文章已删时的手工兜底 |
| GET | `/api/search` | 参数：`q`、`mode`（hybrid\|dense\|sparse）、`category`、`tag`、`limit` |
| GET | `/api/articles` | 参数：`category`、`tag`、`q`、`review_flag`、`page`、`page_size` |
| GET | `/api/articles/{id}` | 详情：主记录 + 分段（若建了索引）+ 评估卡（若带 `repo_card`）+ 概念（若带 `concepts`）+ 附件列表 |
| PATCH | `/api/articles/{id}` | 人工修正：`category`、`tags`、`summary`、`verdict` 等 |
| POST | `/api/articles/{id}/verify` | 重新核查（仅 `features` 含 `repo_card` 的分类），刷新 GitHub 字段 |
| DELETE | `/api/articles/{id}` | 删除（同时清理原文与配图，需二次确认参数 `confirm=yes`） |
| GET | `/api/stats` | 各分类计数（按配置动态生成键）、近 7 日入库量、待复核数、库体量 |
| GET | `/api/categories` | 分类配置 + 每类文章数 + 可选检索方式/能力项（**Skill 判类前必读**，§5.4） |
| POST | `/api/categories` | 新增分类：`key` / `name` / `prefix` / `criteria` / `retrieval` / `features` / `is_default` / `sort_order` |
| PATCH | `/api/categories/{key}` | 改分类（`key` 不可改；`prefix` 只影响未来的新编号） |
| DELETE | `/api/categories/{key}` | 删分类（需 `confirm=yes`；该类下还有文章时拒绝 —— 不做静默迁移） |

**`POST /api/check-urls` 请求与响应**：

```json
// 请求：传规范化后的 url_canon，不传原始分享链接
{ "url_canons": ["https://mp.weixin.qq.com/s?__biz=..&mid=..&idx=..&sn=..", "..."] }

// 响应
{ "dups": [ { "url_canon": "https://mp.weixin.qq.com/s?...", "id": "T-20260101-01" } ] }
```

- **必须传 `url_canon`**：原始分享链接带 `mpshare`/`scene`/`srcid`/`#rd` 等追踪参数，同一篇文章两次转发字符串不同，直接比会漏。
- Skill 拿到 `dups` 后记 `skip_dup`（附已有 `id`），**不抓取**。

**`POST /api/ingest` 请求体**：

```json
{
  "stage_token": "a1b2c3",
  "category": "tech",
  "title": "示例文章 B设计",
  "source": "示例公众号",
  "author": "左德军",
  "published_at": "2026-08-27T10:00:00+08:00",
  "url": "https://mp.weixin.qq.com/s/...",
  "summary": "...",
  "tags": ["RAG", "记忆系统"],
  "review_flag": 0,
  "app_card": null,
  "concepts": [
    { "name": "五层记忆架构", "definition": "...", "related": ["两级目录约束"] }
  ]
}
```

**`POST /api/ingest` 响应**：

```json
{ "ok": true, "dup": false, "id": "T-20260101-01", "fail_reason": null }
```

- **请求体里没有 `id`**。编号由后端分配（§5.3）——`NN` 必须查库才算得准，Skill 侧生成必然撞号。后端按 `category` 定前缀、按 `published_at` 定日期，在响应里返回 `id`。
- **正文与配图不在请求体里**：后端从 `data/_stage/<stage_token>/payload.json` 与 `img/` 读取。请求体只带批注。
- `source` / `author` / `published_at` / `title` 若 Skill 未改，可由后端从 `payload.json` 回填。
- **幂等约定（必须实现）**：
  - 同一个 `stage_token` 重复提交 → 返回 `{ok:true, dup:true, id:<首次分配的 id>}`，**HTTP 200**，不重复写
  - `url_canon` 已存在 → 同样返回 `{ok:true, dup:true, id:<已有 id>}`，**HTTP 200**
  - **不返回 409**：网络超时后重试是正常场景，报错会让 Skill 误判为失败并反复重试。`dup=true` 是"已存在"的正常结果，Skill 按跳过处理。
- ingest 成功后后端清理该 stage 目录；失败则保留，便于重试。
- `app_card` 与 `concepts` 按类选填。

**`POST /api/ingest-text` 请求体（辅路径，§10.3）**：

```json
{
  "title": "文章标题",
  "url": "https://mp.weixin.qq.com/s/...",
  "category": "tech",
  "raw_content": "# 标题\n\n正文 Markdown 或纯文本……",
  "published_at": null
}
```

- **辅路径不抓取、不判类**：后端只做 `分配 id → 切段 → 向量化 → 写库`。
- `url` 可空（文章已删号时用户手里只有正文）；为空则 `url_canon` 由后端按 `title + collected_at` 生成占位 key。
- `category` 可为 `null` —— 落 `tech` 并置 `review_flag = 1` 进待复核队列；`summary` 一律留空（后端不调 LLM）。
- 响应同 `/api/ingest`（含分配的 `id`）。

---

## 10. 工作台（web/index.html）

### 10.1 形态

- **单文件 HTML**，内联 CSS/JS，无构建步骤
- 通过 `fetch` 调 `127.0.0.1:8765`
- 后端地址可配置（页面顶部常量），便于改端口
- 需适配窄屏（手机浏览器同局域网可开，二期再优化）

### 10.2 模块

| # | 模块 | 内容 |
|---|---|---|
| 1 | **顶栏** | 全局搜索框（语义/关键词切换）+ 库体量统计 |
| 2 | **概览** | 各分类计数卡片（按配置生成）、近 7 日入库趋势、待复核数 |
| 3 | **待复核队列** | `review_flag = 1` 与 `review_due` 到期的条目，可一键改类（改类按钮按配置生成） |
| 4+ | **分类视图**（每类一个页签） | 形态由该分类的 `features` 决定：带 `repo_card` → 评估卡网格（详情含"重新核查"）；带 `concepts` → 文章列表 +「概念互链」子页签；其余 → 通用列表。**所有分类统一「点整行进详情」** |
| — | **分类设置** | 增删改分类：显示名、编号前缀、检索方式、能力项、默认分类、划分标准、排序（§5.5） |
| — | **手工补录** | 粘贴正文入库；类别选项按配置生成 |

**关于「整行点击」（v1.7 变更）**：取消「理念类整行跳原文」—— 同类行为必须一致。
跳原文改为详情页内的**显式按钮**（详情里仍有"打开原文"），列表行内也保留原文链接。
原先按分类区别对待整行点击，是分类写死时代的遗留设计。
| 7 | **手工补录** | 粘贴**正文文本** + 手动选类别 → 调 `/api/ingest-text`（辅路径，见 §10.3） |
| 8 | **详情抽屉** | 右侧滑出：原文 Markdown 渲染、配图、元数据、原文链接（新窗口打开） |

### 10.3 入库的两条路径

| 路径 | 谁执行 | 输入 | 适用 |
|---|---|---|---|
| **主路径** | 对话中贴链接给 Agent（Skill 抓取 + 判类） | 链接 | 常规、批量、产出完整（**推荐**） |
| **辅路径** | 工作台粘贴框 → `/api/ingest-text` | **正文文本**（不是链接） | 兜底：抓取失败、文章已删号、手动补录一段 |

**辅路径为什么只能贴正文、不能贴链接**：后端不抓取（§9 开篇即声明），工作台也抓不了（浏览器直连微信会被跨域挡回）。**后端拿到链接变不出正文**——所以辅路径的输入必须是用户自己复制好的正文。

**辅路径的取舍**：后端不调 LLM，因此 `summary` 留空、`category` 由用户在粘贴框旁手动选（三选一 + 「不确定」）；选「不确定」时落 `tech` 并置 `review_flag = 1` 进待复核队列。编号同样由后端分配（§5.3）。一句话——**宁可不判，不可乱判**。

### 10.4 视觉要求

- 深浅两套配色，跟随系统 `prefers-color-scheme`
- 无外部 CDN 依赖（离线可用），字体用系统字体栈
- 不使用 emoji 作为功能图标，用内联 SVG

---

## 11. Skill 与系统的职责边界（关键）

**Skill 负责「抓 + 判」，系统负责「存 + 查」。**

| 角色 | 载体 | 职责 | 明确不做 |
|---|---|---|---|
| **Skill · `MyRag`** | `~/.workbuddy/skills/MyRag/`（`SKILL.md` + `scripts/wx_fetch.py`） | **抓取全文与配图**、判类、出摘要与标签、编排调用 API、核对应用类项目、输出结果清单 | 不写正式存储、不切段、不检索 |
| **系统** | 本地后端 + SQLite + 工作台 | 落盘、切段、向量化、存储、检索、展示、GitHub 核查 | **不抓取、不调 LLM** |

这条边界是硬性的：**判类的错由 Skill 侧承担（标 `review_flag` 修正），不让系统去猜**。

**抓取为什么归 Skill**：微信页面结构会变，抓取是唯一需要频繁适配的环节。放在 Skill 侧脚本里，改一次即刻生效，不必动后端、不必重启服务；同时抓取可复用 Agent 侧已实测通过的路径（§6.2，7/7）。

### 11.1 三条调用保障

新会话的 Agent **不记得任何历史对话**。系统必须自解释。

| 保障 | 载体 | 作用 |
|---|---|---|
| 触发 | **`MyRag`**（5 字母） | 用户唯一需要输入的关键词，命中即加载全部分流规则 |
| 拉起 | `scripts/start.sh` / `stop.sh` / `status.sh` | 新会话可自行检测并拉起后端（**按需拉起，不随登录启动**，见 §11.3） |
| 状态 | `data/wxk.db` + `data/raw/` | 全部状态在磁盘，不依赖对话记忆 |

### 11.2 SKILL.md 必须写明

1. **触发词：`MyRag`**（唯一）
2. **第一步永远是** `curl -s --max-time 3 http://127.0.0.1:8765/api/health`；不通则执行 `scripts/start.sh` 并等待最多 15 秒；仍不通则停止并报告，**不许绕过系统自己写正式存储**
3. **抓取步骤与硬约束**（§6.2）——脚本路径、`stdout` 只回简报的约定、stage 目录结构
4. 完整分流流程（§6.1）
5. 判类规则（§5.4）
6. 反模式清单：❌ 用 `var nickname` 提公众号名（**该变量不存在，实测得空值**） ❌ 把 stage 正文打印到 `stdout`（撑爆上下文） ❌ 把摘要当正文存 ❌ 应用类只复述文章不核查 ❌ 收到验证页仍入库 ❌ 配图不下载
7. 结果输出格式（入库 / 跳过 / 失败 三条计数 + 逐条一行）

### 11.3 服务按需启动（不常驻、不自启）

**取向：单人低频工具，不做系统级常驻。**

- **启动**：`bash scripts/start.sh` —— `nohup` 后台拉起，PID 写 `data/server.pid`，
  内含最多 15 秒健康检查轮询。要看工作台时手跑一次即可。
- **停止**：`bash scripts/stop.sh`。不跑也不影响数据（状态全在 `data/wxk.db` + `data/raw/`）。
- **查询**：`bash scripts/status.sh` —— 回 `/api/health`，并给出当前 pid。
- **已知代价（接受）**：`nohup` 进程只活在当前登录会话里，**重启电脑后不会自动恢复**；
  此时 `status.sh` 会看到「后端未响应 + 残留 PID 文件」，重新 `start.sh` 即可。

**为什么不做重启自动拉起**：常驻方案（macOS LaunchAgent）需要把启动器与日志放到项目外的
`~/.myrag/`（`~/Documents` 受 TCC 隐私保护，launchd 进程对其中**已存在**的文件连读都读不了、也
不能执行项目内的 `.sh`），还要引入 `KeepAlive`（副作用是 `stop.sh` 再也停不掉服务）。**为一个
偶尔打开的工具付出系统级配置的维护成本，不划算。**

真正需要「自动」的只有一处：调 `MyRag` Skill 时后端必须在跑。这件事由 **Skill 第 0 步**承担
（§11.2 第 2 条）——Skill 是唯一会在用户没主动开服务时用到后端的入口。

---

## 12. 技术栈与目录结构

### 12.1 依赖

**系统（后端 + 工作台）**：

```
python >= 3.11
fastapi
uvicorn[standard]
jinja2            # 若需模板渲染
sqlite-vec
FlagEmbedding     # BGE-M3（需安装；会连带装 torch，本机非预装）
scipy             # sparse 词权重矩阵运算
jieba             # 仅 §7.1 备选模型（bge-large-zh）退回 FTS5 方案时需要
httpx             # 仅用于 GitHub API（不用于抓取）
beautifulsoup4    # 可选：正文解析回退（若脚本改用 bs4）
markdown-it-py    # Markdown 渲染（或前端渲染）

# v2.0 新增
pyyaml            # 读 eval/queries.yaml（仅评估脚本用）
ragas             # 可选：RAGAS 复核（仅评估脚本用，需 LLM key）
```

**v2.0 三项新增能力的依赖交代**：

- **Reranker（§8.4）**：与 BGE-M3 **同属 `FlagEmbedding` 库，无需新 pip 依赖**；
  只需额外下载 `bge-reranker-v2-m3` 权重（本地缓存、离线可用）
- **OCR（§6.4）**：**不引入 pip 依赖** —— macOS 优先系统 **Vision**；其他平台回退
  PaddleOCR 或提示未配置（抽象成 `ocr()` 接口，见 §6.4）
- **评估（§8.5）**：`pyyaml` + 可选 `ragas`，**只进评估脚本、不进运行时依赖**

**Skill 抓取脚本：零依赖。**

`~/.workbuddy/skills/MyRag/scripts/wx_fetch.py` 只用 Python 标准库（`urllib` + `re` + `json` + `html`）。

**这条是刻意的**：抓取先于后端就绪就能跑通；脚本不依赖 venv、不依赖任何 pip 包，出问题只可能在网络或页面结构，排查面最小。

### 12.2 目录结构

```
~/MyRag/          ← 系统根（PRD、代码、数据全在这里）
├── PRD.md
├── requirements.txt
├── eval/                   # v2.0：检索评估（§8.5）
│   ├── queries.yaml        # 评测集：query + 相关文章标注
│   └── reports/            # 评测报告（json + md）
├── server/
│   ├── app.py              # FastAPI 入口与路由
│   ├── db.py               # 连接、schema 初始化、迁移
│   ├── schema.sql
│   ├── ingest.py           # 读 stage → 落原文/配图（v2.0：入库过滤 + OCR）→ 切段 + 向量化 + 写库
│   ├── embed.py            # BGE-M3 封装（懒加载单例，产出 dense + sparse）
│   ├── search.py           # 两路检索 + RRF +（v2.0）reranker 精排
│   ├── rerank.py           # v2.0：cross-encoder 精排（缺权重自动跳过，§8.4）
│   ├── imgsieve.py         # v2.0：配图过滤与 OCR（§6.4，待实现）
│   ├── verify.py           # GitHub 核查
│   └── config.py           # 端口、路径、模型名、维度
├── data/
│   ├── wxk.db
│   ├── _stage/             # Skill 抓取脚本的落地区（ingest 后清理）
│   │   └── <token>/
│   │       ├── payload.json
│   │       ├── content.md
│   │       └── img/NN.png
│   ├── raw/
│   │   └── 2026/
│   │       └── A-20260101-01.md
│   └── media/
│       └── A-20260101-01/
│           └── 01.png
├── web/
│   └── index.html
└── scripts/
    ├── start.sh              # 按需启动（nohup；重启电脑后需重跑，见 §11.3）
    ├── stop.sh               # 停止
    ├── status.sh             # 健康检查 + 当前 pid
    ├── reindex.py            # 重建分段与索引（切段规则/模型变更后用，原文只读）
    ├── eval.py               # v2.0：检索评测（只读，§8.5）
    └── fetch_rerank.py       # v2.0：下载精排权重（约 2.2GB；显式动作，§8.4）

~/.workbuddy/skills/MyRag/          # Skill 包（不在系统目录内）
├── SKILL.md
└── scripts/
    └── wx_fetch.py               # 抓取脚本，零依赖，写入 ~/MyRag/data/_stage/
```

**抓取脚本位置的说明**：脚本放在 Skill 包内（而非系统目录），是为了让「抓取」这件事的归属在文件系统上也一目了然；它唯一的外部副作用是往系统 `data/_stage/` 写 staging 目录，不触碰正式存储。

**两处根目录不同**：Skill 包放在 Agent 客户端的 skills 目录（默认 `~/.workbuddy/skills/MyRag/`，
可用 `install.sh --skill-dir` 改），系统根是使用者 clone 下来的目录（默认 `~/MyRag`）。

**路径不写死，靠占位符 + 环境变量解析**（v1.7 起）：

- `SKILL.md` 模板里有两个占位符：`{{MYRAG_HOME}}`（系统根）与 `{{SKILL_DIR}}`（Skill 自身安装目录），
  由 `install.sh` 部署时替换成真实绝对路径；
- `wx_fetch.py` 的 `DEFAULT_STAGE_ROOT` 依次探测 `MYRAG_STAGE_DIR` → `MYRAG_HOME/data/_stage`
  → `~/MyRag/data/_stage`；
- **不能用相对路径推导**：Skill 包与系统根是两个互不相干的目录，`../` 推不出正确结果。

**改系统根位置时要动什么**：PRD §12.2、`.env`（或 `MYRAG_HOME` 环境变量），然后重跑一次 `install.sh`。
`SKILL.md` 与脚本本身**不需要改** —— 占位符与环境变量会跟着走。

⚠️ 本文档在 v1.7 之前写的是「SKILL.md 路径常量与 `DEFAULT_STAGE_ROOT` 都写绝对路径」，
那是**旧实现的描述，已过时**（用户实测指出，2026-09-17 更正）。

---

## 13. 验收标准

| # | 标准 | 验证方式 |
|---|---|---|
| 1 | 贴一条微信链接，60 秒内完成入库并可在工作台查到 | 实跑 3 条 |
| 2 | 语义搜索「怎么防止 Agent 乱改文件」能命中讲沙箱/权限/写前快照的文章（原文无此措辞） | 实跑，检查命中 |
| 3 | 词法（sparse）搜索「示例项目 A」「Apache-2.0」能精确命中 | 实跑 |
| 4 | 带 `repo_card` 能力的分类，详情页可一键核查 GitHub，展示 star / license / 最近提交 / 创建日期 | 实跑 |
| 5 | 各分类视图均可分类浏览；**点整行进详情**（不再按分类区别对待），详情内可跳原文 | 人工检查 |
| 6 | 配图已本地化：断网后详情页图片仍显示 | 断网验证 |
| 7 | 重复贴同一链接，`/api/check-urls` 命中并返回 `skip_dup`，**不重复抓取、不重复入库** | 实跑 |
| 8 | 微信验证页被正确拒收并记 `failed`，不产生空壳记录 | 构造用例 |
| 9 | **新开一个完全空白的会话**，输入 `MyRag` + 贴链接，Agent 能自行拉起后端、抓取、判类、完成入库 | 关掉会话重启验证 |
| 10 | 工作台在无网络（除本地后端）情况下正常显示已入库内容 | 断网验证 |
| 11 | **抓取脚本零依赖**：用系统自带 `python3` 裸跑 `wx_fetch.py`（不激活 venv），7 条链接能抓到正文与配图 | 裸跑 |
| 12 | **公众号名正确**：`source` 与页面 `id="js_name"` 一致，7 条全部非空且未截断 | 实跑 7 条逐条比对 |
| 13 | **长文不截断**：入库后 `data/raw/<id>.md` 字数与脚本 `stdout` 报的 `chars` 一致 | 抽 3 条比对 |
| 14 | **BGE-M3 加载成功**：从 ModelScope 拉取权重后正常编码，`chunk_vecs` 实际写入维度 = 1024，与表声明一致 | 实跑，查表 |
| 15 | **sparse 路独立可用**：`mode=sparse` 下贴专有名词（如 `bge-m3`、`MyRag`）能精确命中，且**全程无分词步骤** | 实跑 |
| 16 | **编号由后端分配**：请求体不传 `id` 也能入库，响应返回的 `id` 符合 `<前缀>-<发布日>-<NN>` | 实跑 |
| 17 | **重复 ingest 幂等**：同一 `stage_token` 连发两次，第二次返回 `{dup:true, id:<同一 id>}` 且 HTTP 200，库中只有一条 | 实跑 |
| 18 | **canon 前置查重**：对带 `mpshare`/`srcid`/`#rd` 的链接，第 1 步即命中 `dups`，不进入抓取 | 实跑 |
| 19 | **分类可配置**（v1.7）：工作台「分类设置」能改名、改前缀、增删分类；改完页签、概览卡片、改类按钮、Skill 判类同步生效 | 实跑（新建临时分类验证后删除） |
| 20 | **分类删除保护**：删除还有文章的分类被拒绝并说明剩余篇数，不做静默迁移 | 实跑 |
| 21 | **直接全文分类**：`retrieval=fulltext` 的分类入库不产生分段，检索时以「全文匹配」兜底召回 | 实跑 |

**第 9 条是这套系统成不成立的判据，必须实跑通过。**
**第 11、12 条是 v1.1 改动的验收点；第 14、15 条是 v1.2（Embedding 换 BGE-M3）；第 16–18 条是 v1.3（编号归属 / 幂等 / canon 前置查重）。**

---

## 14. 分期

| 期 | 内容 | 交付判据 |
|---|---|---|
| **一期（必须）** | 后端全量 + 工作台骨干 + Skill + 三条样例（3 篇参考文章）实跑 | 验收标准 1–18 通过 |
| 二期 | 工作台视觉打磨、窄屏适配、待复核队列增强、复评提醒 | 用户实际使用两周后的反馈 |
| **v2.0（进行中，2026-09-17）** | ① 检索评估体系：评测集 + IR 指标 + RAGAS 复核（§8.5）；② Reranker 精排（§8.4）；③ 配图治理：入库过滤 + OCR 文字化 + 历史回扫（§6.4）；④ 遗忘机制（§5.6）；⑤ README/PRD 补实现细节与已知局限 | 各项独立验收：评估跑出**基线数字**、reranker **开关可对比**、图片过滤**有日志可查** |
| 三期（待定） | 视频入库（视微信是否支持转发而定）、云端发布、全库定期体检（Lint：孤儿记录、失效链接、过期评估卡）、**ColBERT 第三路**（§7.1）、**ANN 索引**（当段数触及 §15 的天花板时） | 用户决定 |

---

## 15. 已知风险

| 风险 | 影响 | 对策 |
|---|---|---|
| 微信反爬升级 / 页面结构变更 | 正文抓不到 | 失败判据已内置；抓取逻辑隔离在 Skill 脚本（§6.2），改一处即刻生效、不必重启后端；终极兜底走 `POST /api/ingest-text` 手工贴正文（§10.3 辅路径） |
| 微信改字段名 | 元数据缺失（如公众号名变空） | §6.2 已记录实测字段与踩坑点；**`source` 为空即视为抓取降级**，该条标 `review_flag = 1` 待人工复核 |
| BGE-M3 权重下载失败（HuggingFace 本机不通） | 无法向量化 | 从 ModelScope 拉取（§7.1）；权重本地缓存后可离线运行 |
| 2000+ 篇存量只能一条条来 | 导入周期长 | 单条入库摩擦已压到最低（§10.3 辅路径 + Skill 快捷触发）；不追求一次性导入 |
| 判类误判 | 库内分类不准 | `review_flag` + 工作台一键改类；边界规则已前置（§5.4） |
| 本地服务未启动 | 新 session 调不通 | §11.2 第一步强制健康检查 + 自动拉起 |
| sparse 矩阵内存占用 | 段数增长后内存上升 | 启动时构建常驻矩阵并监控；超阈值改分片载入（§8.1） |
| **向量检索是暴力 KNN（无 ANN）** | 段数增长后检索延迟**线性上升**：实测库 786 段 / 向量仅 3.2 MB，看不出问题；到几万段才明显 | `sqlite-vec` 只有暴力扫描 + SIMD，**没有 HNSW/IVF 索引**。对策：先用 §8.5 建立**延迟基线**（P50/P95），**把规模天花板写进 README**（不写"够用"这类不可验证的话），到阈值再评估换 ANN 方案 |
| **文档曾欠实现细节** | 读者只能靠猜——「两路是 BM25 还是 BGE-M3 sparse」「用了什么向量索引」都无从判断，容易得出错误结论 | v2.0 起 README 必须写明：向量索引方案、两路构成、reranker 有无、**已知局限**；§6.4 / §8.4 / §8.5 三节同步 |
| **配图无节制落盘** | 实测占全库 **96%** 体积（269 MB / 810 张），且随收藏持续增长 | 入库时按规则 + OCR 过滤（§6.4）；存量回扫；「遗忘」只弃载体保语义（§5.6） |

---

## 16. 附：三类产物字段速查

| | 应用类 | 技术类 | 理念类 |
|---|---|---|---|
| 前缀 | A | T | I |
| 专属表 | `app_cards` | `concepts` | 无 |
| 核查 | 必须（GitHub） | 无 | 无 |
| 复评 | `review_due` 必填（默认 +90 天） | 无 | 无 |
| 工作台视图 | 评估卡列表 + 核查按钮 | 文章列表 + 概念互链 | 轻量列表，整行跳转 |
| 检索权重 | 同等 | 同等 | 同等 |
