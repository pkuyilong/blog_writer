# b_writer — 文章生成 Agent

一个基于 **LangGraph** 的初级多 Agent 项目：给定一个题目，自动完成中文**科普文章**的创作。

三个 Agent（调研/选题 → 写作 → 审校/润色）依次协作，共享一份状态。LLM 通过 **DeepSeek V4 Flash**（`deepseek-v4-flash`，OpenAI 兼容端点）调用，调研 Agent 通过 **DuckDuckGo** 联网搜索真实资料，并对检索内容做 LLM 审查，保证素材真实可靠。

## 目录

- [功能特性](#功能特性)
- [工作流程](#工作流程)
- [项目结构](#项目结构)
- [环境要求](#环境要求)
- [安装](#安装)
- [配置 API Key](#配置-api-key)
- [运行](#运行)
- [工作原理](#工作原理)
- [常见问题](#常见问题)
- [重大改动记录](#重大改动记录)
- [学习与扩展方向](#学习与扩展方向)

## 功能特性

- 🤖 多 Agent 分工协作：大纲子智能体、写作、审校润色
- 🧩 **大纲子智能体**（自包含独立子图）：检索、生成、保障一体——内部 搜索 → 审查素材 → 生成提纲 → 自检；素材不足会**自动补搜**、提纲不合格会重试，最终保证返回可用的提纲
- ⚡ **并行搜索**：一轮提出 3-5 个聚焦查询，**并发**执行搜索，节省时间、覆盖更多关键词
- 🔍 通过 `web_search` 工具（DuckDuckGo，免费无需 Key）联网搜索真实资料，素材带来源链接
- 🧐 **检索内容审查**：搜索结果先交给 LLM 审查（来源可信度 / 信息含量 / 相关度），剔除营销味、宽泛无信息、脏数据内容，再基于可靠素材生成提纲
- 🎯 **科普定位**：写作要求生活化类比、术语先解释、通俗但有深度；内容以真实素材为准
- ✍️ **按章节并发写作**：提纲拆成 5-7 个章节，各章节**并行**写作后按序合并成全文，生成更快
- 🔄 审校 Agent 输出质量分（0-100），**不合格打回时只重写问题章节**、每章带专属修改意见，其余保留（最多 2 次）
- 👀 **可选人工介入**：大纲生成后暂停，由你确认/修改提纲再继续（`--human-review`，默认全自动）
- 🧩 审校节点使用结构化 JSON 输出，方便程序读取分数/意见与问题章节
- 📝 输出**标准 Markdown**：一级标题 + `##` 小节，可直接渲染
- 📊 可选接入 **LangSmith** 追踪每次 Agent 执行与 LLM 调用（`llm.py` 用 `@traceable` 上报）
- 💻 纯命令行使用，零界面依赖

## 工作流程

```
题目 ──→ [大纲子智能体] ──→ 可用提纲 ──→（可选人工确认）──→ [拆分章节] ──→ [并行写各章节] ──→ 合并 → [审校/润色 Agent] ──→ 成品文章
             │（自包含）                               ▲                           │
             └─ 搜索 → 审查素材 → 生成提纲 → 自检   └（不合格只重写问题章节，最多 2 次）┘
                  （素材不足则补搜 / 提纲不合格则重试）
```

- **大纲子智能体**（自包含独立子图）：一次到位——① 让模型一次提出 3-5 个**具体聚焦**的查询，**并发**调用 `web_search`（DuckDuckGo，`region=cn-zh`）联网搜索；② 用 `REVIEWER_PROMPT` 让 LLM **审查搜索结果**，剔除营销味/宽泛/脏数据，整理出可靠素材；③ 基于素材生成提纲并自检（非空 + 够长）。**素材不足会自动补搜（≤2 轮），提纲不合格会换提示重试（≤2 次），仍不行用自身知识兜底**——搜索、审查、提纲三者闭环，保证返回的提纲一定可用。
- **人工确认（可选）**：加 `--human-review` 后，大纲生成完会停下展示提纲供你确认——回车通过 / 输入修改意见重新生成 / `#` 开头粘贴自己的完整大纲 / `q` 退出；不加该参数则全自动、完全跳过。
- **拆分章节**：把提纲拆成 5-7 个结构化章节（标题 + 要点 + 对应素材）。
- **并行写作**：各章节**并发**扩写成 Markdown（`##` 小节），再按章节顺序合并成完整文章；被打回时结合审校意见修改。
- **审校/润色**：检查错别字、语病，给出质量分（0-100），指出问题章节并给出**每章专属**的修改意见；润色后输出全文（保持 Markdown 标题结构）；不合格则**只打回问题章节重写**（最多 2 次），这就是图中的"条件分支 + 循环"。

## 项目结构

```
b_writer/
├── requirements.txt        # langgraph, openai, ddgs, langsmith
├── state.py                # ArticleState：节点间共享的状态定义
├── llm.py                  # DeepSeek 调用封装 call_llm() / chat()（@traceable 上报 LangSmith）
├── prompts.py              # 各 Agent 的中文 system prompt（含 REVIEWER_PROMPT 素材审查）
├── langsmith_config.py     # LangSmith 配置：校验环境变量、读取 .env
├── .env.example            # 环境变量模板（复制为 .env 填写 Key）
├── agents/
│   ├── __init__.py
│   ├── tools.py            # web_search 联网搜索工具（DuckDuckGo，region=cn-zh + 标题清洗）
│   ├── outliner.py         # 大纲子智能体（自包含子图：搜索→审查→生成→自检→补搜/重试→兜底）
│   ├── writer.py           # 拆章 split / 并行写章节 write_section / 合并 merge
│   └── editor.py           # 审校/润色节点（JSON 结构化输出，含问题章节）
├── graph.py                # LangGraph 编排：大纲 → 拆章 → 并行写 → 合并 → 审校（含打回循环）
└── main.py                 # CLI 入口
```

## 环境要求

- Python 3.10 及以上
- 一个可用的 DeepSeek API Key（[DeepSeek 开放平台](https://platform.deepseek.com) 申请）
- 联网（调研 Agent 搜索资料需要；搜索不可用时会自动回退到模型自身知识）

## 安装

```bash
cd b_writer
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
export LANGCHAIN_PROJECT=b_writer
```

未配置时程序正常运行、不做追踪。详细说明见 `LANGSMITH.md`。

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
  [q]             退出
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
   | `sections` | 拆章节点 | 拆分出的章节列表（`id` + 标题 + 要点 + 素材，`id` 由程序按顺序补） |
   | `section_drafts` | 写章节节点 | 各章节草稿（并行写入，按 id 合并） |
   | `failed_sections` | 审校节点 | 打回时需重写的章节及各自修改意见 [{id, feedback}] |
   | `draft` | 合并节点 | 合并后的全文草稿 |
   | `final_article` | 审校节点 | 润色后的文章（合格时为成品） |
   | `quality_score` | 审校节点 | 质量分（0-100） |
   | `passed` | 审校节点 | 是否通过质量检查 |
   | `revision_count` | 审校节点 | 已审校次数（控制循环上限） |

2. **编排（`graph.py`）**：用 `StateGraph` 串联节点，审校后通过条件边 `should_continue` 决定结束还是打回重写。

   ```python
   graph.add_node("outline", build_outliner())             # 大纲子智能体（自包含子图）
   graph.add_node("human_review", human_review_node)       # 可选：人工确认/修改大纲
   graph.add_node("split", split_sections)                 # 拆章
   graph.add_node("write_section", write_section)          # 写单个章节（可并行多次）
   graph.add_node("merge", merge_sections)                 # 按序合并
   graph.add_node("edit", editor_node)                     # 审校
   graph.add_edge(START, "outline")
   graph.add_conditional_edges("outline", route_outline,    # --human-review 开关路由
                               {"human_review": "human_review", "split": "split"})
   graph.add_edge("human_review", "split")                 # 人工确认后进入拆章
   graph.add_conditional_edges("split", fan_out_write)     # 返回 [Send(...)×N] 并行写各章节
   graph.add_edge("write_section", "merge")
   graph.add_edge("merge", "edit")
   graph.add_conditional_edges("edit", should_continue,
                               {"rewrite": "split", "end": END})
   ```

   `should_continue` 读到 `passed` 为真或 `revision_count` 达到上限（`MAX_REVISIONS = 2`）就结束，否则打回 `split`（**只重写 `failed_sections` 里的问题章节**）。`--human-review` 开启时，`outline` 后经条件边 `route_outline` 先进入 `human_review` 节点（人工确认/修改大纲）再到 `split`；默认关闭直接到 `split`，该节点完全不执行。

3. **大纲子智能体（`agents/outliner.py`）**：一个编译好的**自包含子图**，挂到主图的 `outline` 节点，负责"检索 + 生成提纲"一体：
   - **搜索**：把对话交给模型（带 `web_search` 工具），模型一次提出 3-5 个具体查询，用 `ThreadPoolExecutor` **并发**执行搜索（上限 4）；
   - **审查**：用 `REVIEWER_PROMPT` 让 LLM 逐条审查搜索结果，剔除营销味、宽泛无信息、与主题无关、明显拼接的脏数据，整理出可靠素材；
   - **生成 + 自检**：基于素材生成提纲并自检（非空且不低于最低长度）；
   - **失败分类路由**：素材不足（搜索失败/审查太差）→ 补搜（≤2 轮）；提纲不合格 → 换提示重试（≤2 次）；都到上限 → 基于自身知识兜底。**保证最终一定返回可用的 outline**。
   - 补搜 / 重试计数（`search_round` / `outline_attempt`）都是子图私有键，不会泄漏回主图 state（对 langgraph 1.2.11 实测验证）。

4. **写作（`agents/writer.py`）**：先用 `split` 把提纲拆成 5-7 个结构化章节，再用 `Send` API **并行**写各章节（`write_section`），最后按序合并（`merge`）；打回只重写 `failed_sections` 里的问题章节（其余章节草稿保留），并把**该章节专属的审校意见**分别交给对应的重写章节，互不串味。

5. **审校（`agents/editor.py`）**：用 `json_mode=True` 让模型输出 JSON（分数/是否合格/意见/润色全文），解析后写入多个状态字段；润色时保持 Markdown 标题结构；解析失败时保守按通过处理，避免卡死循环。

6. **LLM 调用（`llm.py`）**：通过 OpenAI 兼容端点访问 DeepSeek；`call_llm()` 用于一次性问答（可选 JSON 模式），`chat()` 保留完整响应以便读取 `tool_calls`。两个函数都用 `@traceable` 装饰，LangSmith 启用时每次 LLM 调用会上报为独立的 run，便于在追踪面板查看。


## 重大改动记录

以下是项目演进过程中的关键改动，便于回顾每次变更的目的。

### v1.9 — 可选人工介入（Human-in-the-Loop）：大纲确认环节

- **新增 `--human-review` 开关**（默认关闭，全自动）：大纲生成后、拆分章节前，由人工确认/修改提纲再继续——补上自动链路里"大纲方向对不对"的盲区。
- **介入点在大纲后**：返工杠杆最高的位置——改大纲只花 30 秒，省掉后面所有并行写作与审校打回的浪费。
- **交互方式**：`agents/human_review.py` 用同步 `input()`——回车确认 / 输入修改意见交 LLM 重写（`REVISE_OUTLINE_PROMPT`）/ `#` 开头粘贴完整新大纲 / `q` 退出；交互提示走 stderr，stdout 仍只留给成品文章。
- **实现**：开关经 `build_graph(enable_human_review=...)` 参数 + `outline` 后条件边 `route_outline` 路由；关闭时 `human_review` 节点完全不执行；打回重写循环不经过该节点，人工只确认一次。

### v1.8 — 日志系统化：logging 替代 print

- **新增 `logging_config.py`**：各模块的进度/告警输出从 `print` 改为标准 `logging`（`logger.info` / `logger.warning`），统一管理级别、格式与输出目标；成品文章仍用 `print` 输出到 stdout。
- **终端简洁 + 文件详细追踪**：日志固定走 stderr、stdout 留给成品文章（`python main.py "题目" > out.md` 重定向时日志不会混进产物）；文件默认写项目根目录 `b_writer.log`（已 `.gitignore`），带时间戳/级别/模块名、始终记录 DEBUG，便于事后排查。
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

- **按章节并发写作**：提纲拆成 5-7 个结构化章节，用 LangGraph `Send` API **并行**写各章节再按序合并——写作阶段大幅提速（`agents/writer.py` 拆为 `split` / `fan_out_write` / `write_section` / `merge` 四部分）。
- **只重写问题章节**：审校 JSON 新增 `failed_sections` 字段，打回时只重新写作出问题的章节、其余保留；拆章结果在打回时复用、不重复调用模型。
- **搜索并行化**：大纲子智能体一轮提出 3-5 个查询，用 `ThreadPoolExecutor` 并发搜索（上限 4），节省时间、覆盖更多关键词。

### v1.3 — 大纲子智能体：自包含（搜索 + 审查 + 提纲一体）

- **新增 `agents/outliner.py`**：把"搜索素材 → 审查 → 生成提纲"收进一个独立的 LangGraph 子图，作为主图 `outline` 节点挂载（主图简化为 `outline → write → edit`，`research` 节点移除，`agents/researcher.py` 删除）。
- **补搜闭环**：子智能体内部按失败类型分类路由——素材不足（搜索失败/审查太差）会**自动补搜**（≤2 轮），提纲不合格会换提示重试（≤2 次），都到上限则用自身知识兜底，保证返回的提纲一定可用。解决"素材单向流入、烂素材只能硬写"的问题。
- **子图私有状态**：补搜/重试计数（`search_round` / `outline_attempt`）是子图私有键，不泄漏回主图 state（对 langgraph 1.2.11 实测验证）。

### v1.1 — 调研质量优化（搜索 → 审查 → 提纲）

- **检索内容 LLM 审查**（新增 `REVIEWER_PROMPT`）：搜索结果先由 LLM 逐条评估来源可信度、信息含量、相关度，剔除营销味（"一篇就够了""全面爆发"）、宽泛无信息、拼接脏数据，再基于可靠素材生成提纲。解决"搜索内容质量不高、过于宽泛"的问题。
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
- [x] **logging 替代 print**（已实现 v1.8）：新增 `logging_config.py` 统一配置，`--verbose` 控制级别，日志同时输出到 stderr 与文件（默认 `b_writer.log`）。

### P1 — 中期

- [ ] **多模型路由**：`write_section` 用便宜模型（如 `deepseek-v4-flash`），`editor_node` 用更强模型（如 `deepseek-reasoner`），实现"任务难度分级路由"的成本优化。
- [ ] **流式输出（stream mode）**：`main.py` 用 `graph.stream()` 替代 `graph.invoke()`，按节点事件逐段输出（"正在搜索…"→"正在写第一章…"），学习 LangGraph streaming API。

### P2 — 远期

- [x] **Human-in-the-loop 中断点**（已实现 v1.9，大纲后介入）：`--human-review` 开关在**大纲后**暂停人工确认/修改（同步 `input()`，未用 `interrupt()`）。可选扩展：在 `merge` 后加第二介入点预览全文草稿，选"继续审校 / 直接输出 / 打回某章重写"，届时再学习 checkpoint + interrupt 机制。
- [ ] **多视角审校（Judge Panel）**：3 个并行审校角色——语言编辑（语病/流畅度）、事实核查（数据/引用准确性）、结构编辑（逻辑/衔接），独立打分取多数意见。
- [ ] **搜索素材缓存 + 知识复用**：搜索结果按 query hash 缓存到本地，过期 24h，跨文章复用，减少重复搜索。

## License

MIT
