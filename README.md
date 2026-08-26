# blog_writer — 文章生成 Agent

一个基于 **LangGraph** 的初级多 Agent 项目：给定一个题目，自动完成中文**科普文章**的创作。

三个 Agent（大纲子智能体（调研选题一体）→ 写作 → 审校/润色）依次协作，共享一份状态。LLM 通过 **DeepSeek V4 Flash**（`deepseek-v4-flash`，OpenAI 兼容端点）调用，调研 Agent 通过 **DuckDuckGo** 联网搜索真实资料，并对检索内容做 LLM 审查，保证素材真实可靠。

## 目录

- [功能特性](#功能特性)
- [工作流程](#工作流程)
- [项目结构](#项目结构)
- [环境要求](#环境要求)
- [安装](#安装)
- [配置 API Key](#配置-api-key)
- [运行](#运行)
- [工作原理](#工作原理)
- [重大改动记录](#重大改动记录)
- [待办事项](#待办事项)

## 功能特性

- 🤖 多 Agent 分工协作：大纲子智能体、写作、审校润色
- 🧩 **大纲子智能体**（自包含独立子图）：检索、生成、保障一体——内部 搜索 → 审查素材 → 生成提纲 → 自检；素材不足会**自动补搜**、提纲不合格会重试，最终保证返回可用的提纲
- ⚡ **并行搜索**：一轮提出 3-5 个聚焦查询，**并发**执行搜索，节省时间、覆盖更多关键词
- 🔍 通过 `web_search` 工具（DuckDuckGo，免费无需 Key）联网搜索真实资料，素材带来源链接
- 🧐 **检索内容审查**：搜索结果先交给 LLM 审查（来源可信度 / 信息含量 / 相关度），剔除营销味、宽泛无信息、脏数据内容，再基于可靠素材生成提纲
- 🎯 **科普定位**：写作要求生活化类比、术语先解释、通俗但有深度；内容以真实素材为准
- ✍️ **按章节并发写作**：提纲拆成 5-7 个章节，各章节**并行**写作后按序合并成全文，生成更快
- 🧐 **章节写作子智能体**：每个章节独立完成"初稿 → 自检 → 条件重写"——自检看篇幅/标题/要点覆盖，合格直接通过（省 token），不合格才重写
- 🔄 审校 Agent 输出质量分（0-100），**不合格打回时只重写问题章节**、每章带专属修改意见，其余保留（最多 2 次）
- 👀 **可选人工介入**：大纲生成后暂停，由你确认/修改提纲再继续（`--human-review`，默认全自动）
- 🧩 审校节点使用结构化 JSON 输出，方便程序读取分数/意见与问题章节
- 📝 输出**标准 Markdown**：一级标题 + `##` 小节，可直接渲染
- 🧭 **多模型路由**：所有 LLM 调用按角色路由（research/outline/split/write/edit/revise_outline），调用失败自动 fallback 切备用模型；`--model` 可全局换模型（目前注册 DeepSeek，加第二个模型只需在 `MODEL_REGISTRY` 注册一个规格）
- 📦 **搜索素材缓存（知识复用）**：搜索原始结果与整题审查后素材按 7 天 TTL 存进 SQLite（`.cache/`），同一题目/相似关键词跨运行复用，跳过重复联网与重复审查；`--clear-search-cache` 可手动清空
- 📊 可选接入 **LangSmith** 追踪每次 Agent 执行与 LLM 调用（`llm.py` 用 `@traceable` 上报）
- 💻 纯命令行使用，零界面依赖

## 工作流程

```
题目 ──→ [大纲子智能体] ──→ 可用提纲 ──→（可选人工确认）──→ [写作子 Agent] ──→ [审校/润色 Agent] ──→ 成品文章
             │（自包含）                                 │（自包含：拆章→并行写各章→合并）        │
             └─ 搜索 → 审查素材 → 生成提纲 → 自检        └─────（不合格只重写问题章节，最多 2 次）──────┘
                  （素材不足则补搜 / 提纲不合格则重试）
```

- **大纲子智能体**（自包含独立子图）：一次到位——① 让模型一次提出 3-5 个**具体聚焦**的查询，**并发**调用 `web_search`（DuckDuckGo，`region=cn-zh`）联网搜索；② 用 `MATERIAL_REVIEW_PROMPT` 让 LLM **审查搜索结果**，剔除营销味/宽泛/脏数据，整理出可靠素材；③ 基于素材生成提纲并自检（非空 + 够长）。**素材不足会自动补搜（≤2 轮），提纲不合格会换提示重试（≤2 次），仍不行用自身知识兜底**——搜索、审查、提纲三者闭环，保证返回的提纲一定可用。
- **人工确认（可选）**：加 `--human-review` 后，大纲生成完会停下展示提纲供你确认——回车通过 / 输入修改意见重新生成 / `#` 开头粘贴自己的完整大纲 / `q` 退出（稍后可用 `--resume` 续跑）；不加该参数则全自动、完全跳过。
- **写作子 Agent**（自包含独立子图）：把提纲拆成 5-7 个结构化章节（标题 + 要点 + 对应素材），用 LangGraph `Send` **并行**触发各章节写作——每个章节是 `section_writer.py` **自包含子智能体**：并发扩写成 Markdown（`##` 小节）后先**自检**（篇幅 / 标题，不调 LLM），不合格且未到上限则**条件重写**（自检意见作为反馈塞回生成提示），合格才结束。所有章节写完后按 id 合并成完整文章；被审校打回时只重写问题章节并携带该章专属意见。
- **审校/润色**：检查错别字、语病，给出质量分（0-100），指出问题章节并给出**每章专属**的修改意见；润色后输出全文（保持 Markdown 标题结构）；不合格则**只打回问题章节重写**（最多 2 次），这就是图中的"条件分支 + 循环"。

## 项目结构

```
blog_writer/
├── requirements.txt        # langgraph, langgraph-checkpoint-sqlite, openai, ddgs, langsmith
├── state.py                # ArticleState：节点间共享的状态定义
├── llm.py                  # LLM 调用统一封装 call_llm() / chat()（消息形状 + @traceable，委托 model_router 选模型）
├── model_router.py         # 多模型路由：ModelSpec 注册表 + role→模型链 + fallback + client 缓存
├── search_cache.py         # 两级搜索缓存：query→搜索结果 + topic→审查后素材，SQLite 跨进程复用
├── prompts.py              # 各 Agent 的中文 system prompt（含 MATERIAL_REVIEW_PROMPT 素材审查）
├── logging_config.py       # 统一日志：stderr 简洁 + 文件详细，--verbose 控制级别
├── langsmith_config.py     # LangSmith 配置：校验环境变量、读取 .env
├── .env.example            # 环境变量模板（复制为 .env 填写 Key）
├── agents/
│   ├── __init__.py
│   ├── tools.py            # web_search 联网搜索工具（DuckDuckGo，region=cn-zh + 标题清洗）
│   ├── outliner.py         # 大纲子智能体（自包含子图：搜索→审查→生成→自检→补搜/重试→兜底）
│   ├── human_review.py     # 人工介入节点：interrupt() 暂停 + Command(resume) 续跑（--human-review）
│   ├── writing.py          # 写作子 Agent（自包含子图：拆章 split / Send 分发 fan_out / 合并 merge）
│   ├── section_writer.py   # 章节写作子智能体（自包含子图：初稿 → 自检 → 条件重写）
│   └── editor.py           # 审校/润色节点（JSON 结构化输出，含问题章节；解析失败重试）
├── graph.py                # LangGraph 编排：大纲 →（人工确认）→ 写作子 Agent → 审校（含打回循环）
└── main.py                 # CLI 入口（交互循环 + --resume 断点续跑 + checkpointer 装配）
```

## 环境要求

- Python 3.10 及以上
- 一个可用的 DeepSeek API Key（[DeepSeek 开放平台](https://platform.deepseek.com) 申请）
- 联网（调研 Agent 搜索资料需要；搜索不可用时会自动回退到模型自身知识）

## 安装

```bash
cd blog_writer
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 配置 API Key

```bash
export DEEPSEEK_API_KEY=sk-你的密钥
```

> 建议写入 `~/.zshrc` 或 `~/.bashrc`，避免每次都要设置。

也可以把 Key 写进项目根目录的 `.env` 文件（复制 `.env.example` 为 `.env` 填写），程序启动时会自动读取。`.env` 已被 `.gitignore` 忽略，不会提交泄露。

### 可选：接入 LangSmith 追踪

设置以下环境变量即可让 LangGraph 自动上报每次执行过程（含 LLM 调用）：

```bash
export LANGCHAIN_TRACING_V2=true
export LANGCHAIN_API_KEY=lsv2_你的Key   # 在 https://smith.langchain.com 生成
export LANGCHAIN_PROJECT=blog_writer
```

未配置时程序正常运行、不做追踪。

## 运行

```bash
python main.py "为什么越来越多的人选择远程办公"
```

可选：把成品保存到文件。

```bash
python main.py "为什么越来越多的人选择远程办公" --output out.md
```

可选：大纲生成后暂停，由人工确认/修改后再继续（默认全自动、不介入）。

```bash
python main.py "为什么越来越多的人选择远程办公" --human-review
```

运行到大纲生成后会停在人工审阅提示，提供四种选择：

```
👀 人工审阅大纲（--human-review 已开启）：
----------------------------------------
（当前提纲）
----------------------------------------
  [回车]          确认大纲，继续写作
  [输入文字]      作为修改意见，重新生成大纲
  [# 开头的内容]  视为你粘贴的完整新大纲，直接采用
  [q]             退出（稍后可用 `--resume` 从断点继续）
```

断点续跑：每次运行会生成一个 `thread_id`（日志开头可见）。中途退出（`q` / Ctrl+C / 断电）后，用同一 thread_id 从上次中断点接着写，已生成的大纲、素材、章节草稿都不丢失：

```bash
python main.py --resume <thread_id>   # 从断点继续（带相同的 --human-review 参数）
python main.py "题目" --human-review --in-memory   # 对比：进程内 checkpointer，退出即失
```

可选：覆盖全局默认模型（多模型路由入口；`--model` 后跟 `MODEL_REGISTRY` 里的名字）：

```bash
python main.py "为什么越来越多的人选择远程办公" --model deepseek-v4-flash
```

可选：清空搜索素材缓存（`.cache/`，缓存默认 7 天自动过期，此命令强制清空后退出）：

```bash
python main.py --clear-search-cache
```

运行时会依次显示各阶段提示：

```
  🔍 大纲子智能体搜索素材（第 1 轮）…
    🔍 搜索：远程办公 兴起 原因 数据 疫情 增长
    🔍 搜索：远程办公 好处 效率 员工满意度 调查 数据
    🔍 搜索：混合办公 趋势 企业 案例 远程办公 未来      ← 3-5 个查询并发搜索
  🧐 审查搜索结果素材…
  📋 大纲子智能体生成提纲（第 1 次）…
→ 拆分章节…
  📑 已拆分为 7 个章节
→ 写作章节[0]：引言…  ×7 并行写作
→ 合并章节完成
→ 审校/润色中…
```

最后打印成品文章及质量信息，例如 `成品文章（质量分 90/100，审校 1 次）`。输出为标准 Markdown（一级标题 + `##` 小节），可直接渲染。

> 说明：搜索后端用 DuckDuckGo（免费、无需 Key），指定 `region=cn-zh` 提升中文结果质量，并清洗 DuckDuckGo 偶发的拼接标题。若搜索不可用，大纲子智能体会自动回退到模型自身知识，流程不会中断。

## 工作原理

1. **状态（`state.py`）**：用 `TypedDict` 定义 `ArticleState`，字段在节点间传递：

   | 字段 | 写入者 | 含义 |
   |---|---|---|
   | `topic` | 用户（`main.py`） | 输入的题目 |
   | `outline` | 大纲子智能体 | 可用的提纲 |
   | `sections` | 写作子 Agent（split） | 拆分出的章节列表（`id` + 标题 + 要点 + 素材，`id` 由程序按顺序补）；跨打回循环保留 |
   | `section_drafts` | 写作子 Agent（section_writer） | 各章节草稿（并行写入，按 id 合并，reducer 聚合）；跨打回循环保留 |
   | `failed_sections` | 审校节点 | 打回时需重写的章节及各自修改意见 [{id, feedback}] |
   | `draft` | 写作子 Agent（merge） | 合并后的全文草稿 |
   | `final_article` | 审校节点 | 润色后的文章（合格时为成品） |
   | `quality_score` | 审校节点 | 质量分（0-100） |
   | `passed` | 审校节点 | 是否通过质量检查 |
   | `revision_count` | 审校节点 | 已审校次数（控制循环上限） |
   | `outline_review_feedback` | 人工介入节点 | 人工修改意见；有值时回 `human_review` 先重写大纲再二次确认 |

2. **编排（`graph.py`）**：用 `StateGraph` 串联节点，审校后通过条件边 `should_continue` 决定结束还是打回重写。

   ```python
   graph.add_node("outline", build_outliner())             # 大纲子智能体（自包含子图）
   graph.add_node("human_review", human_review_node)       # 可选：人工确认/修改大纲
   graph.add_node("writing", build_writing_agent())        # 写作子 Agent（自包含子图：拆章+并行写章+合并）
   graph.add_node("edit", editor_node)                     # 审校
   graph.add_edge(START, "outline")
   graph.add_conditional_edges("outline", route_outline,    # --human-review 开关路由
                               {"human_review": "human_review", "writing": "writing"})
   graph.add_conditional_edges("human_review", route_review,  # 有意见→回 human_review 重写；无→writing
                               {"human_review": "human_review", "writing": "writing"})
   graph.add_edge("writing", "edit")
   graph.add_conditional_edges("edit", should_continue,
                               {"rewrite": "writing", "end": END})
   ```

   `should_continue` 读到 `passed` 为真或 `revision_count` 达到上限（`MAX_REVISIONS = 2`）就结束，否则打回 `writing`（**只重写 `failed_sections` 里的问题章节**）。`--human-review` 开启时，`outline` 后经条件边 `route_outline` 先进入 `human_review` 节点（`interrupt()` 暂停展示大纲）再到 `writing`；用户输入修改意见时经条件边 `route_review` 回 `human_review` 先重写大纲、再二次确认；默认关闭直接到 `writing`，该节点完全不执行。

3. **大纲子智能体（`agents/outliner.py`）**：一个编译好的**自包含子图**，挂到主图的 `outline` 节点，负责"检索 + 生成提纲"一体：
   - **搜索**：把对话交给模型（带 `web_search` 工具），模型一次提出 3-5 个具体查询，用 `ThreadPoolExecutor` **并发**执行搜索（上限 4）；
   - **审查**：用 `MATERIAL_REVIEW_PROMPT` 让 LLM 逐条审查搜索结果，剔除营销味、宽泛无信息、与主题无关、明显拼接的脏数据，整理出可靠素材；
   - **生成 + 自检**：基于素材生成提纲并自检（非空且不低于最低长度）；
   - **失败分类路由**：素材不足（搜索失败/审查太差）→ 补搜（≤2 轮）；提纲不合格 → 换提示重试（≤2 次）；都到上限 → 基于自身知识兜底。**保证最终一定返回可用的 outline**。
   - 补搜 / 重试计数（`search_round` / `outline_attempt`）都是子图私有键，不会泄漏回主图 state（对 langgraph 1.2.11 实测验证）。

4. **写作（`agents/writing.py` + `agents/section_writer.py`）**：`writing.py` 是**自包含子图**，内部先用 `split` 把提纲拆成 5-7 个结构化章节，再用 `Send` API **并行**触发 `write_section`；每个 `write_section` 实例是 `section_writer.py` 的**自包含子图**——初稿 → 启发式自检（篇幅 / 标题，不调 LLM）→ 不合格且未到上限则**条件重写**（自检意见塞回生成提示），合格直接结束。最后按 id 合并（`merge`）；打回只重写 `failed_sections` 里的问题章节（其余章节草稿保留），并把**该章节专属的审校意见**分别交给对应的重写章节，互不串味。`output_schema` 只把 `draft`/`sections`/`section_drafts` 写回主图（后两者是跨打回循环的共享通道）。

5. **审校（`agents/editor.py`）**：用 `json_mode=True` 让模型输出 JSON（分数/是否合格/意见/润色全文），解析后写入多个状态字段；润色时保持 Markdown 标题结构；**解析失败自动重试**（最多 2 次，提示附"不是合法 JSON"），重试耗尽才保守按通过处理，避免卡死循环。

6. **LLM 调用（`llm.py` + `model_router.py`）**：`llm.py` 通过 OpenAI 兼容端点访问模型；`call_llm()` 用于一次性问答（可选 JSON 模式），`chat()` 保留完整响应以便读取 `tool_calls`。两个函数都用 `@traceable` 装饰。**模型选择交给 `model_router.py`**——8 个调用点各带 `role=`，按 `ROLE_MODEL_MAP` 路由到候选模型链，调用失败自动 fallback 切下一个；`--model` 可覆盖全局默认。目前只注册 DeepSeek，加第二个模型只需在注册表加一个 `ModelSpec`。


## 重大改动记录

以下是项目演进过程中的关键改动，便于回顾每次变更的目的。

### v2.4 — 写作子 Agent：拆章 + 并行写章 + 合并封装为自包含子图

- **新增 `agents/writing.py`**：把"拆章（`split`）→ Send 并行触发章节写作子智能体（`write_section`）→ 按 id 合并（`merge`）"整条链收进一个**自包含子图**，作为主图单个 `writing` 节点挂载（`agents/writer.py` 迁入 `agents/writing.py`）。主图简化为 `outline →（可选人工确认）→ writing → edit`。
- **审校留在主图**：打回重写时主图**再次进入** writing 子图；`sections`/`section_drafts` 作为共享通道跨父子图双向流动，`split` 在 `revision_count>0` 时复用已拆章节（不调 LLM）、`fan_out` 只 Send 问题章节并携带该章专属意见，保住"只重写问题章节、其余草稿保留"语义。
- **双层 output_schema**：section_writer 子图限 `section_drafts`（消除子图内并行实例写冲突）+ writing 子图限 `draft/sections/section_drafts`（消除父图写冲突），主图永不出现并发写。
- **merge 显式按 id 排序**：`sorted(sections, key=id)` 拼装，不依赖列表序（原 enumerate 保证列表序==id 序，行为不变，更稳健）。

### v2.3 — 搜索素材缓存 + 知识复用（两级缓存）

- **新增 `search_cache.py`**：SQLite（`.cache/search_cache.db`）持久化两级缓存——`search_cache` 表（query → `web_search` 原始结果，相似关键词跨题目共享）+ `topic_materials` 表（topic → 审查后素材，同一题目跳过 搜索+审查）。
- **大纲不缓存**：它依赖 LLM 生成，缓存会让每次运行结果一模一样；只缓存"事实数据"。
- **TTL 7 天惰性失效**：读时发现过期即删即重新搜索；`--clear-search-cache` 手动清空。
- **单飞防抖**：同 query 并发搜索只真正请求一次，后到线程复用结果；锁只在 SQLite 读写处，不阻塞并行搜索。
- **兼容性**：outliner 的 `_run_search` 从 `web_search` 换为 `cached_search`（`web_search` 本身保持纯函数不变）；`agents/tools.py` 无改动。

### v2.2 — 多模型路由：角色路由 + fallback 链 + `--model` 覆盖

- **新增 `model_router.py`**：`ModelSpec` 注册表（默认只注册 deepseek-v4-flash）+ `ROLE_MODEL_MAP`（role → 候选模型链，`__default__` 哨兵跟随全局默认）+ `resolve_chain()`（优先级 显式 model > role 链 > 全局默认，带能力过滤）+ `call_with_fallback()`（API/传输异常自动切下一个模型）+ `get_client()`（按 provider 懒加载缓存）。
- **8 个调用点打 `role=`**：outliner（research/outline）、split、write、edit、revise_outline——将来可让不同环节用不同模型（如更贵更准的模型审校、便宜的模型写章）。
- **`--model <名字>` CLI**：一处覆盖全局默认模型，直观演示路由入口。
- **兼容性**：`call_llm`/`chat` 新增 `model`/`role` 为 **keyword-only** 参数，无参调用行为不变；client 改为懒加载（模块导入不再读 env，缺 key 在首次真实调用才报友好错误）。

### v2.1 — 写作/审校升级为真正 Agent：章节子智能体 + 审校重试

- **`write_section` 升级为自包含子智能体**（新增 `agents/section_writer.py`）：每个章节独立完成"初稿 → 自检 → 条件重写"。自检用**启发式**（篇幅 ≥ 120 字 / 以 `## ` 开头 / 覆盖要点），不调 LLM、确定性可测——**合格直接通过，省掉旧版"无条件自我反思一轮"的调用**；不合格且未到上限才重写（自检意见作为反馈塞回生成提示），达上限接受当前结果。
- **审校解析失败重试**：`editor_node` 的 JSON 解析从"失败即保守通过"改为"**重试最多 2 次**（提示附'不是合法 JSON'），耗尽才保守通过"；`revision_count` 不因重试多计。
- **踩过的坑（langgraph 1.2.11）**：Send 并行触发编译子图时，子图若把输入键（如 `topic`）原样写回父图会抛 `INVALID_CONCURRENT_GRAPH_UPDATE`——用 `output_schema` 限定子图只输出 `section_drafts` 根除；子图内部草稿键绝不能命名 `draft`（会覆盖父图合并结果），一律用 `section_text`。

### v2.0 — 正统 Human-in-the-Loop：interrupt + checkpoint 断点续跑

- **从同步 `input()` 升级为 LangGraph 正统 HITL**：`agents/human_review.py` 改用 `interrupt()` 暂停图、`Command(resume=...)` 续跑；交互逻辑（回车确认 / 意见重写 / `#` 粘贴大纲 / `q` 退出）移到 `main.py` 的统一 invoke 循环，提示仍走 stderr。
- **checkpoint 持久化**：默认 `SqliteSaver` 写 `.checkpoints/blog_writer.db`（`--in-memory` 可选 `MemorySaver` 对比学习）；每次运行生成 `thread_id` 作寻址单位。
- **断点续跑 `--resume <thread_id>`**：长文写作中途（`q` 退出 / Ctrl+C / 断电）后，用同一 thread_id 从上次中断点接着写，已生成的大纲/素材/章节草稿不丢失。
- **多轮修改自环**：输入修改意见后经 `route_review` 条件边回 `human_review` 节点，先按意见重写大纲、再二次确认（`outline_review_feedback` 状态字段驱动）。
- **踩过的坑**：`SqliteSaver.from_conn_string()` 返回 context manager，必须 `with` 解包取实例再传 `compile(checkpointer=...)`；`interrupt()` 无 checkpointer 时可暂停但状态不持久化、不可跨进程 resume。

### v1.9 — 可选人工介入（Human-in-the-Loop）：大纲确认环节

- **新增 `--human-review` 开关**（默认关闭，全自动）：大纲生成后、拆分章节前，由人工确认/修改提纲再继续——补上自动链路里"大纲方向对不对"的盲区。
- **介入点在大纲后**：返工杠杆最高的位置——改大纲只花 30 秒，省掉后面所有并行写作与审校打回的浪费。
- **交互方式**：`agents/human_review.py` 用同步 `input()`——回车确认 / 输入修改意见交 LLM 重写（`REVISE_OUTLINE_PROMPT`）/ `#` 开头粘贴完整新大纲 / `q` 退出；交互提示走 stderr，stdout 仍只留给成品文章。
- **实现**：开关经 `build_graph(enable_human_review=...)` 参数 + `outline` 后条件边 `route_outline` 路由；关闭时 `human_review` 节点完全不执行；打回重写循环不经过该节点，人工只确认一次。

### v1.8 — 日志系统化：logging 替代 print

- **新增 `logging_config.py`**：各模块的进度/告警输出从 `print` 改为标准 `logging`（`logger.info` / `logger.warning`），统一管理级别、格式与输出目标；成品文章仍用 `print` 输出到 stdout。
- **终端简洁 + 文件详细追踪**：日志固定走 stderr、stdout 留给成品文章（`python main.py "题目" > out.md` 重定向时日志不会混进产物）；文件默认写项目根目录 `blog_writer.log`（已 `.gitignore`），带时间戳/级别/模块名、始终记录 DEBUG，便于事后排查。
- **`--verbose` 控制级别**：默认 INFO，`--verbose` 降到 DEBUG；可用 `--log-file <路径>` 指定日志文件位置。

### v1.7 — 写作加入自我反思（Self-Reflection）

- **每个章节写完初稿后自我审视并改进**：`write_section` 现在分两轮——先用 `WRITE_SECTION_PROMPT` 写初稿，再用新增的 `SELF_REVIEW_PROMPT` 让模型审视自己的输出（内容扎实度 / 语言自然度 / 科普效果 / 衔接流畅度），直接输出改进后的章节。
- **提升初稿质量，减少外部审校打回次数**：自我反思环节属于 Reflexion 模式——模型先写、再自评、再改，相当于"自己先改一遍再交作业"。审校 Agent 仍作为外部质量把关存在，两道关卡互为补充。
- **成本说明**：每章节多一次 LLM 调用（约 +1 次/章），但因章节并行写作，墙钟时间只增加一轮 LLM 调用时长（约数秒）。

### v1.6 — Section 自带 id，"章节"成为自洽对象

- **`Section` 结构加 `id` 字段**（`state.py`）：章节 = `{id, title, points, materials}`。`id` 由 `split_sections` 程序补（`enumerate` 顺序编号），不依赖 LLM 输出，杜绝编号重复/缺失。
- **Send payload 缩到 3 个键**：`fan_out_write` 不再并列传 `section_id` + `section`，id 直接存在 `section["id"]` 里；`write_section` / `merge_sections` 都改为按 `section["id"]` 读写，`section_drafts` 的 key 与章节本体绑定、不再依赖列表位置。

### v1.5 — 审校意见按章节拆分（去掉全局 revision_feedback）

- **`failed_sections` 升级为 `[{id, feedback}]`**：每个问题章节带自己的专属修改意见，彻底移除 state 里的全局 `revision_feedback` 字段。
- **各章只见自己的意见**：打回重写时，`write_section` 只拿到本章节的 feedback，不再把整段意见塞给所有重写章节，避免"章节 A 照着章节 B 的意见瞎改"的串味问题。

### v1.4 — 按章节并发写作 + 搜索并行化

- **按章节并发写作**：提纲拆成 5-7 个结构化章节，用 LangGraph `Send` API **并行**写各章节再按序合并——写作阶段大幅提速（写作链封装在 `agents/writing.py`，内部 `split` / `fan_out_write` / `write_section` / `merge`）。
- **只重写问题章节**：审校 JSON 新增 `failed_sections` 字段，打回时只重新写作出问题的章节、其余保留；拆章结果在打回时复用、不重复调用模型。
- **搜索并行化**：大纲子智能体一轮提出 3-5 个查询，用 `ThreadPoolExecutor` 并发搜索（上限 4），节省时间、覆盖更多关键词。

### v1.3 — 大纲子智能体：自包含（搜索 + 审查 + 提纲一体）

- **新增 `agents/outliner.py`**：把"搜索素材 → 审查 → 生成提纲"收进一个独立的 LangGraph 子图，作为主图 `outline` 节点挂载（主图简化为 `outline → write → edit`，`research` 节点移除，`agents/researcher.py` 删除）。
- **补搜闭环**：子智能体内部按失败类型分类路由——素材不足（搜索失败/审查太差）会**自动补搜**（≤2 轮），提纲不合格会换提示重试（≤2 次），都到上限则用自身知识兜底，保证返回的提纲一定可用。解决"素材单向流入、烂素材只能硬写"的问题。
- **子图私有状态**：补搜/重试计数（`search_round` / `outline_attempt`）是子图私有键，不泄漏回主图 state（对 langgraph 1.2.11 实测验证）。

### v1.1 — 调研质量优化（搜索 → 审查 → 提纲）

- **检索内容 LLM 审查**（新增 `MATERIAL_REVIEW_PROMPT`）：搜索结果先由 LLM 逐条评估来源可信度、信息含量、相关度，剔除营销味（"一篇就够了""全面爆发"）、宽泛无信息、拼接脏数据，再基于可靠素材生成提纲。解决"搜索内容质量不高、过于宽泛"的问题。
- **科普定位**：写作端要求生活化类比、术语先解释、通俗但有深度；调研端要求"主题 + 具体方面"的聚焦查询词，不再是宽泛的单一主题词。
- **搜索质量**：`web_search` 指定 `region=cn-zh`（中文权威源更靠前），新增 `_clean_title()` 清洗 DuckDuckGo 偶发的多站点标题拼接。
- **Markdown 输出**：写作端必须输出标准 Markdown（一级标题 + `##` 小节），审校润色时保持标题结构，成品可直接渲染。

### v1.0 — 收敛与可观测性

- **调研收敛**：ReAct 循环改为两阶段（一次搜索 → 强制收敛出提纲），消除"搜索轮次用尽"反复搜索、浪费 token 的问题。
- **LangSmith 集成**：新增 `langsmith_config.py`、`.env.example`、`LANGSMITH.md`；`llm.py` 用 `@traceable` 上报每次 LLM 调用，可在 smith.langchain.com 查看完整执行追踪。
- **去 AI 味**：重写写作/审校 prompt，禁用套话与模板结构，要求长短句交错、具体数据落地。

## 待办事项

按优先级排列的优化方向，兼顾学习 Agent 技术与完善项目。

### P0 — 近期

- [x] **Self-Reflection 自我反思**（已实现 v1.7）：写作章节后让模型自我审视并改进，提升初稿质量，减少外部审校打回次数。
- [x] **logging 替代 print**（已实现 v1.8）：新增 `logging_config.py` 统一配置，`--verbose` 控制级别，日志同时输出到 stderr 与文件（默认 `blog_writer.log`）。

### P1 — 中期

- [x] **多模型路由**：`write_section` 用便宜模型（如 `deepseek-v4-flash`），`editor_node` 用更强模型（如 `deepseek-reasoner`），实现"任务难度分级路由"的成本优化。

### P2 — 远期

- [x] **Human-in-the-loop 中断点**（已实现 v2.0，大纲后介入 + checkpoint 断点续跑）：`--human-review` 走 LangGraph 正统 `interrupt()` + `Command(resume=...)`，默认 `SqliteSaver` 落盘；`q` 退出后可用 `--resume <thread_id>` 跨进程续跑；有修改意见时经 `route_review` 自环先重写大纲再二次确认。可选扩展：在 `merge` 后加第二介入点预览全文草稿，选"继续审校 / 直接输出 / 打回某章重写"。
- [x] **搜索素材缓存 + 知识复用**：搜索结果按 query hash 缓存到本地，过期 24h，跨文章复用，减少重复搜索。
- [ ] **多视角审校子智能体（Judge Panel）**：3 个并行审校角色——语言编辑（语病/流畅度）、事实核查（数据/引用准确性）、结构编辑（逻辑/衔接），独立打分取多数意见。
- [ ] **多智能体协作**


## License

MIT
