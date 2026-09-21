---
name: MyRag
description: 微信文章收藏系统 MyRag 的入口。用户输入 MyRag 并附微信文章链接时使用。职责两件：① 抓取全文与配图（调本 Skill 自带脚本，零依赖）② 按系统配置判类并编排调用本地 MyRag 系统的 API 完成入库。分类的名字、数量、划分标准都由使用者在工作台「分类设置」里自定义，所以判类前必须先读 /api/categories，不要在本文档里硬找分类。不做存储、不做检索——那两件事由本地系统承担。触发词："MyRag"。
version: 2.3
created: 2026-09-11
updated: 2026-09-14
---

# MyRag · 微信文章分流器

## 职责边界（先读这条）

**本 Skill 负责「抓 + 判」。系统负责「存 + 查」。**

| | 谁做 | 做什么 |
|---|---|---|
| **抓取全文与配图** | **本 Skill**（`scripts/wx_fetch.py`，零依赖） | 直连微信抓正文、元数据、配图 |
| 判类、摘要、标签、概念抽取、项目核对、编排 | **本 Skill**（Agent 判断） | 全部需要判断的工作 |
| 落盘、切段、向量化、存储、检索、展示、GitHub 核查 | **系统**（本地 FastAPI + SQLite） | 全部确定性工作 |

**越过边界的两种错误**：
- ❌ Skill 往正式存储写文件（`data/raw/`、`data/media/`、`wxk.db`）——正式存储只有一个写入口，就是后端的 ingest
- ❌ 让系统去抓取、去猜类别——系统不联网抓取、不调 LLM

**允许的中间态**：Skill 可读写 `data/_stage/`（抓取落地区，ingest 成功后由系统清理）。

## 触发

用户输入 **`MyRag`**，后面附一条或多条链接：微信文章（`mp.weixin.qq.com/s/...`）、
微信图片合辑（分享卡片），或其他网页。

用户不会说别的，这 5 个字母就是全部触发词。

## 路径常量

```
系统根目录  {{MYRAG_HOME}}/
抓取脚本    {{SKILL_DIR}}/scripts/wx_fetch.py
venv 解释器 {{MYRAG_HOME}}/.venv/bin/python
stage 目录  {{MYRAG_HOME}}/data/_stage/
系统文档    {{MYRAG_HOME}}/PRD.md

**解释器约定（踩过坑）**：
  · `{{SKILL_DIR}}/scripts/wx_fetch.py` —— **零依赖**，系统 `python3` 直接跑即可；
  · `{{MYRAG_HOME}}/scripts/` 下的脚本（ocr_images / reindex / eval / fetch_rerank…）——
    **依赖都装在 `{{MYRAG_HOME}}/.venv` 里，必须用 `.venv/bin/python` 跑**。
    用系统 `python3` 会报「缺少 pyobjc / yaml / FlagEmbedding」之类，不是脚本坏了。

注：Skill 包装在 Agent 客户端的 skills 目录（本机为 {{SKILL_DIR}}），
系统根由安装时写入，两者不在同一目录——**路径一律以上面两条为准，不要自己拼相对路径。**

注：若上面两条路径指向的目录在本机并不存在，说明这个 Skill **还没装好**（占位符没被替换掉）。

这时**不要自己猜系统根在哪，也不要手改本文件**，而是把下面这条命令交给使用者执行——
本 Skill 就是靠它部署的，它会同时写好「系统根」与「Skill 目录」两个路径，并装好依赖：

```bash
# 在使用者 clone 下来的 MyRag 目录里执行；--skill-dir 指向本 Skill 当前所在的目录
bash scripts/install.sh --skill-dir "<本 Skill 所在目录>"
```

装完再重读本文件，路径常量就正确了。使用者不熟悉命令行时，直接把这条命令原文发给他。
```

## 第 0 步：确保系统在跑（不可跳过）

后端是**按需启动**的：不随开机启动，也可能被用户手动停掉。所以每次调用都先探活，
不通就用脚本拉起。

> **本步就是「自动拉起」的唯一入口 —— 不要给后端装开机自启 / LaunchAgent / 常驻服务。**
> 单人低频工具，常驻收益为零、维护成本为正。需要它跑的时候，这个脚本会负责拉起。

```bash
curl -s --max-time 3 http://127.0.0.1:8765/api/health
```

| 结果 | 动作 |
|---|---|
| 返回 JSON 且 `status == "ok"` | 继续 |
| 不通 | `bash {{MYRAG_HOME}}/scripts/start.sh`（内含 15 秒健康检查轮询，成功即打印 pid 与 URL），然后再探一次 |
| 仍不通 | **停止，报告"后端起不来"及其报错**。不许绕过系统自己写正式存储 |

**两个实测坑（不处理会静默失败，务必照做）**：

1. **必须把后端与本次会话的 shell 生命周期绑在一起。** 在工具里跑 `start.sh`，脚本 exit 的那一刻，
   后端会随 shell 一起被杀掉——健康检查当时是通过的，下一次调用就 Connection refused，看起来像"自己挂了"。
   做法：把 `start.sh` 放进一个长时间存活的调用里（脚本跑完接一个 `sleep`），让它整轮都待在后台。
2. **启动前必须清掉 WorkBuddy 的删除守卫环境变量**：
   `env -u CODEBUDDY_SAFE_DELETE_BULK_STATE_DIR -u CODEBUDDY_TOOL_CALL_ID -u CODEBUDDY_SAFE_DELETE_BULK_GUARD bash start.sh`。
   否则后端会继承这三个变量，`ingest` 成功写库后清理 stage 时 `shutil.rmtree` 撞上 bulk-delete guard，
   抛 `SystemExit(1)` → HTTP 500（日志里是 `shim/sitecustomize.py _check_bulk_delete_guard`）。
   表现极具迷惑性：**头几条能成功、后面开始 500**（守卫按累计量触发）。
   已入库但报 500 时，重跑会得到 `UNIQUE constraint failed: articles.url_canon`——说明那条其实已经进去了，
   查库确认即可，不要重复提交。（守卫只在 python 侧，不影响 `rm` 命令。）

配套脚本（都在系统根的 `scripts/` 下）：`start.sh` 启动 / `stop.sh` 停止 / `status.sh` 查状态。

两条约定：

- **拉起后不要停**。本次用完让它在后台待着，下次调用会直接命中健康检查。
- 用户自己要看工作台时，用的也是同一条 `start.sh`，与本流程无关。

## 第 1 步：算 canon + 查重（先做，避免白抓）

微信分享链接带 `mpshare` / `scene` / `srcid` / `#rd` 等追踪参数，同一篇文章两次转发的 URL 字符串不同。**先去追踪参数，再比对**：

```bash
python3 {{SKILL_DIR}}/scripts/wx_fetch.py \
  --urls-file /tmp/myrag_urls.txt --canon-only
```

输出 `[{url, url_canon}]`。这一步是**纯字符串计算，不联网、不抓取**。

把这些 `url_canon` 提交给后端：

```
POST http://127.0.0.1:8765/api/check-urls
{ "url_canons": ["https://mp.weixin.qq.com/s/...", "..."] }
→ { "dups": [ { "url_canon": "...", "id": "T-20260101-01" } ] }
```

命中 `dups` 的直接记 `skip_dup`（附上后端给的已有 `id`），**不抓取**。剩余链接进入第 2 步。

## 第 1.5 步：链接分流（文章 vs 视频号）

`wx_fetch.py` 按链接类型自动分派（结果里的 `kind` 字段会告诉你走了哪条路）：

| 链接 | kind | 处理 |
|---|---|---|
| `mp.weixin.qq.com/s/...` 文章 | `wechat` | 解析 `js_content` 正文 |
| 微信图片合辑（分享卡片） | `wx_album` | 无正文模板，取文案 + 全部图片（图片在页面 JS 的 `picture_page_info_list` 里，**不在 HTML 标签中**） |
| 其他网页 | `web` | 通用正文定位（`<article>` → `<main>` → 文本最长的块，尽力而为） |
| 文件直链（`.pdf/.zip/.mp4`…） | — | 判 `unsupported_file` 拒收，明确告诉用户改存网页版或直接下载 |
| `weixin.qq.com/sph/...`（视频号） | — | **不走抓取**（判 `bad_url`），改走转写链路 |

> ⚠️ **本节路径全部按 WorkBuddy 的默认位置写**（`~/.workbuddy/...`）。换了 Agent 客户端
> （Claude Code 是 `~/.claude/`、Codex 是 `~/.codex/`），或把 `video-transcript` 装在了别处，
> 请先按实际位置替换再执行——**不要照抄这串路径**。视频号是可选功能，文章链路不受影响。

**动手前先体检**——缺环要明确告诉用户缺什么，不要把原始报错抛回去：

```bash
ls ~/.workbuddy/skills/video-transcript/scripts/sph_resolver.py    # 转写 Skill
ls ~/.workbuddy/credentials/yuanbao_state.json                      # 元宝登录态
```

| 检查结果 | 动作 |
|---|---|
| 转写 Skill 不存在 | 停止视频这条，告诉用户需要先装 `video-transcript`（见 README「视频号」） |
| 登录态文件不存在 | 让用户先扫码：`sph_resolver.py --login` |
| 转写报错且疑似登录态过期 | 引导重新 `--login` 扫码，不要反复重试 |

**不要试图绕过**：视频号没有别的可转写通道，元宝这一步躲不开。文章链接不受影响，可以继续处理。

转写链路：

```bash
PYTHONPATH= ~/.workbuddy/binaries/python/envs/video-transcript/bin/python \
  ~/.workbuddy/skills/video-transcript/scripts/transcript.py "<sph 链接>"
```

要点与硬约束：

| 项 | 值 |
|---|---|
| 转写 skill | `~/.workbuddy/skills/video-transcript/`（Backtthefuture/video-transcript，FunASR 本地转写） |
| venv | `~/.workbuddy/binaries/python/envs/video-transcript/` |
| 运行前缀 | **必须 `PYTHONPATH=`**——WorkBuddy 的 python shim 会把 sdist 解包的 EEXIST 变成异常 |
| 登录态 | 腾讯元宝扫码一次（`sph_resolver.py --login`），存 `~/.workbuddy/credentials/yuanbao_state.json`；失效时重新 `--login` |
| 媒体留存 | 默认音频转完即删，不保留视频——符合「只要文字」的约束 |
| 产物 | `outputs/<日期>_<标题>_transcript.md`（原始）与 `_预整理.md`（带时间戳小标题）；元数据在 `_outputs.json` |

入库规则（视频转录条目专属）：

1. **正文用 `_预整理.md`**，入库前对 ASR 专名错误做确定性替换修正（如 `l l m vki`→`LLM Wiki`、`R a g`→`RAG`），并在正文头部声明「视频转录 + 已做 ASR 专名修正」，文末附修正对照表。
2. **source 固定格式**：`视频转录 @ <账号名>`（账号名取自 outputs.json 的 title 前缀或视频页）。
3. **published_at 视频号拿不到**，取转写时间，`content_note` 注明「发布时间未知，取转写日」。
4. **review_flag = 1**：口播稿有 ASR 噪声与观点性内容，一律进待复核。
5. **concepts 从简**（≤3 个）：口播信息密度低，只抽核心概念，不复述广告。
6. **payload.json 手工构造**：`content_md` 直接放 payload 里（后端 `ingest.py` 从 payload 读正文），`url_canon` = 原始 sph 链接。
   **漏写 `url_canon` 必炸（实测 2026-09-15）**：后端从 payload 取 canon（`ingest.py` 的 `payload.get("url_canon") or ""`），
   不落回 `url` 字段。漏写 → 第一条以空 canon 入库成功，**之后每一条都报
   `IntegrityError: UNIQUE constraint failed: articles.url_canon`**。
   修复：删掉那条空 canon 条目（`DELETE /api/articles/<id>?confirm=yes`），补上 `url_canon` 重跑；
   **重跑要用新的 `stage_token`** —— 旧 token 已写进 ingest_log，会被幂等判断拦成 `dup=true` 而静默跳过。
7. 首次使用需元宝扫码登录；`transcript.py --doctor` 检查依赖。
8. **写 `verdict` / `boundary` 之后要复查 `review_flag`**：`PATCH /api/articles/{id}` 的实现是「人工修正即清待复核」
   （`review_flag = body.get("review_flag", 0)`），只传 verdict 也会把规则 4 要求的 `review_flag = 1` 清成 0。
   需要保留待复核时，核查完再补一次 `PATCH {"review_flag": 1}`。

## 第 2 步：抓取（本 Skill 的核心动作之一）

把待抓链接写进一个临时文件（一行一条），然后：

```bash
python3 {{SKILL_DIR}}/scripts/wx_fetch.py \
  --urls-file /tmp/myrag_urls.txt \
  --stage-root "{{MYRAG_HOME}}/data/_stage"
```

`stdout` 返回 JSON 数组，每链接一条：

```json
[
  { "idx": 1, "url": "https://mp.weixin.qq.com/s/...", "ok": true,
    "kind": "wechat",
    "title": "某篇示例文章",
    "source": "示例公众号", "author": "...",
    "published_at": "2026-09-10T18:05:00+08:00",
    "chars": 10216, "images": 3, "embedded": [],
    "token": "a1b2c3", "fail_reason": null }
]
```

`kind` ∈ `wechat`（公众号文章）/ `wx_album`（图片合辑）/ `web`（其他网页）。

**图片合辑（`kind=wx_album`）**：正文主体是图片、文字只有文案，语义检索基本搜不到它——
按普通文章入库即可，但要清楚它更多是「存档」而非「可检索」，可建议用户归到一个
「直接全文」分类（§分类设置）。

判定：

| 情况 | 动作 |
|---|---|
| `ok=false` | **拒收**，记 `failed` + `fail_reason`，不入库、不重试 |
| `ok=true` | 进入第 3 步 |

**失败原因枚举**（报给用户时用右列的「怎么办」，别只抛枚举值）：

| 原因 | 含义 / 怎么办 |
|---|---|
| `verify_page` | 微信返回了验证/异常页（反爬）。稍后重试或换网络，**不是链接错了** |
| `network` | 网络或 HTTP 错误 |
| `bad_url` | 不是 http/https 链接（含视频号，见第 1.5 步） |
| `unsupported_file` | 文件直链（pdf/zip/mp4…）。改存网页版，或直接下载文件 |
| `not_article` | 微信域名但不是文章页、也没认出图片合辑。让用户在微信里「用浏览器打开」后重新复制链接 |
| `need_render` | **JS 渲染的页面**（非微信链接常见）：HTML 里只有外壳、正文为空。**不是反爬、也不是页面坏了** —— 换 `render_fetch.py` 渲染抓取（见下节），**别让用户重试或换网络** |
| `parse_failed` | 抓到页面但定位不到正文（可能要登录或页面结构特殊） |

**判定验证页看特征词，不看体积** —— 图片合辑等非文章页的 HTML 也可能很小，
误报成「验证页」会让用户往反爬方向白折腾。反过来也成立：微信有一种**没有文案的验证页**
（body 只有空的 `weui-msg` 占位、`<title>` 为空），特征词一个都不出现，
只留下 `PAGE_MID='mmbizwap:secitptpage/verify.html'`——`VERIFY_HINTS` 里已收
`secitptpage/verify` 兜这一种；不认它就会把一个明确的验证页误报成 `not_article`，
把用户引向"重新复制链接"的错误动作。

**`source` 为空 = 抓取降级**：仍然入库，但必须置 `review_flag = 1`。

**`embedded` 非空**：文章含内嵌视频或视频号卡片，未下载（一期不做视频），正文里已留 `> [视频] <链接>` 痕迹。**在结果清单里注明，不要当没这回事。**

抓取产物落在 `data/_stage/<token>/`：`payload.json`（给 ingest）、`content.md`（给你读）、`img/NN.png`。

## 第 2.5 步：配图过滤与 OCR（不可跳过）

**为什么要这步**：微信文章的配图大半是**无信息载体**（小图标、表情、分割线、引导关注图、
动图）。实测它们占全库体积的 **96%**，而没人会看。更麻烦的是：**图片里的文字完全搜不到** ——
图片合辑类文章甚至「正文 0 字」，检索里等于不存在。

**分两道**：

| 道 | 在哪做 | 做什么 |
|---|---|---|
| **L1 硬规则** | `wx_fetch.py` **抓取时自动**（你不用管） | 最小边 <100px / 长宽比 >8 / GIF → **不落盘** |
| **L2 OCR 判定** | **这一步你要跑** | 对留下的图 OCR：文字 ≥10 字 → 保留并把文字写进正文；<10 字 → 删图 + 去引用 |

```bash
{{MYRAG_HOME}}/.venv/bin/python {{MYRAG_HOME}}/scripts/ocr_images.py --stage <token>
```

> ⚠️ **必须用 `.venv/bin/python`，不能用系统 `python3`。**
> OCR 依赖（pyobjc）装在 `{{MYRAG_HOME}}/.venv` 里，系统 python 找不到，会报「缺少 pyobjc」。
> **凡是 `{{MYRAG_HOME}}/scripts/` 下的脚本都要这样跑** —— 那里面的脚本依赖 venv。
> 唯一的例外是本 Skill 自带的 `wx_fetch.py`：它**零依赖**，用系统 `python3` 就行。

它做三件事：

1. 把 OCR 文字以 `> 图片文字：` 的形式**附在正文对应图片引用之后** —— 图片内容从此可检索；
2. 删掉无文字的图，**并去掉正文里对应的引用行**（否则入库后是破图）；
3. **同步 `payload.json` 的 `images`** —— 后端是按那份清单决定入库哪些图的，不同步会不一致。

**顺序不能反：先 OCR、再删。** 反了就是连内容一起丢；而"弃载体、保语义"才是对的
（PRD §5.6）。跳过这步的代价是永久的：图里的信息此后永远搜不到。

**L1 过滤掉多少，抓取日志里会报**（例：「已过滤 22 张无信息配图（尺寸过小 10 张、动图 12 张）」），
**在最后的结果清单里向用户提一句** —— 这是"入库更干净"的证据。

## JS 渲染页面（`need_render`）—— 非微信链接常遇到

**症状**：抓取返回 `fail_reason = need_render`，detail 说「HTML 里只有外壳、正文为空」。

**这不是反爬、也不是链接错了、更不是页面坏了** —— 是单页应用（SPA），
内容由 JS 异步渲染，静态 HTTP 请求天生拿不到。**不要建议用户重试或换网络**
（那会把人带到完全错误的方向）。

**处理**：换用渲染抓取（真的开一个浏览器把页面跑起来）：

```bash
{{MYRAG_HOME}}/.venv/bin/python {{MYRAG_HOME}}/scripts/render_fetch.py "<URL>"
```

它会：渲染 → 等待正文挂上来 → 取正文 → **按同一套 L1 规则过滤配图** →
落在**与 wx_fetch 完全一致的 stage 结构**里。

**所以拿到 token 后，接原来的第 2.5 / 3 / 5 步即可，后续一步都不用改。**

**前置**（一次性，约 180MB）：浏览器引擎

```bash
{{MYRAG_HOME}}/.venv/bin/python -m playwright install chromium
```

**它为什么要单独一个脚本**：`wx_fetch.py` 的硬约束是零依赖（Skill 侧要能在
没装任何包的机器上跑），而渲染必须要 playwright。所以分工是
**wx_fetch 只负责识别、渲染在 `render_fetch.py` 里做**。

**报给用户时说清楚区别**：这不是「抓不到」而是「静态抓不到，已换渲染方式」。
若渲染后仍取不到正文（脚本会明说），那才可能是需要登录或正文在 iframe 里。

## 第 3 步：判类（本 Skill 的核心动作之二）

**分类由系统配置决定 —— 不要在本文档里找分类名。** 先拉一次配置：

```bash
curl -s http://127.0.0.1:8765/api/categories
```

返回的 `items[]` 就是这份部署的全部分类，每个字段都有用途：

| 字段 | 用途 |
|---|---|
| `key` | **要提交的 `category` 值**（是 `key`，不是 `name`） |
| `name` | 显示名，只用于向用户回报 |
| `criteria` | **判类标准，以它为准**（本文档不重复写） |
| `principles` | **使用者的收录原则** —— 判断价值、定结论、决定核查侧重都以它为准（第 4、6 步用） |
| `is_default` | 为 `1` 的那个 = 判不准时落这里 |
| `retrieval` / `features` | 入库后怎么处理，系统自己按配置做，你不用管 |

流程：

1. **读 `data/_stage/<token>/content.md`**（不要只读标题）
2. 照各分类的 `criteria` 判定，把命中的 `key` 填进第 5 步请求体的 `category`

**拿不准时的口径（以此为准）**：

| 情况 | 怎么做 |
|---|---|
| 能与某一类的 `criteria` 对上（哪怕把握不大） | **判给它**，并置 `review_flag = 1` —— 进待复核队列等用户确认 |
| 与所有类的 `criteria` 都对不上 | 落 `is_default = 1` 那一类，同样置 `review_flag = 1` |

一句话：**「拿不准」不等于「随便塞进默认类」**。能判就按最接近的判（标复核让用户兜底），
真的判不了才落默认类。别为了"安全"把所有模糊内容都堆进同一个类 —— 那样待复核队列
会失去筛选意义，用户也不爱看。

**拿不准不要回报请示**：判类犹豫、形态特例（无口播空稿、同一内容多账号转述等）
一律不写进交付汇报、不列「待用户定夺」，直接标 `review_flag = 1` 进待复核队列 ——
用户会自己看。汇报只讲已定的结论，不把判断权推回去。

（`review_flag` 的其余触发条件，以第 4 步读到的 `review_when` 规则为准。）

## 第 4 步：生成抽取物

**两处配置都要读**：`/api/rules` 管「产物要求」，第 3 步拉到的
`categories[].principles` 管「按**使用者本人的标准**该怎么看这篇文章」。

**数量与写法以规则为准，不要照本文档的旧数字来。** 先拉一次规则：

```bash
curl -s http://127.0.0.1:8765/api/rules
```

| 规则键 | 用在哪 |
|---|---|
| `summary_max_chars` | 摘要字数上限 |
| `summary_guide` | **摘要要说什么** —— 以它为准（使用者可能调过） |
| `tags_max` | 标签数量上限 |
| `concepts_max` | 概念数量上限 |
| `review_when` | 什么情况该置 `review_flag = 1` |

**还有一条不来自 `/api/rules`，而来自第 3 步的分类配置**：`categories[].principles`
= **使用者本人的收录原则**（他关心什么、什么算值得看）。

**摘要要按它写**，不能只按 `summary_guide` 写。两者分工：
`summary_guide` 说的是"摘要该包含哪几个要素"（**形状**），
`principles` 说的是"这个人到底在意什么"（**重点**）。

例：`principles` 写了「我关心能不能自托管、讨厌被营销话术带节奏」，
摘要就该优先给自托管结论、主动点出夸大处，而不是平均罗列功能。

**该类 `principles` 为空时**：按 `summary_guide` 写即可，**不要自己编标准**。

| 项 | 哪些分类要 | 说明 |
|---|---|---|
| `summary` | 都要 | 长度按 `summary_max_chars`；**说什么按 `summary_guide`** |
| `tags` | 都要 | 最多 `tags_max` 个，领域/技术/场景词 |
| `concepts` | `features` 含 `concepts` 的分类 | 最多 `concepts_max` 个：概念名 + 一句定义 + 关联概念（互链） |
| `app_card` | `features` 含 `repo_card` 的分类 | 此步留空，第 6 步核查后填 |

两句"看配置，别猜"：**分类**看 `/api/categories` 的 `features`；**产物要求**看 `/api/rules`。
分类名和规则都能被使用者改，写死在文档里的数字一定有对不上的时候。

## 第 5 步：入库（id 由后端分配 —— 你不要自己编）

**不要自己生成 `id`。** 编号是 `<A|T|I>-<YYYYMMDD>-<NN>`，其中 `NN` = 「该前缀该日已有条数 + 1」——这必须查库才算得准。你编必然撞号（重跑、并发、同日多篇都会撞）。

```
POST http://127.0.0.1:8765/api/ingest
{
  "stage_token": "a1b2c3",
  "category": "<第 3 步从 /api/categories 判定的 key>",
  "published_at": "2026-08-27T10:00:00+08:00",
  "title": "...", "source": "...", "author": "...",
  "url": "...",
  "summary": "...", "tags": ["..."],
  "review_flag": 0,
  "concepts": [ { "name": "...", "definition": "...", "related": ["..."] } ]
}
→ { "ok": true, "dup": false, "id": "T-20260101-01" }
```

- **请求体里没有 `id`，`id` 只出现在响应里。** 后端按该分类配置的 `prefix` 定前缀、定日期（取 `published_at` 的发布日，不是今天）、取下一个序号。
- **`dup = true`** = 这篇已在库（`url_canon` 命中，或同一个 `stage_token` 重复提交）。**按"跳过"处理，不要当失败重试。**
- **正文与配图不在请求体里**——后端从 `stage_token` 对应的 `payload.json` 与 `img/` 读。请求体只带批注。

后端负责：分配 id、落原文、归位配图、切段、向量化、写库、清理 stage。

## 第 6 步：带 GitHub 核查能力的分类必须核对（不可跳过）

**适用判据**：该分类 `features` 含 `repo_card`（不按分类名判断）。

1. 调 `POST /api/articles/{id}/verify` —— 后端拉 GitHub 客观数据（license / 语言 / star / 最近提交 / 创建日期 / 最近发版）
2. **然后自己读一遍仓库**（README、topics、release 记录），核对**文章宣称**与**仓库实情**的差距
3. 差距写入 `boundary`（能力边界）与 `evidence`（含来源 URL 与核实时间）

**一篇讲到多个仓库时**（如「今日 GitHub 热榜三款 skills」）：卡片的客观指标（`repo_url` / `license` / `stars` / `last_release`…）**只放文章主体（头条）那一个仓库**，不许把几个库的数字混着填；其余仓库的指标与核对结论**逐个写进 `boundary`**，点名各自库 + 星数 + 协议 + 发版。文里只以 `owner/repo` 文本出现、没给完整 URL 的仓库，也要自己去核，不因地址不完整就跳过。

**核查侧重与 `verdict` 都以该分类的 `principles` 为准**（第 3 步就能拿到）：

- `principles` 写了他在意什么 → **核查就优先回答那几个问题**。
  例：若写明「我关心能不能自托管」，先查部署方式与依赖，而不是先罗列功能。
- `verdict`（`try` / `watch` / `dead`）**按 `principles` 里写的判定口径定** ——
  那段文字就是使用者本人的标准，**不要另立一套你自己的**。
- `principles` 为空时：按通用口径（成熟度、维护活跃度、能否自托管）判断，
  并在 `boundary` 里写清你用的是哪套口径。

**为什么这步不能省**：这类内容的价值不在文章里，在仓库里。自媒体常夸大（蹭热词、单方面贴标签、把 roadmap 说成现状）。**只复述文章 = 这篇白收了。**

## 第 7 步：输出结果清单

```
入库 3 条 ｜ 跳过 1 条 ｜ 失败 1 条

A-20260101-01  示例项目 A              开源项目  观察（v0.1 / 无 release / 蹭 world model 词）
T-20260101-01  示例文章 B      底层技术  5 个概念，已互链
I-20260101-01  示例文章 C            理念思考  —

跳过：https://... （已存在）
失败：https://... （verify_page 环境异常验证页）
```

**逐条一行，不许合并成"已处理完毕"。**

## 第 8 步：用户说「打开看看 / 我看看」时

确保后端在跑（第 0 步那套），然后把工作台打开，**不要替用户筛选或搜索**：

```bash
open "http://127.0.0.1:8765/"      # macOS；Linux 用 xdg-open，Windows 用 start
```

服务是按需启动的，打开前先确认健康检查通过，否则用户会看到一个空白页。

## 抓取硬约束（实测，不可违反）

| 项 | 值 |
|---|---|
| UA | 桌面 Chrome UA |
| 正文容器 | `#js_content` |
| 标题 | `<meta property="og:title">`，回退 `var msg_title` |
| **公众号** | **`id="js_name"` 标签内文本**，回退 `data-nickname="..."` 属性 |
| 作者 | `<meta property="og:article:author">` |
| 发布时间 | `var ct`（秒级时间戳） |
| 配图属性 | `data-src`（不是 `src`） |
| **配图下载** | **必须带 `Referer: https://mp.weixin.qq.com/`**，否则 403 |
| 失败判据 | 200 但 HTML < 20KB 且正文为空 = 验证页 |

**这些细节由抓取脚本承担，你不必自己写抓取代码。** 列在这里是为了你判断失败原因。

## 硬约束（不可违反）

1. **判类只在入库时做一次**，此后不重判（除非用户要求）。
2. **不许把摘要当正文存。**
3. **带 `repo_card` 能力的分类必须核实**，不许只复述文章里的数字。
4. **收到验证页必须拒收**，不许写空壳入库。
5. **失败必须说清楚**，不许静默跳过，不许编造成功。
6. **判类可以错，但要自知**——模糊就标 `review_flag = 1`。
7. **不写正式存储**：`data/raw/`、`data/media/`、`wxk.db` 只由后端写。Skill 只写 `data/_stage/`。
8. **抓取走脚本，不自己写抓取代码**。脚本坏了就改脚本，不要在对话里临时拼一个。
9. **不自己编 `id`**。编号由后端分配并在响应里返回；`dup=true` 记跳过，不当失败重试。

## 反模式

| ❌ 错误做法 | 为什么错 |
|---|---|
| 用 `var nickname` 提公众号名 | **微信页面里没有这个变量**，实测得到空值；正确位置是 `id="js_name"` |
| 把 `content.md` 全文打印到 `stdout` | 长文可达 11000 字，一次十几篇会撑爆上下文；只读你要用的 |
| 绕过系统，直接把文章写成 md 文件 | 正式存储只能有一个写入口，写文件等于数据散落、检索查不到 |
| 后端不通就自己凑合存 | 同上，且下次查不到 |
| 带 repo_card 能力的分类只写文章里宣称的 star 数 | 自媒体常夸大，必须查仓库实况 |
| 配图不管 | 公众号删文后图就没了 |
| 判类犹豫时随口选一个 | 应按 `criteria` 判给最接近的一类 + 标 `review_flag`；**只有全都对不上时**才落 `is_default` 类 |
| 自己拼 `id = T-20260101-01` 提交 | `NN` 要查库才算得准，你编必然撞号；后端分配，你只读响应里的 `id` |
| 视频条目 payload 只放 `content_md`、不放 `url_canon` | 首条以空 canon 入库、其余全撞唯一约束；且重跑同 token 会被幂等拦成 dup，看着像"跳过"其实是没进去 |
| 把 `dup=true` 当失败重试 | 那是"已存在"的正常结果，重试只会再拿一次 `dup` |
