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

-- ================================================================
-- 知识编译 Wiki（PRD §5.7）
--
-- 为什么要它：检索是「提问时现场找」—— 每次都要重读原文，同一篇被反复读，
-- 而读完的东西不留下。Wiki 是反过来：**在编译期把一批文章读透、写成一篇成品页**，
-- 每条结论挂着编号引用，页面上点角标就能回到原文。
--
-- 它替代不了检索（752 篇、粒度到段，未编译的部分仍然只能靠检索），
-- 但「这个话题整体讲了什么」这类问题，只适合读一篇成品页。
--
-- 这三张表全是**派生数据**：原文与 chunks 都在、随时可整表重建，
-- 所以设计上可以放心「重编译先删后插」，不必像 articles 那样处处防御。
-- ================================================================
CREATE TABLE IF NOT EXISTS wiki_pages (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  slug        TEXT NOT NULL UNIQUE,     -- URL 片段（如 t54）：与话题一一对应，
                                        -- 不随标题变 —— 改标题不该让已分享的链接失效
  title       TEXT NOT NULL,
  topic_id    INTEGER REFERENCES themes(id) ON DELETE SET NULL,  -- 话题被删/下线后页面还在
  concept_id  INTEGER REFERENCES wiki_concepts(id) ON DELETE SET NULL,  -- 概念页；NULL=话题综述页
  summary     TEXT,                     -- 一句话概览（页头展示）
  content_md  TEXT NOT NULL,            -- 正文 Markdown
  source_count INTEGER DEFAULT 0,       -- 编译时实际用到的文章数（不等于话题篇数，被截断时会小）
  model       TEXT,                     -- 编译用哪个模型：换模型重编译要看出差别
  prompt_version TEXT,                  -- 提示词「文件名#内容指纹」：提示词改了能溯源
  status      TEXT DEFAULT 'ok',        -- ok | stale（引用的文章被删过，只剩角标编号）
  built_at    TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
-- **不存 content_html**：markdown-it 渲染是纯函数、毫秒级（api_article_detail 同此做法），
-- 存两份必然有一份是过期的。
CREATE INDEX IF NOT EXISTS idx_wiki_pages_topic ON wiki_pages(topic_id);

-- 引用：正文里那些 [R1] 角标，一条编号对应一篇原文
CREATE TABLE IF NOT EXISTS wiki_refs (
  page_id    INTEGER NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
  article_id TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,  -- 删文章自动清引用，
                                        -- 不留悬空 id（页面上就少一个角标，正文 md 不受影响）
  ord        INTEGER NOT NULL,          -- 正文里的编号 [Rn]
  quote      TEXT,                      -- 模型摘的原文短句：事后能核对「这条凭什么在这儿」
  PRIMARY KEY (page_id, ord)
);
CREATE INDEX IF NOT EXISTS idx_wiki_refs_article ON wiki_refs(article_id);

-- 双链（相关页面）。dst_slug 允许指向还没编译的页面 —— 本次只写不校验，
-- 校验（Lint：查断链、补索引）是后面的事，不在这里提前做。
CREATE TABLE IF NOT EXISTS wiki_links (
  src_page_id INTEGER NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
  dst_slug    TEXT NOT NULL,
  label       TEXT,
  PRIMARY KEY (src_page_id, dst_slug)
);

-- ================================================================
-- 概念层（PRD §5.7.1）：LLM Wiki 真正的页面单位
--
-- **为什么要多这一层**：话题页（上面那三张表）回答的是「这一批文章讲了什么」，
-- 它是**人工目录的话题**做单位，一页覆盖几十上百篇 —— 那本质是话题综述，
-- 不是 Wiki。Wiki 形态要求：① 页面单位是**概念**（有名有姓的术语，由 LLM
-- 从素材里发现，不是人预先定的）；② 页与页之间**连成网**；③ 新素材进来时
-- **只更新受影响的页面**（知识因此能持续生长，这就是 Karpathy 说的复利）。
-- 实测 LLM 从标题+摘要里抽出的概念质量很高（FDE / Rerank / 认知负荷 /
-- 餐饮食材周期…），且自带编号归属，所以这条路走得通。
-- ================================================================
CREATE TABLE IF NOT EXISTS wiki_concepts (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  slug          TEXT NOT NULL UNIQUE,   -- URL 片段（如 c-rerank）：概念改名不改链接
  name          TEXT NOT NULL,          -- 概念名（「Rerank」「认知负荷」）：保留模型给的写法，用于显示
  name_key     TEXT,                   -- **归一化后的名字，查重与判重全用它**：
                                       -- LLM 在不同批次会把同一个概念写成
                                       -- 「AI Agent」/「AI-Agent」，按原名去重会分裂成
                                       -- 几个几乎一样的节点、还会撞 slug 唯一约束（实测踩到）。
                                       -- 唯一性由下面的**唯一索引**承担（不能用列约束：
                                       -- SQLite 的 ALTER TABLE ADD COLUMN 不支持加约束，
                                       -- 老库升级必须靠这个索引，见 db._migrate）。
  topic_id     INTEGER REFERENCES themes(id) ON DELETE SET NULL,
                               -- **归属话题**（「食材周期」→ 餐饮经营与连锁）：
                               -- 概念是横跨素材的术语，但用户要按「几大方向」浏览库，
                               -- 所以每个概念都归到一个话题（质心相似度最高者），
                               -- 主题树展开话题时列出概念，概念页头部显示归属。
                               -- LLM 点名/向量归属都不改它 —— 归属只看质心。
  definition    TEXT,                   -- 一句话定义，来自素材，不是教科书
  keywords      TEXT,                   -- json 数组：合并时的别名/英文写法，用于去重
  centroid      BLOB,                   -- 成员文章画像的均值（L2 归一）：新文章该归谁靠它
  article_count INTEGER DEFAULT 0,      -- 成员文章数
  link_count    INTEGER DEFAULT 0,      -- 与其它概念有多少条互链（网里的度数）
  role          TEXT DEFAULT 'found',   -- found=LLM 发现 | manual=人工补充
  status        TEXT DEFAULT 'active',  -- active | archived（下架概念页，不删成员）
  pending       INTEGER DEFAULT 0,      -- **增量生长**：有多少篇新文章命中这个概念、还没被编译进去
  compiled_at   TEXT,                   -- 上次编译时间；与 members 变动对比就知道是不是过期
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
-- 查重走 name_key（归一化名），不是 name —— 同一个概念的多种写法要合并成一条，
-- 见 wiki_concepts.normalize_name 的说明。索引而非列约束，老库升级才能补上。
-- ⚠️ 依赖 _migrate 新增列的索引**不能写在这里**：executescript 先跑、
-- _migrate 后跑，老库此时还没有新列，建索引会直接报 no such column（实测踩过）。
-- 那类索引统一放 db.init_db 的 migrate 之后。
CREATE UNIQUE INDEX IF NOT EXISTS idx_wiki_concepts_name_key ON wiki_concepts(name_key);
CREATE INDEX IF NOT EXISTS idx_wiki_concepts_pending ON wiki_concepts(pending);

-- 概念 ↔ 文章归属。**这是 Wiki 的"原料表"**：页面内容由它编译出来，
-- 增量生长也靠它（新文章进来先落这张表、concept.pending 加一，再等编译）。
CREATE TABLE IF NOT EXISTS wiki_concept_articles (
  concept_id  INTEGER NOT NULL REFERENCES wiki_concepts(id) ON DELETE CASCADE,
  article_id  TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
  sim         REAL,                     -- 该文章与概念质心的相似度（向量算的）
  role        TEXT DEFAULT 'suggested', -- confirmed=LLM 点名的 | suggested=向量补进来的
  created_at  TEXT NOT NULL,
  PRIMARY KEY (concept_id, article_id)
);
CREATE INDEX IF NOT EXISTS idx_wca_article ON wiki_concept_articles(article_id);

-- 概念页挂在 wiki_pages 上，用 concept_id 区分「概念页」与「话题综述页」。
-- 加列而不是另建一张表：渲染、引用、角标、双链全都只认 wiki_pages，
-- 两类页面共用同一套展示与校验，**不为两种形态各写一遍**。
-- 老库靠 _migrate() 补列（CREATE TABLE IF NOT EXISTS 不会给已存在的表加列）。
