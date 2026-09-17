# -*- coding: utf-8 -*-
"""MyRag 系统配置：端口、路径、模型、维度、检索参数（PRD §12.2）。

**配置优先级：环境变量 > 项目根的 `.env` 文件 > 本文件的默认值。**

所有路径默认由本文件位置推导，所以**项目可以放在任何目录**（不需要改代码），
要挪数据（比如放到外置大盘）也只是改一个环境变量的事。

常用环境变量：
    MYRAG_HOST          监听地址（默认 127.0.0.1，只给本机用）
    MYRAG_PORT          监听端口（默认 8765）
    MYRAG_DATA_DIR      数据目录（默认 <项目根>/data）—— 库、原文、配图都在这
    MYRAG_DB_PATH       数据库文件（默认 <MYRAG_DATA_DIR>/wxk.db）
    MYRAG_STAGE_DIR     抓取落地区（默认 <MYRAG_DATA_DIR>/_stage）
    MYRAG_MODEL_PATH    BGE-M3 权重目录（默认自动探测 modelscope 缓存）
    GITHUB_TOKEN        GitHub API token（可选，只为提高限额）
"""

from __future__ import annotations

import os
from pathlib import Path


# ---------------------------------------------------------------- .env 加载

def _load_dotenv(path: Path) -> None:
    """极简 .env 解析 —— 不为了读 10 行配置去引入 python-dotenv 依赖。

    格式：一行一对 `KEY=value`；空行与 `#` 开头忽略；值可带单/双引号。
    **已存在的环境变量优先**，不会被 .env 覆盖（方便临时 `MYRAG_PORT=9000 bash start.sh`）。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


ROOT = Path(__file__).resolve().parent.parent
_load_dotenv(ROOT / ".env")


def _env(name: str, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _env_path(name: str, default: Path) -> Path:
    v = _env(name)
    return Path(v).expanduser().resolve() if v else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 路径
ROOT = ROOT                                  # 项目根（由本文件位置推导，可整体挪动）
DATA_DIR = _env_path("MYRAG_DATA_DIR", ROOT / "data")
RAW_DIR = DATA_DIR / "raw"
MEDIA_DIR = DATA_DIR / "media"
STAGE_DIR = _env_path("MYRAG_STAGE_DIR", DATA_DIR / "_stage")
DB_PATH = _env_path("MYRAG_DB_PATH", DATA_DIR / "wxk.db")
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
WEB_DIR = ROOT / "web"
INDEX_HTML = WEB_DIR / "index.html"
SERVER_LOG = DATA_DIR / "server.log"
PID_FILE = DATA_DIR / "server.pid"
SKILL_DIR = ROOT / "skill"                   # 仓库自带 Skill 包（安装脚本把它部署到客户端）

# ---------------------------------------------------------------- 服务
HOST = _env("MYRAG_HOST", "127.0.0.1")
PORT = _env_int("MYRAG_PORT", 8765)
VERSION = "2.1"

# ---------------------------------------------------------------- Embedding（PRD §7.1）
EMBED_MODEL_ID = "BAAI/bge-m3"
EMBED_DIM = 1024
EMBED_BATCH = 8
CHUNK_MAX_TOKENS = 1024
CHUNK_OVERLAP_RATIO = 0.15
MODEL_SOURCE = _env("MYRAG_MODEL_SOURCE", "modelscope")

# 本机实测缓存路径（modelscope 1.40 采用 HF 风格 `--` 布局）。
# 只作为回退——正常情况由 resolve_model_path() 探测。
KNOWN_MODEL_PATH = os.path.expanduser(
    _env("MYRAG_KNOWN_MODEL_PATH",
         "~/.cache/modelscope/models/BAAI--bge-m3/snapshots/master")
)

# ---------------------------------------------------------------- 检索（PRD §8）
RRF_K = 60
TOP_PER_ROUTE = 30
SEARCH_TOP_N = 20
SPARSE_KNN_CAP = 200000  # 有过滤条件时 KNN 取全量的上限，防呆

# ---------------------------------------------------------------- Reranker 精排（PRD §8.4）
# 为什么要它：BGE-M3 是 **bi-encoder**（问题与文档分别编码后比距离），只能回答
# 「这段话和问题语义像不像」，回答不了「到底答没答到点上」。初检 Top-N 里混进的
# 「语义相近但无用」的段会原样进 prompt，LLM 照单全收、反被带偏。
#
# RERANK_ENABLED 默认开，但**加载失败会自动跳过**（见 rerank.py），
# 所以老部署即使没下 reranker 权重也不会挂 —— 只是没有精排而已。
RERANK_ENABLED = _env("MYRAG_RERANK", "1").strip().lower() not in ("0", "false", "no", "off")
RERANK_MODEL_ID = _env("MYRAG_RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
# 进入精排的候选数。**本机实测**（CPU、M-series、bge-reranker-v2-m3）：
#   top_n=20 → 1849ms   top_n=12 → 1090ms   top_n=10 → 908ms   top_n=8 → 744ms
#
# ⚠️ **不要为了省延迟砍这个值**：精排的价值恰恰在于「把 RRF 排错的捞回来」，
# 候选砍到 10 时，评测集里 q05 的目标段（RRF 第 9 位附近）就捞不回来了 ——
# MRR 从 1.000 掉回 0.911。要省延迟请改 RERANK_MAX_CHARS。
RERANK_TOP_N = int(_env("MYRAG_RERANK_TOP_N", "20"))
# 送进 cross-encoder 的文本上限（字符）—— **延迟的第一大来源，也是调它的地方**。
# cross-encoder 耗时几乎正比于序列长度，而完整段落可达 1024 token。实测：
#   top_n=12 时  480 字符 → 1615ms   320 → 1121ms   240 → 852ms   160 → 604ms
# 不截断（送全文）时整次检索从 0.6s 涨到 4.7s。
#
# ⚠️ **但别为了延迟砍到 320 以下**：评测集显示 160 字符会损失精度
# （20 候选时 MRR 从 1.000 掉到 0.900 —— 太短了判断不准）。
# 320 是实测的「效果与延迟」平衡点；想更快请改用更小的 reranker 模型。
RERANK_MAX_CHARS = int(_env("MYRAG_RERANK_MAX_CHARS", "320"))
KNOWN_RERANK_PATH = os.path.expanduser(
    _env("MYRAG_KNOWN_RERANK_PATH",
         "~/.cache/modelscope/models/BAAI--bge-reranker-v2-m3/snapshots/master")
)

# ---------------------------------------------------------------- 分类（PRD §5.5）
# 分类**不写死在代码里**。出厂默认只在这里（播种用），运行时一律以数据库
# categories 表为准（工作台「分类设置」可改名字、增删分类、改划分标准、改检索方式）。
#
#   key        稳定标识，写进 articles.category；入库后不建议改（改 = 迁移数据）
#   prefix     编号前缀，只影响**未来**生成的 id（已有 id 不变）
#   criteria   划分标准，注入 Skill 供 Agent 判类
#   retrieval  rag      = 切段 + 向量化，参与语义/词法检索
#              fulltext = 不建索引（省算力），按时间浏览、点开读全文
#   features   repo_card（入库后调 GitHub 核查）/ concepts（抽概念、互链）
RETRIEVAL_MODES = ("rag", "fulltext")
RETRIEVAL_NAMES = {"rag": "走 RAG 检索", "fulltext": "直接全文（不建索引）"}
FEATURE_NAMES = {"repo_card": "GitHub 核查", "concepts": "概念互链"}

# 展示样式：空字符串 = 按 features 自动推导（向后兼容，老行为不变）
DISPLAY_MODES = ("", "cards", "list", "compact")
DISPLAY_NAMES = {
    "": "自动（有仓库核查用卡片，其余用列表）",
    "cards": "卡片网格（适合能展示仓库指标的分类）",
    "list": "列表（标题 + 摘要 + 指标 + 标签）",
    "compact": "紧凑列表（只留标题与日期，适合量大归档）",
}

# 入库规则（PRD §5.6）：业务规则可配，技术规则写死
#   type: int | text   —— 工作台据此决定用输入框还是多行文本框
RULE_DEFS = [
    {
        "key": "summary_max_chars", "name": "摘要字数上限", "type": "int",
        "default": "120",
        "hint": "建议 80–200。原来的 60 字太短，常把关键信息挤掉。",
    },
    {
        "key": "summary_guide", "name": "摘要要说什么", "type": "text",
        "default": "说清「它能做什么 + 与同类比的关键差异 + 最重要的限制」，"
                   "让人不点开原文也知道值不值得看。不要复述标题。",
        "hint": "这段话会直接写进 Agent 的抽取指引，用它来控制摘要的信息密度。",
    },
    {
        "key": "tags_max", "name": "标签数量上限", "type": "int",
        "default": "6",
        "hint": "标签用于横向串联（点标签能看全部同标签文章），建议 3–6。",
    },
    {
        "key": "concepts_max", "name": "概念数量上限", "type": "int",
        "default": "5",
        "hint": "仅对勾了「概念互链」能力的分类生效。",
    },
    {
        "key": "review_when", "name": "什么情况进「待复核」", "type": "text",
        "default": "判类拿不准、抓取降级（来源为空）、或与已有条目可能重复时。",
        "hint": "写得越具体，Agent 越不容易把该复核的条目漏掉。",
    },
]

DEFAULT_CATEGORIES = [
    {
        "key": "app", "name": "开源项目", "prefix": "A", "retrieval": "rag",
        "features": ["repo_card"], "is_default": 0, "sort_order": 1,
        "criteria": "主体是具体的开源项目、产品或工具，或某种技术方案的落地实现。"
                    "特征：出现仓库名、产品名、「XX 开源了」、「我试了 XX」、性能对比数字。",
        "principles":
            "我收藏开源项目的原则：\n\n"
            "· 我关心的是「今天能不能用上它」——能否自托管、能否离线跑、文档够不够"
            "我照着启动。名气大不大不重要，有没有正式 release、多久没提交、"
            "issue 有没有人管才重要。\n\n"
            "· 凡标题或正文出现「全球首个」「颠覆」「取代 XX」这类话，先去仓库核实。"
            "文章说的与仓库实况不一致时，以仓库为准，并写明差在哪。\n\n"
            "· 我讨厌被文章带节奏。若文章自己引用的原话与标题矛盾（标题说「XX 开源版」、"
            "正文却引作者说「不希望被视为 XX 的开源版」），必须标出来。\n\n"
            "· 「值得试」= 有正式 release + 三个月内有提交 + 我能自托管跑起来；"
            "缺一项即「观望」；半年没动或已归档即「已死」。",
    },
    {
        "key": "tech", "name": "底层技术", "prefix": "T", "retrieval": "rag",
        "features": ["concepts"], "is_default": 1, "sort_order": 2,
        "criteria": "主体是底层原理、架构、方法论。特征：讲「怎么实现」「为什么这样设计」，"
                    "不绑定具体产品。边界模糊时落这一类。",
        "principles":
            "我收藏技术文章的原则：\n\n"
            "· 我要的是「能不能照着做出来」——有可运行的代码、命令、配置示例，"
            "而不是只讲概念。通篇「赋能」「闭环」却没具体做法的，在摘要里点出来。\n\n"
            "· 讲原理的要能说清「为什么这样设计、替代方案差在哪」，只堆术语不算讲到。\n\n"
            "· 文中给了性能数字或对比结论时，注明它有没有给测试条件；"
            "没给条件的数字只能当宣传看。\n\n"
            "· 这类没有可核查的仓库对象，所以不做「值得试/已死」判断。"
            "摘要要说清「读完能学会做什么、需要什么前置知识」。",
    },
    {
        "key": "idea", "name": "理念思考", "prefix": "I", "retrieval": "rag",
        "features": [], "is_default": 0, "sort_order": 3,
        "criteria": "主体是观点、趋势、商业判断。特征：无技术细节，或技术只是论据。",
        "principles":
            "我收藏观点文章的原则：\n\n"
            "· 我关心「这个判断有没有依据」——是拍脑袋，还是有一手数据/案例支撑。"
            "只有断言没有论据的，在摘要里点明。\n\n"
            "· 作者身份与立场要写进摘要（谁说的、代表谁的利益），"
            "因为这决定我该怎么读它。\n\n"
            "· 互相矛盾的观点都要留——收藏不是选边，是为了以后能对照着看。"
            "若库中已有相反观点的文章，注明「与某篇观点相左」。\n\n"
            "· 这类不做「值得试」判断。摘要要说清「核心主张是什么、他反对什么」。",
    },
]

ID_MAX_RETRY = 50
REVIEW_DUE_DAYS = 90  # 带 repo_card 能力的分类，复评周期

# ---------------------------------------------------------------- GitHub 核查（PRD §6.3）
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or None
GITHUB_API = "https://api.github.com"
GITHUB_TIMEOUT = 20

# ---------------------------------------------------------------- 上传/文本限制
MAX_INGEST_TEXT_CHARS = 200000

# ---------------------------------------------------------------- 抓取脚本（可选）
# URL 规范化（canon）必须"查重与入库同一实现"（PRD §5.2）。首选路径：
#   1) 环境变量 MYRAG_FETCH_SCRIPT 显式指定
#   2) 仓库自带的 skill/scripts/wx_fetch.py（开源版把 Skill 包收进了仓库）
#   3) 已安装的 WorkBuddy Skill 位置
# 三个都拿不到时，退回本模块内置的等价实现（ingest._inline_canon）。
FETCH_SCRIPT_CANDIDATES = [
    os.path.expanduser("~/.workbuddy/skills/MyRag/scripts/wx_fetch.py"),
]


def fetch_script_path() -> Path | None:
    """找出可用的 wx_fetch.py（用于取 canonical_url）。找不到返回 None。"""
    cands = []
    env = _env("MYRAG_FETCH_SCRIPT")
    if env:
        cands.append(Path(env).expanduser())
    cands.append(SKILL_DIR / "scripts" / "wx_fetch.py")
    cands.extend(Path(p).expanduser() for p in FETCH_SCRIPT_CANDIDATES)
    for p in cands:
        try:
            if p.is_file():
                return p
        except Exception:
            continue
    return None


def _exists(p) -> bool:
    try:
        return bool(p) and Path(p).exists()
    except Exception:
        return False


def resolve_model_path() -> str:
    """解析 BGE-M3 本地权重目录。

    优先级：环境变量 → modelscope 本地缓存探测 → 实测已知路径 → 联网下载。
    前三级都不联网，保证权重已缓存时可完全离线运行（PRD §15）。
    """
    env = os.environ.get("MYRAG_MODEL_PATH")
    if _exists(env):
        return str(env)

    try:
        from modelscope import snapshot_download

        p = snapshot_download(EMBED_MODEL_ID, local_files_only=True)
        if _exists(p):
            return str(p)
    except Exception:
        pass

    if _exists(KNOWN_MODEL_PATH):
        return KNOWN_MODEL_PATH

    # 兜底：联网拉取（首次部署时唯一会联网的路径）
    from modelscope import snapshot_download

    return str(snapshot_download(EMBED_MODEL_ID))


# 权重文件的常见后缀。判断「模型在不在」必须**看文件**，不能只看目录：
# 中断的下载会留下空壳目录，于是加载要到更深处才失败，错误信息也变难懂（实测踩过）。
_WEIGHT_GLOBS = ("*.safetensors", "*.bin", "*.ckpt", "*.h5", "*.msgpack")


def _has_weights(path) -> bool:
    """目录里是否真的存在模型权重文件（而不只是个空壳）。"""
    if not path:                     # 环境变量未设置时是 None，不能直接丢给 Path()
        return False
    p = Path(path)
    if not p.is_dir():
        return False
    return any(next(p.rglob(g), None) is not None for g in _WEIGHT_GLOBS)


def resolve_rerank_path(allow_download: bool = False) -> str:
    """解析 reranker 本地权重目录。

    **默认只查本地、绝不联网**（与 `resolve_model_path` 的关键区别）：
    权重可能有 2GB 级、下载要几分钟，而 `get_model()` 是在**检索请求路径**上
    被调用的 —— 一旦在那里触发下载，一次普通检索就会挂住直到超时（已踩过）。

    embedding 模型可以联网兜底，是因为没有它整个系统不可用；reranker 只是增强项，
    **没有它检索照常工作**，所以没理由让用户在一次搜索里等下载。

    要下载请走显式入口：`.venv/bin/python scripts/fetch_rerank.py`
    """
    env = os.environ.get("MYRAG_RERANK_PATH")
    if _has_weights(env):
        return str(env)

    try:
        from modelscope import snapshot_download

        p = snapshot_download(RERANK_MODEL_ID, local_files_only=True)
        if _has_weights(p):
            return str(p)
    except Exception:
        pass

    if _has_weights(KNOWN_RERANK_PATH):
        return KNOWN_RERANK_PATH

    if not allow_download:
        raise RuntimeError(
            "reranker 权重不在本地（%s）。这不影响检索，只是没有精排。\n"
            "要启用请先下载：.venv/bin/python scripts/fetch_rerank.py"
            % RERANK_MODEL_ID)

    from modelscope import snapshot_download

    return str(snapshot_download(RERANK_MODEL_ID))


def ensure_dirs() -> None:
    for d in (DATA_DIR, RAW_DIR, MEDIA_DIR, STAGE_DIR):
        d.mkdir(parents=True, exist_ok=True)
