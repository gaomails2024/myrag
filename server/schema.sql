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
  verdict       TEXT,                      -- 建议（try | watch | dead）：Agent 给的建议，非系统判定
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

-- ================================================================
-- 知识结构：自生长主题（PRD §5.7）
--
-- 为什么不用 articles.category：分类是**入库那一刻的一次性判类**、每篇只能进
-- 一个格子、全库只有 5 档。几百篇以后它只能回答「这篇属于哪一筐」，回答不了
-- 「这篇在库里和谁是一伙的」。标签更糟：实测 2148 个 tag / 752 篇 = 每个 tag
-- 平均只覆盖 0.35 篇，而且同义词泛滥（Agent / AI Agent / 智能体 各成一类，
-- 人机协同 / 人机协作 并存）—— 标签退化成了「每篇自己的词」，不具聚合能力。
--
-- 主题是**另一层视角**，与 category 完全解耦：
--   1) 一篇可同时属于多个主题（带权重），跨主题的关系本身是知识
--   2) 主题之间有关系：相关 related / 对立 opposed / 依赖 depends
--   3) 由全库向量层次聚类长出来（不是拍脑袋定的分类），命名由 Agent 给
--   4) 派生数据：原文与向量都在，随时可整表重建；不重判 category、不迁移任何数据
-- ================================================================
CREATE TABLE IF NOT EXISTS themes (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT NOT NULL,        -- 主题名（Agent 命名，用户可改；改了就以用户为准）
  summary       TEXT,                 -- 一句话说明：这个主题在讲什么、为什么值得看
  keywords      TEXT DEFAULT '[]',    -- JSON array
  parent_id     INTEGER REFERENCES themes(id) ON DELETE SET NULL,  -- 树：聚类树的层级
  level         INTEGER DEFAULT 1,    -- 1 = 一级主题
  centroid      BLOB,                 -- 1024 维 float32（已 L2 归一化）：增量归类算距离用
  article_count INTEGER DEFAULT 0,
  status        TEXT DEFAULT 'active',-- active | retired（被合并或拆分后下线，不删行）
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_themes_parent ON themes(parent_id);

-- 文章 <-> 主题（**一文多主题**，这是与 category 最大的区别）
--   role   primary（主）/ secondary（次）
--   status suggested（算法建议、待用户确认）/ confirmed / rejected
CREATE TABLE IF NOT EXISTS article_themes (
  article_id TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
  theme_id   INTEGER NOT NULL REFERENCES themes(id) ON DELETE CASCADE,
  weight     REAL DEFAULT 1.0,       -- 0-1，属于该主题的强度（到质心的相似度）
  role       TEXT DEFAULT 'primary',
  status     TEXT DEFAULT 'confirmed',
  added_at   TEXT NOT NULL,
  PRIMARY KEY (article_id, theme_id)
);
CREATE INDEX IF NOT EXISTS idx_article_themes_theme ON article_themes(theme_id, status);

-- 文章画像向量（**不是** chunk 级向量）：一篇一条，用于聚类与增量归类。
--
-- 为什么存 BLOB 自己算距离，不建 vec0 虚表：
--   1) 752 篇 x 1024 维全量线性扫描是毫秒级（search.py 已有同样先例），
--      vec0 带来的加速在这里毫无意义；
--   2) 省掉「TEXT 主键在 vec0 里受不受支持」这个不确定性。
-- 换 embedding 模型必须整表重算 —— model_id 就是为此存在的。
CREATE TABLE IF NOT EXISTS article_profiles (
  article_id TEXT PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
  vec        BLOB NOT NULL,          -- float32 x 1024，L2 归一化
  model_id   TEXT NOT NULL,
  built_at   TEXT NOT NULL
);

-- 结构版本（key/value）：当前生效的是哪一档聚类、什么时候建的、用什么模型
CREATE TABLE IF NOT EXISTS structure_meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

-- ================================================================
-- 标签规范化（PRD §5.8）
--
-- 为什么需要：标签是**每篇互斥挑几个**、不是内容打标，所以同一个概念在不同文章上
-- 会被写成不同写法。实测 `Agent`(33 篇) 与 `AI Agent`(31 篇) 的文章**交集是 0** ——
-- 它们从没在同一篇上共现，靠「共现度高 = 同义」判断同义在这套数据上完全失效。
-- 后果：2166 个标签 / 752 篇，平均每个标签只覆盖 0.35 篇，词云上「智能体」「AI Agent」
-- 「AI智能体」并排出现却指同一件事。
--
-- tag_aliases 存「别名 → 规范名」的映射，是这套规范的**唯一事实来源**：
-- 归并一次、存一次，以后所有入库自动套用（见 app.py 的 ingest 路径），
-- 判类时也把规范词表注入 Skill 让 Agent 先查词表再选词（减少新增碎片）。
-- ================================================================
CREATE TABLE IF NOT EXISTS tag_aliases (
  alias     TEXT PRIMARY KEY,        -- 旧写法 / 待收敛的写法
  canonical TEXT NOT NULL,           -- 规范名（用户拍板，不是算法决定）
  note      TEXT DEFAULT '',         -- 为什么这么合（可选，便于日后回看）
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tag_aliases_canonical ON tag_aliases(canonical);

-- 规范词表之外的**新标签**队列：入库时不在 tag_aliases 里的词会落这里计数。
-- 不禁止使用（会阻断入库），只是让「又攒出多少碎片」变成一个可见的数字。
CREATE TABLE IF NOT EXISTS tag_pending (
  tag        TEXT PRIMARY KEY,
  seen_count INTEGER DEFAULT 0,      -- 累计出现次数
  first_seen TEXT,
  last_seen  TEXT
);
