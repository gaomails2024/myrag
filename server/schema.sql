PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 文章主表：三类通用字段
CREATE TABLE IF NOT EXISTS articles (
  id            TEXT PRIMARY KEY,          -- <分类前缀>-YYYYMMDD-NN（NN 为两位序号）
  category      TEXT NOT NULL,             -- → categories.key（分类可配置，见文件末尾）
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
  review_due    TEXT,                      -- 复评日期（应用类必填，默认 +90 天）
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

-- 应用类专属（评估卡）
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

-- 技术类专属（概念）
CREATE TABLE IF NOT EXISTS concepts (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL UNIQUE,
  definition  TEXT,
  article_id  TEXT REFERENCES articles(id) ON DELETE CASCADE,
  related     TEXT DEFAULT '[]'            -- JSON array，关联概念名（互链）
);

-- 入库日志（可追溯）
-- detail 为 JSON 文本；ingest 动作里固定写入 {"stage_token": "<token>"}，
-- 用于同一 stage_token 重复提交时的幂等判定（PRD §9）。
CREATE TABLE IF NOT EXISTS ingest_log (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  url        TEXT,
  article_id TEXT,
  action     TEXT,                         -- ingest | skip_dup | fail
  detail     TEXT,
  at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ingest_stage ON ingest_log(json_extract(detail, '$.stage_token'));

-- ================================================================
-- 分类配置（PRD §5.5）
-- 分类**不写死在代码里**：别人拿去开源版，需要的分类数量、名字、划分标准、
-- 检索方式都不一样。这里就是唯一事实来源，工作台「分类设置」页可增删改；
-- 首次启动时由 config.DEFAULT_CATEGORIES 播种（已存在则不覆盖）。
-- ================================================================
CREATE TABLE IF NOT EXISTS categories (
  key        TEXT PRIMARY KEY,       -- 稳定标识，写进 articles.category（入库后不建议改）
  name       TEXT NOT NULL,          -- 显示名，如「开源项目」
  prefix     TEXT NOT NULL,          -- 编号前缀，如 A / T / I（全局唯一）
  criteria   TEXT DEFAULT '',        -- 划分标准：注入 Skill，Agent 据此判类
  principles TEXT DEFAULT '',        -- 收录原则（使用者自己的偏好）：注入 Skill，
                                     -- Agent 据此判断价值、定 verdict、决定核查侧重。
                                     -- 与 criteria 的分工：criteria 答「这篇归哪类」，
                                     -- principles 答「这篇值不值得、该怎么看」。
  retrieval  TEXT DEFAULT 'rag',     -- rag=切段向量化、进语义检索；
                                     -- fulltext=不建索引，省算力，按时间浏览 + 点开读全文
  features   TEXT DEFAULT '[]',      -- JSON array：repo_card（GitHub 核查）/ concepts（概念互链）
  is_default INTEGER DEFAULT 0,      -- 1 = 判不准时落到这一类（全局最多一个）
  sort_order INTEGER DEFAULT 0,      -- 页签顺序
  display    TEXT DEFAULT '',        -- 展示样式：cards / list / compact
                                     -- 空 = 按 features 自动推导（保持老行为）
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_categories_prefix ON categories(prefix);

-- ================================================================
-- 入库规则（PRD §5.6）
--
-- 为什么只放「业务规则」：摘要要多长、标签几个、什么情况进待复核 —— 这些改错了
-- 只是"结果不合口味"，用户该能自己调。
-- 而「技术规则」（抓取解析细节、URL 规范化、切段参数）不在这里：那些改错了是
-- 系统直接坏掉（抓不到正文 / 查重失效），不该给用户一个能改坏系统的输入框。
-- ================================================================
CREATE TABLE IF NOT EXISTS rules (
  key        TEXT PRIMARY KEY,       -- 规则标识（与 config.RULE_DEFS 对应）
  value      TEXT NOT NULL,          -- 值（数字也存字符串，读取时按 type 解析）
  updated_at TEXT NOT NULL
);
