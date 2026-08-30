# CLAUDE.md — blog_writer 开发说明

给 Claude Code（以及后续开发者）看的项目说明。面向用户的文档在 [README.md](README.md)，本文件侧重**代码结构、关键决策、踩过的坑**。

## 项目概览

`blog_writer` 是一个基于 **LangGraph** 的中文**科普文章**生成多 Agent 项目：给定一个中文题目，自动完成 调研 → 写作 → 审校 的创作。

- LLM：**DeepSeek V4 Flash**（`deepseek-v4-flash`），通过 **OpenAI 兼容端点**调用（`base_url=https://api.deepseek.com`）。**不是 Anthropic API**，别按 Claude SDK 写代码。
- 多模型路由（`model_router.py`）：所有 LLM 调用按**角色**（research/outline/split/write/revise_outline/edit_lang/edit_logic/edit_fact）路由到候选模型链，调用失败自动 fallback 切下一个；`--model` 可覆盖全局默认。目前只注册 DeepSeek，加第二个模型只需在注册表加一个 `ModelSpec`（环境里已有 `DASHSCOPE_API_KEY`/Qwen 可用）。
- 搜索：**DuckDuckGo**（`ddgs` 库，免费无需 Key），`region=cn-zh` 提升中文结果质量。**两级搜索缓存**（`search_cache.py`）：query 级缓存 `web_search` 原始结果 + topic 级缓存审查后素材（SQLite `.cache/` 跨进程复用，TTL 7 天，`--clear-search-cache` 清空）。
- 主图节点：`outline`（子图）→ `writing`（写作子 Agent 子图，内部 `split` → `write_section`（子图，Send 并行 ×N）→ `merge`）→ `edit`（审核子智能体，内部 3 角色并行打分 + 多数表决；条件边打回只重写问题章节，最多 2 次）。
- 搜索：outliner 子图内 **ThreadPool 并行**（`MAX_PARALLEL_SEARCHES=4`）；写作按章节 **Send 图级并行**。
- **Web 页面版**（`web_server.py` + `web/index.html`，可选）：FastAPI 本地服务，浏览器完成 选项控制（题目/模型/人工确认开关）/ 大纲人工确认 / 成品展示；复用 CLI 同一套 `interrupt()`/`Command(resume)` 协议，**图逻辑零改动**。

## 常用命令

```bash
# 运行（题目必填）
python main.py "为什么越来越多的人选择远程办公"
python main.py "为什么越来越多的人选择远程办公" --output out.md

# 调试：输出 DEBUG 级日志，并指定日志文件（默认项目根目录 blog_writer.log）
python main.py "题目" --verbose --log-file /tmp/bw.log

# 用虚拟环境
.venv/bin/python main.py "题目"

# 人工介入：大纲生成后 interrupt() 暂停，确认/修改后再继续（默认全自动）
python main.py "题目" --human-review
# 断点续跑：中途退出/Ctrl+C 后，从上次暂停点接着写（SqliteSaver 持久化到 .checkpoints/）
python main.py --resume <thread_id>
# 用 MemorySaver 代替 SqliteSaver（进程内、退出即失；仅供对比学习跨进程持久化）
python main.py "题目" --human-review --in-memory

# 覆盖全局默认模型（多模型路由入口；--model 后跟 MODEL_REGISTRY 里的名字，如 deepseek-v4-flash）
python main.py "题目" --model deepseek-v4-flash

# 清空搜索素材缓存（.cache/，除 TTL 7 天自动过期外的强制清空；清空后退出）
python main.py --clear-search-cache

# Web 页面版：浏览器里填题目/选模型/人工确认，页面展示大纲确认与成品（必须单 worker）
.venv/bin/python -m uvicorn web_server:app --port 8000
# 打开 http://localhost:8000

# 确定性控制流测试（mock chat/cached_search，不耗 token）
.venv/bin/python tests/test_outliner_subagent.py
```

## 架构与数据流

```
主图（graph.py）：START → outline(子图) → [human_review?] → writing(写作子Agent子图) → edit
                                                                    ↑                  │
                                                    rewrite（只重写问题章节）←（should_continue）┘
  · human_review（可选，--human-review 开关经条件边启用）：大纲后 interrupt() 暂停，把大纲交给 main.py 交互循环；
    回车确认 / 意见重写 / #粘贴后 Command(resume=...) 续跑；有意见（outline_review_feedback）经 route_review 条件边回环
    重写再二次确认；默认关闭完全不执行
  · edit（审核子智能体，agents/review.py）：3 个审校角色（语言/逻辑/事实）并行独立打分，多数表决（显式通过票 ≥2/3）；passed 为假且 revision_count < MAX_REVISIONS(2) → 打回 writing（只重写 failed_sections）
```

writing 子图（agents/writing.py，自包含，挂 writing 节点）：
  START → split →(fan_out：Send×N 或 "merge")→ write_section(子图) → merge → END
  · split：LLM 把 outline 文本拆成 [{title, points, materials}]（revision_count>0 时复用，不重复调 LLM）
  · fan_out（split 的条件边）：返回 [Send("write_section", ...)×N]；打回时只 Send failed_sections 里的章节
  · write_section：并行写单章，返回 {"section_drafts": {id: text}}（reducer {**a,**b} 聚合，重写覆盖同 id）
  · merge：按 id 升序拼装各章节草稿 → draft
  · output_schema 只写回 {draft, sections, section_drafts}：sections/section_drafts 是跨重写循环的共享通道

edit 审核子智能体（agents/review.py，自包含，挂 edit 节点）：
  START →(fan_out_reviewers：Send×3)→ review_role(并行×3) → aggregate(多数表决) → END
  · review_role：按 role_name 选 prompt，call_llm(json_mode=True, role=edit_xxx)；pydantic 校验失败重试 REVIEW_MAX_RETRIES 次，耗尽弃权（passed=None，不投通过票）
  · role_reviews：私有 reducer 聚合键（role_name → {score, passed, failed_sections}），Send 并行实例只写它
  · aggregate：显式通过票 ≥2（len(REVIEW_ROLE_NAMES)//2+1）才算通过；quality_score=有效票 score 均值；failed_sections 按 id 合并（【角色名】前缀拼接，passed 时 []）；final_article=当前 draft（取消润色）；revision_count 只在此 +1
  · output_schema 只写回 {final_article, quality_score, passed, failed_sections, revision_count}：与旧 editor 输出键一致，主图 should_continue / writing 打回链路零改动

outline 子图（agents/outliner.py，自包含）：
  START → search → should_search_again ── 素材够 → generate → should_retry ── 合格 → END
        （搜索+审查）  ├─ 素材不足且未达上限 → search（补搜）         ├─ 不合格且未达上限 → generate（换提示重试）
                      └─ 素材不足且达上限 → fallback                 └─ 不合格且达上限 → fallback
  每条路径最终都保证返回可用的 outline（fallback 兜底）。search 内多查询用 ThreadPoolExecutor 并行。
```

## 核心模块职责

| 文件 | 职责 |
|---|---|
| `main.py` | CLI 入口：解析参数/装配 checkpointer（SqliteSaver 或 --in-memory 的 MemorySaver）/统一交互循环（处理 `__interrupt__` 与 `Command(resume=...)`）/`--resume` 断点续跑/打印成品 |
| `graph.py` | 主图编排：`outline → writing → edit`，`should_continue` 决定打回 `writing`（只重写问题章节） |
| `state.py` | `ArticleState`（TypedDict）：主图共享状态，**不含 materials**（素材只在子图内部流动）；`section_drafts` 用 `Annotated[dict, reducer]` 聚合并行章节草稿 |
| `llm.py` | LLM 调用统一封装（消息形状 + `@traceable`）：`call_llm()`（一次性问答，**默认 JSON 模式**，仅 Markdown 输出显式关）/ `chat()`（返回完整响应以便读 `tool_calls`）；新增 keyword-only 的 `model`/`role` 参数，委托 model_router 选模型与兜底 |
| `model_router.py` | **多模型路由**：`ModelSpec` 注册表（MODEL_REGISTRY）+ `ROLE_MODEL_MAP`（role→候选模型链，`__default__` 哨兵跟随全局默认）+ `resolve_chain()`（显式 model > role 链 > 全局默认，能力过滤）+ `call_with_fallback()`（失败按原因分类退避重试、耗尽切下一个模型）+ `get_client()`（按 provider 懒加载缓存）；`ModelRoutingError` |
| `output_validation.py` | **LLM JSON 输出强约束**：`call_json_model()`（json_mode LLM + pydantic 强校验，校验失败把**结构化字段错误**（字段路径+原因）反馈给模型重试、耗尽返回 None）+ 输出 pydantic model（`ReviewOutput`/`SplitOutput`，`model_json_schema()` 导出文本嵌入 prompts，客户端与 prompt 用同一 schema 不漂移） |
| `prompts.py` | 各 Agent 中文 system prompt：RESEARCHER / MATERIAL_REVIEW / OUTLINER / FALLBACK_OUTLINE / REVISE_OUTLINE / SPLIT / WRITE_SECTION / REVIEW_LANG / REVIEW_LOGIC / REVIEW_FACT（含共享 `_REVIEW_JSON_SCHEMA`，已嵌入 `model_json_schema()` 导出的真实 JSON Schema） |
| `agents/tools.py` | `web_search()`（成功返回 JSON 数组 [{title,link,body}]，失败/无结果返回 {"error"} 但保留关键词；失败按原因分类、退避重试同一 query）+ `WEB_SEARCH_TOOL`（OpenAI 兼容 function schema） |
| `search_cache.py` | **两级搜索缓存**：`search_cache` 表（query→web_search 原始结果）+ `topic_materials` 表（topic→审查后素材），SQLite 跨进程复用；`cached_search`（query 级，单飞防并发重复搜索）/ `get_cached_materials`+`store_materials`（topic 级）/ `clear()`（供 `--clear-search-cache`）；TTL 惰性失效 |
| `agents/outliner.py` | **大纲子智能体**：自包含子图（搜索→审查→生成→自检→补搜/重试/兜底）；搜索多查询 ThreadPool 并行 |
| `agents/writing.py` | **写作子 Agent**（自包含子图，挂 `writing` 节点）：内部 `split_sections`（拆章，经 `SplitOutput` 强校验、失败反馈重试，打回复用）/ `fan_out_write`（条件边，返回 `[Send]` 或 "merge"）/ `merge_sections`（按 id 升序拼装）；`output_schema` 只写回 `draft`/`sections`/`section_drafts`；**不含写作 LLM** |
| `agents/section_writer.py` | **章节写作子智能体**（自包含子图，挂 `write_section` 节点）：`write`（初稿/重写）→ `self_check`（启发式自检：篇幅/标题/要点）→ `should_rewrite`（条件重写，`MAX_SECTION_ATTEMPTS` 上限）→ `emit`（输出 `section_drafts`）；`output_schema` 限定子图只输出该键 |
| `agents/review.py` | **审核子智能体**（自包含子图，挂 `edit` 节点）：`START →(fan_out_reviewers: Send×3)→ review_role(并行×3) → aggregate`；3 个审校角色（语言/逻辑/事实）各自独立 `call_llm(json_mode=True, role=edit_xxx)` 输出 JSON（score/passed/**failed_sections**[{id,feedback}]），经 `ReviewOutput`（pydantic，本模块别名 `ReviewJudgeOutput` 防与子图输出 TypedDict 重名）强校验；多数表决（≥2/3）+ 校验失败重试 `REVIEW_MAX_RETRIES` 次、耗尽**弃权**、全弃权保守通过；`output_schema` 只写回 {final_article, quality_score, passed, failed_sections, revision_count}；**取消润色**（final_article=当前 draft） |
| `agents/human_review.py` | **人工介入节点**：`interrupt()` 暂停把大纲交给客户端，按 `Command(resume=...)` 的 action（confirm/revise/replace）决定下一步；revise 时把 `outline_review_feedback` 写进 state、经 `route_review` 条件边回环，节点开头按意见重写（REVISE_OUTLINE_PROMPT）再二次确认；`build_graph(enable_human_review=True)` 经条件边启用 |
| `langsmith_config.py` | LangSmith 配置：读 `.env`、校验环境变量、设置项目名 |
| `web_server.py` | **Web 页面版后端**（可选）：FastAPI + 单任务运行器（后台 worker 线程跑图，`__interrupt__` 用 `threading.Condition` 挂起、resume API 唤醒续跑）+ 1 个页面路由 `GET /` 与 5 个 API 路由（`/api/models` `/api/run` `/api/status` `/api/resume` `/api/cancel`）；`web/index.html` 是单页前端（表单 / 大纲确认 / 结果展示，`setInterval` 轮询 status） |
| `logging_config.py` | 统一日志：所有模块用 `logging` 替代 print；stderr 简洁输出（保留 emoji 观感）+ 文件（默认 `blog_writer.log`）详细追踪；`--verbose` 控制终端级别；`setup_logging()` 幂等 |

## 关键设计决策与踩过的坑（务必阅读）

1. **孤儿 tool 消息问题（最坑）**：OpenAI 兼容 API 要求 tool 消息必须跟在含 `tool_calls` 的 assistant 消息后面。程序**主动**发起的补搜如果往上一轮对话里追加 tool 消息（没有对应的 assistant tool_calls）会直接 400。**解法：`search` 节点每次进入都重新构建干净对话**（`messages = [user: 题目]`），搜索轮次之间绝不累积消息。改这块时务必保持这个不变量。
    - **补搜轮带历史查询上下文（不破坏本不变量）**：`search` 节点新增子图私有键 `search_history: list[str]`，记录每轮实际执行的查询关键词（**去重保序**，`list(dict.fromkeys(history + new_queries))`）。补搜轮（`round_n>1`）重进 search 时，把 history 以**纯文本**拼进 user 消息（`【上一轮搜索记录】…请避免重复搜索、针对缺失角度补充新查询`），让模型避免重复盲搜、针对缺失角度定向补搜。关键：**只拼进 user 消息、不追加 tool 消息**——"重建干净对话、绝不累积消息"的不变量原样成立，只是 user 文本更厚。每轮执行完搜索把新查询并入 history 并写回；**命中 topic 缓存的分支不带 history**（返回时不写该键、保留原值——命中即素材够用、根本不会进补搜，写不写无影响）。

2. **子图私有键不泄漏（langgraph 1.2.11 实测）**：子图作为父图节点时，`OutlineState` 里的私有键（`search_round` / `outline_attempt` / `materials`）**不会写回父图 `ArticleState`**，且私有计数能跨子图内部自循环正确递增。所以补搜/重试计数放子图私有，不用加到 `ArticleState`。若升级 langgraph，需回归验证这一点。

3. **失败分类路由**：素材问题（搜索失败/审查太差）和提纲问题（太短）**分开处理**——素材不足→补搜，提纲不合格→重试，不要混为一谈。阈值：
   - `MAX_ATTEMPTS = 2`（提纲生成 1 次 + 重试 1 次）
   - `MAX_SEARCH_ROUNDS = 2`（先搜 1 轮 + 补搜 1 轮）
   - `MIN_OUTLINE_LEN = 60`（提纲低于此长度视为不可用）
   - `MIN_MATERIALS_LEN = 40`（素材低于此长度视为不足）
   - `FAILURE_MARKERS`：素材/审查文本中含这些词视为"没搜到"

4. **审查失败降级**：`search` 节点里，LLM 审查结果不可用（`_materials_ok` 为假）时**退回原始搜索结果**作为素材——这样即使审查坏了，后续 `should_search_again` 还能基于真实内容判断是否补搜。

5. **JSON 输出强约束 + 校验失败反馈重试（output_validation.py）**：DeepSeek 官方 API **不支持** `response_format={"type":"json_schema"}`（会报 unavailable），只能客户端强约束。做法：定义 pydantic 输出 model（`ReviewOutput`/`SplitOutput`），`model_json_schema()` 导出真实 JSON Schema 文本嵌入 prompt（`_REVIEW_JSON_SCHEMA`/`SPLIT_PROMPT`），客户端用 `call_json_model()` 以同一 model 强校验——校验失败把**结构化字段错误**（字段路径 + 原因，如 "score: 应为整数"）反馈给模型重试（`REVIEW_MAX_RETRIES=2`），**替代旧版泛泛的"不是合法 json"与静默丢弃**。要点：
   - **`call_llm` 默认 `json_mode=True`**：审校/拆章经 `call_json_model` 无需显式传参（内部不再传 `json_mode=True`）；仅输出 Markdown 的调用（写正文 `WRITE_SECTION`/`SELF_REVIEW`、修订大纲 `REVISE_OUTLINE`）显式传 `json_mode=False`。`chat()`（outliner 工具调用 + Markdown 输出）不走 json。
   - **小写 "json" 字样**：DeepSeek 的 `response_format={"type":"json_object"}` 要求 prompt 里出现 "json" 才生效（各 prompt 头文字用小写 json 满足，schema dump 本身不含该词）。
   - **pydantic 细节**：`model_validate_json` 对非法 JSON 抛 `ValidationError`（type=json_invalid，**无需另捕 json.JSONDecodeError**）；`score:"abc"` 报 `int_parsing`（pydantic 会把 `"85"` 宽松解析成 85）；非 dict 数组报 `model_type`；缺字段报 `missing`。
   - **调用点必须传自己的模块级 `call_llm`**（`llm_call=call_llm`）：默认参数在模块定义时绑定，显式传才让测试里 `R.call_llm = fake` / `W.call_llm = fake` 的模块级替换继续生效。
   - **耗尽兜底保留**：审校角色**弃权**（`passed=None`，不投通过票、不计入分数均值）——弃权帮不了任何一方凑到多数，**单个健康角色无法单独通过，坏角色不拉偏多数表决**；3 角色全弃权才**保守按通过处理**（`passed=True, score=0`），避免把流程卡进死循环。拆章回退单章节。加了具体错误反馈后兜底极少触发。
   - **重名坑**：`agents/review.py` 本身有 `class ReviewOutput(TypedDict)`（子图输出 schema），模块加载时会**覆盖** import 进来的同名 pydantic model（`model_validate_json` 不存在报 AttributeError），故该模块用别名 `ReviewJudgeOutput` 区分。
   - `revision_count` 只在 `aggregate` 执行时 +1，不因角色重试/并行多计。

6. **ReAct 工具调用**：`chat()` 返回完整响应，`agents/outliner.py` 里手动执行 `tool_calls`（逐条 `web_search`），再把 assistant 消息（含 tool_calls）+ tool 结果放回对话交给审查阶段。

7. **DeepSeek 是 OpenAI 兼容格式**：system 提示是 messages 里的一条消息，不是顶层参数。`call_llm` 和 `chat` 都遵守这个约定。

8. **Send 并行写作（按章节）**（封装在 `agents/writing.py` 写作子 Agent 子图内部，语义不变）：`split` 的条件边 `fan_out_write` 返回 `[Send("write_section", {section_id, section, ...})×N]`，下一 superstep 并行执行同一节点多次；`write_section → merge` 由内置屏障同步，**所有分支写完才触发 merge**。`section_drafts` 用 `Annotated[dict, _merge_dicts]` 聚合——重写章节时同 id 覆盖旧草稿（reducer 语义已验证）。打回时 `fan_out_write` 只看 `failed_sections`（`[{id, feedback}]`，每章带专属修改意见）只 Send 问题章节并把该章意见传给 `write_section`，`split` 在 `revision_count>0` 时直接复用 sections **不再调 LLM**。**审核子智能体同样用 Send 并行**（`agents/review.py` 的 `fan_out_reviewers` 挂在 `START` 上，直接返回 `[Send("review_role", {role_name, draft})×3]`——fan-out 前无工作可做，不必像写作链那样挂真实节点），`review_role → aggregate` 由屏障同步，`role_reviews` 用 reducer 聚合。

9. **`get_graph()` 静态渲染不展开 Send 条件边**：编译后的图 `get_graph().edges` 只显示静态直连边，Send fan-out 不会出现在边列表里（断言节点链/边时别依赖它）。重构后写作链在 `writing` 子图内部，主图静态渲染连 split/write_section/merge 都看不到，主图节点集断言改为 `{outline, writing, edit}`。运行时链路正确性用 `invoke` + mock LLM 验证（见 test_section_writer.py）。

10. **人工介入（可选，`--human-review`）——LangGraph 正统 HITL**：`outline` 后条件边路由到 `human_review` 节点，用 **`interrupt()` + `Command(resume=...)`** 协议替代早期版本的节点内同步 `input()`（早期决策笔记见 git 历史）。要点：
    - **必须配 checkpointer**：`build_graph(enable_human_review, checkpointer)` 把 saver 传进 `graph.compile(checkpointer=...)`，interrupt 才有意义。无 checkpointer 时 interrupt 仍返回 `__interrupt__`（可暂停）但不持久化、不可 resume（实测 1.2.11）。
    - **resume 后节点从头重执行**：所以"按意见重写"逻辑放在 `human_review_node` 开头（读 `state["outline_review_feedback"]` 非空就调 `_revise_outline`），重写后再次 interrupt 展示确认；确认后清空 `outline_review_feedback`。
    - **多轮修改**：`outline_review_feedback` 非空时 `route_review` 条件边回环 `human_review`，形成 interrupt → resume(revise) → 回环 → interrupt → resume(confirm) 的循环，完全由 LangGraph 控制（不再阻塞节点）。
    - **`__interrupt__` 返回格式**：invoke 返回 `{"__interrupt__": [Interrupt(value=..., id=...), ...]}`，payload 取 `result["__interrupt__"][0].value`；resume 用 `Command(resume={...})`。
    - **断点续跑（`--resume <thread_id>`）**：SqliteSaver 持久化到 `.checkpoints/`（需 `langgraph-checkpoint-sqlite` 包）。**坑**：`SqliteSaver.from_conn_string(DB)` 返回的是 context manager，必须 `with ... as cp:` 解包后再传给 `compile(checkpointer=cp)`，直接传会报 `TypeError: Invalid checkpointer... Received _GeneratorContextManager`（实测踩坑；MemorySaver 直接 new 即可）。进程退出/Ctrl+C 后，`python main.py --resume <thread_id>` 打开同一 checkpoint（`graph.get_state(config)` 看快照），停在中断处时 `invoke(None)` 会重新触发同一 interrupt 进入交互。**resume 时须与上次带相同的 `--human-review`**，保证图结构一致。
    - **`--in-memory`**：用 MemorySaver（进程内、退出即失）对比 SqliteSaver 的跨进程持久化，纯学习用。
    - 开关经 `build_graph(enable_human_review)` + `functools.partial` 绑定 `route_outline`（条件边函数）：`route_outline` 若直接读模块级名字会 `NameError`，用全局变量则多图并存互相污染，只能 `partial` 注入。关闭时节点完全不执行、图与全自动版本一致。
    - 交互提示由 `main.py` 统一走 stderr（`print(..., file=sys.stderr)`），保持 stdout 只给成品文章（`> out.md` 重定向不混入）。
    - 打回重写循环（split ← edit）不经过该节点；`REVISE_OUTLINE_PROMPT` 不含 "json" 字样（走非 json_mode 的 `call_llm`）。

11. **write_section 升级为自包含子图（"写单章 → 自检 → 条件重写"）**（agents/section_writer.py，挂 `write_section` 节点）：初稿后先用**启发式自检**判断是否合格——篇幅 ≥ `MIN_SECTION_LEN(120)`、以 `## ` 开头，不调 LLM、确定性可测。合格直接结束（**省掉旧版"无条件自我反思一轮"的那次调用**）；不合格且 `write_attempt < MAX_SECTION_ATTEMPTS(2)` 才重写（自检意见塞回生成提示）；达上限接受当前结果（warning 兜底）。
    - **不做"要点覆盖"子串检查（真实 e2e 两次 100% 误判后移除）**：split 生成的 `points` 是**描述性写作指令**（如"用生活化场景对比早高峰通勤与在家办公的差异"），正文几乎不可能逐字复述——任何固定长度阈值都会误判，两次真实 e2e 中 7 章全部被误判"未覆盖要点"、白白各多付一次重写调用，还把错误意见塞回模型。**要点覆盖是"内容层"检查，与 LLM 自由表达天生不兼容，交给外部审核子智能体审校**；子图自检只保留篇幅/标题两个确定性检查（宁可漏检，不可误检）。

12. **Send 并行触发编译子图：`output_schema` 限定输出是硬要求**（langgraph 1.2.11 实测踩坑）：fan_out 的 Send payload `{section, topic, feedback}` 直接是子图输入 state；多个子图实例同一 superstep 并行结束时，若子图把输入键（如 topic）**原样写回父图**，父图 topic 是普通键（LastValue），同一 superstep 收到多个值会抛 `INVALID_CONCURRENT_GRAPH_UPDATE`。**解法：`StateGraph(SectionWriterState, output_schema=SectionWriterOutput)` 把子图 output_channels 限定为 `section_drafts`**，冲突从根上消除（父图零改动）。若想给子图加新输出，改 `SectionWriterOutput` 即可。
    - **重构后双层 output_schema**：写作链收进 `writing` 子图后形成两层限定——`section_writer` 限 `section_drafts`（写作子图内并行实例只写 reducer 键）、`writing` 限 `{draft, sections, section_drafts}`（对主图是单实例顺序节点，一次性 apply_writes）。Send 并行只发生在写作子图内部，主图永不出现并发写。
    - **审核子智能体同样靠 output_schema 兜底**（`agents/review.py`）：`ReviewState(output_schema=ReviewOutput)`，`ReviewOutput` 只暴露 {final_article, quality_score, passed, failed_sections, revision_count}；3 个角色实例并行时只写私有 reducer 键 `role_reviews`，输入键 `draft` 与私有键不写回父图，冲突根除。`draft` 仅作输入读取（不在 ReviewOutput），输出键由 `aggregate` 单点写出（屏障后），父图不会出现并发写。

13. **子图内部键名避免与父图通道重名**：`ArticleState` 与 `writing` 子图 `WritingState` 都有 `draft` 通道，章节级草稿键若命名 `draft`，会把单章文本写回并覆盖整篇 draft、破坏 merge 结果。**硬约束：章节写作子图一律用 `section_text`**（`SectionWriterState` 顶部注释列禁用名）。子图私有键（`section_text`/`write_attempt`/`self_check_notes`）与父图键零重叠，由 output_schema 保证不写回父图。**审核子智能体同样约束**：`ReviewState` 的私有键 `role_name`/`role_reviews` 与父图零重叠；父图的 `draft`/`passed`/`quality_score`/`failed_sections`/`final_article` 只能当共享输入或 `aggregate` 单点输出，绝不能当并行角色实例的私有写键。

14. **多模型路由（model_router.py）——角色路由 + fallback 链 + `--model` 覆盖**：10 个 LLM 调用点各带 `role=`（审校是 3 个角色 edit_lang/edit_logic/edit_fact 各 1 次），路由表 `ROLE_MODEL_MAP` 把 role 映射到候选模型链，单次调用失败自动切下一个。要点：
    - **`__default__` 哨兵是 `--model` 生效的关键**：role 链默认都指向哨兵（=跟随全局默认模型），于是 `--model X` 一处切换让所有角色切到 X；将来某环节想用更强模型只改那一个 role 的链（如 `{"edit_fact": ["deepseek-reasoner", DEFAULT_MODEL]}`，让"事实核查"角色用更强模型）。guard：哨兵字符串不能作为真实模型名。
    - **resolve 优先级**：显式 `model=` > role 链 > 全局默认；`json_mode`/`tools` 会带 `required_capabilities`，role 链里能力不符的模型**静默跳过**、显式 model 能力不符**直接报错**。
    - **fallback 是"API/传输异常"层**（默认 `openai.OpenAIError`，含超时/限流/连接错误），与客户端 pydantic 校验重试（output_validation.py，"返回内容非法"层）**两层正交**——前者按失败原因调整后重试/切模型、后者同模型重试，互不影响。**按失败原因调整**（`_classify_llm_error` + `_retry_delay`）：`rate_limit`（限流，优先服务端 `retry-after` 头、本地退避翻倍）与 `transient`（超时/连接/5xx，指数退避）**不切模型、同模型退避重试** `PER_SPEC_MAX_RETRIES=2` 次——切模型对限流无效（限流是账号/端点级，等待往往就好）；`context_exceeded`（`BadRequestError`，`body.error.code` 含 context_length_exceeded 或 message 含 maximum context length）**按失败信息编辑参数**：把 `adjust["max_tokens"]` 减半后立即重试同一模型（不退避），缩到 `MIN_MAX_TOKENS=512` 仍超才切下一个——`adjust` 是 `llm.py` 传入的可变 dict，闭包读它取当前 max_tokens；`fatal`（认证/权限/其他 400/未知）**重试无意义、直接切下一个模型**。退避 `RETRY_BASE_DELAY=1s` 2 倍递增封顶 `RETRY_MAX_DELAY=4s`。注意 openai 3.x：`APITimeoutError` 是 `APIConnectionError` 的子类（检查父类即可覆盖超时）、`retry-after` 在 `exc.response.headers`（不在 exc 顶层）、`BadRequestError` 的错误码在 `exc.body["error"]["code"]`（不在 exc 顶层，`exc.message` 只是 HTTP 状态文本）。
    - **client 懒加载 + 缓存**：按 `(base_url, api_key_env)` 缓存 OpenAI client；模块导入不读 env，缺 key 在首次真实调用才报友好错误（原 llm.py import 时直接 KeyError）。
    - **教学取舍**：`retryable_exceptions` 默认只捕 OpenAIError、不吞代码 bug；`timeout=300s`（原 openai 默认 600s，超长调用有被切断风险，值可在 ModelSpec 按模型调）。
    - 新参数 `model`/`role` 是 keyword-only，测试 fake 的 `**kw` 可吸收；无参调用行为与旧版完全一致（走全局默认）。

15. **搜索素材缓存 + 知识复用（search_cache.py）——两级缓存 + SQLite 跨进程**：outliner 每次运行同一题目都重复真实联网搜索 + LLM 审查，缓存让其跨进程复用。要点：
    - **两级缓存**：底层 `search_cache` 表（query → `web_search` 原始结果，相似关键词跨题目共享命中）；顶层 `topic_materials` 表（topic → 审查后素材，同一题目完全跳过 搜索+审查 子流程）。**大纲不缓存**——它是 LLM 生成内容，缓存会让每次运行结果一模一样，失去练手价值；只缓存"事实数据"。
    - **接入点**：outliner 的 `_run_search` 从 `web_search` 换成 `cached_search`（query 级）；`search` 节点开头查 `get_cached_materials(topic)` 命中直接返回，末尾 `store_materials`（**仅 `_materials_ok` 为真才写**，不足素材不缓存，避免下次命中坏缓存再触发补搜）。
    - **命中 topic 缓存时 `search_round` 照常 +1（不重置为 1）**：若重置，素材不足 → 补搜 → 又命中同一缓存 → 永远到不了补搜上限，死循环（实现时踩坑修正）。
    - **单飞（single-flight）防并发重复搜索**：`cached_search` 用 `_cond`（Condition）让同 key 并发的后到线程等待，第一个线程真正 `web_search` 后写库，等待线程复用结果。**锁只在 SQLite 读写处，`web_search` 始终在锁外**（保持 outliner 多查询并行不串行化）。
    - **SQLite 线程安全**：单连接 `check_same_thread=False` + `threading.Lock`/`Condition` 保护所有读写；`PRAGMA journal_mode=WAL`；惰性 TTL 失效（读时发现过期即删即 miss，无后台清理任务）。`DB_PATH` 可覆盖（测试用临时库隔离）。
    - **测试 mock 点变化（关键）**：outliner 不再有 `web_search` 模块属性，`_run_search` 走 `cached_search`。所有测试改 mock `O.cached_search`，并**必须同时 mock `O.get_cached_materials`（返回 None）与 `O.store_materials`（no-op）**——否则真实连 `.cache/` 库，命中真实缓存会改变 chat 调用次数（test_model_router 实测踩坑）。

16. **写作子 Agent 内嵌 Send 并行 + 共享通道跨父子图双向流动**：把主图 `split → write_section×N → merge` 收进 `agents/writing.py` 自包含子图（挂 `writing` 节点），审校 edit 留主图。要点：
    - **三层嵌套**：主图 → writing 子图 → section_writer 子图。核心机制（Send 并行触发编译子图 + output_schema + reducer 聚合）与原来在主图层级完全一致，只是下推一层，1.2.11 下实测成立（test_section_writer 16 项全过）。
    - **共享通道跨重写循环**：`sections`/`section_drafts` 同时声明在 `ArticleState` 与 `WritingState`，既是输入也是输出。重写时主图再次进入 writing，父图注入这些键 → split 复用、fan_out 只 Send 失败章节、merge 重组，并把更新后的 sections/section_drafts 经 `WritingOutput` 写回父图。**必须写回**，否则下一轮循环丢失章节/旧草稿。
    - **merge 显式按 id 排序**：`sorted(sections, key=id)`——不再隐式依赖 split 的 enumerate 列表序，更稳健（解决了「merge 没按 id 组合」的疑虑，行为不变）。

17. **Web 页面版驱动层（web_server.py）——图逻辑零改动**：CLI 的 `_interactive_invoke`（main.py）是唯一把图绑在 stdin 的地方。Web 版用一个后台 worker 线程跑 `graph.invoke`，遇 `__interrupt__` 把大纲存进 TaskState 并用 `threading.Condition` 挂起；前端轮询 `/api/status` 拿大纲、`POST /api/resume` 提交 `{"action": confirm|revise|replace, ...}` 唤醒续跑。要点：
    - **必须专用后台线程**：`graph.invoke` 同步分钟级、且每次在当前线程内部建/拆事件循环；放 asyncio 端点会冻结所有请求（ainvoke 会让外部循环与图内部循环纠缠），HTTP 线程永不进入图执行。
    - **resume 双条件 409**：`waiting` 且 `resume_payload` 未消费才接受；worker 在锁内原子「消费 payload + 置 running」，`wait_for(predicate)` 防 missed-wakeup（resume 在 worker 进 wait 前已 notify 也会先查 predicate 直接放行）。
    - **checkpointer 用 MemorySaver、每任务一个**：web 单任务单进程、无跨进程续跑诉求，避开 SqliteSaver 的线程安全与 `.checkpoints/` 文件锁（避免 web 与 CLI 并发写同一 sqlite 报 database is locked）；只在 worker 线程内触碰。
    - **uvicorn 必须单 worker**：单槽注册表是进程级全局，`--workers N` 会把轮询打散到不同进程副本。
    - **`set_default_model` 只在 worker 线程开头调用一次**（单写者），模型校验放 `/api/run` 端点（400）、生效放 worker；`replace` 用显式 action、textarea 是完整大纲原文，不沿用 CLI 的 `#` 前缀约定。
    - **`revise` 会二次 interrupt**（`route_review` 自环），worker 的 while 循环 + outline 覆盖是硬要求；前端按「大纲内容变了就重新启用操作按钮」刷新确认框。
    - **cancel 仅对 waiting 有效**：running 阶段 `graph.invoke` 原子执行、无法中途打断（打断会静默无效），所以 `/api/cancel` 只在 `waiting` 时接受（running → 409）、前端只在 waiting 显示取消按钮；waiting 取消后 worker 醒来置 `error="canceled"`。run 端点锁外 `build_graph` 失败（如缺 API key）会 try/except 清槽回 idle 再抛 500，绝不残留卡死的 running 槽位。

18. **搜索工具失败重试（tools.py）——失败原因分类驱动重试同一 query**：原 `web_search` 单次调用，失败直接返回失败文本，DDG 限流/网络抖动会把整轮搜索降级成"自身知识兜底"；且失败文本会被 `cached_search` 写进 query 级缓存污染 7 天。**解法**：重试放在 `web_search` 内部（`SEARCH_MAX_RETRIES=2`），每次失败先 `_failure_kind(exc)` 分类失败原因再决定退避/是否重试：
  - **transient**（超时/连接/未知异常）：指数退避重试同一 query（`RETRY_BASE_DELAY=1s` 2 倍递增，封顶 `RETRY_MAX_DELAY=4s`）；
  - **ratelimit**（`RatelimitException` 或 message 含 429/rate limit）：退避翻倍——注意 ddgs 的 `_search_sync` 常把限流吞进 `err` 再包成 `DDGSException` 抛，所以类型与 message 文本**都要**判断；
  - **no_results**（`DDGSException("No results found.")`）：**不是故障、不重试**，直接返回"没有返回结果"——重试无意义，换关键词才有效（交 outliner 补搜链路）。
  - **确定性 jitter**：`delay += (hash(query) % 10) / 10`，让不同 query 的并发重试错开（同 query 并发由 cached_search 单飞保证只搜一次，不依赖此 jitter）。
  - **返回 JSON 格式**：成功返回 JSON 数组 `[{title, link, body}]`（title 经 `_clean_title` 清洗、body 截断 500 字）；失败/无结果返回 `{"error": "…"}`，保留"搜索暂时不可用"/"没有返回结果"关键词——返回契约变了但 FAILURE_MARKERS 识别不受影响。`MATERIAL_REVIEW_PROMPT` 已注明输入是 JSON 数组。
  - 重试耗尽返回失败文本（带最后一次失败原因），让模型回退自身知识；返回契约不变，下游 `_materials_ok` 的 FAILURE_MARKERS 仍能识别。
  - **遗留问题**：重试耗尽返回的失败文本仍会被 `cached_search` 写入 query 级缓存（污染 7 天）——后续若处理，加"失败结果不写缓存"检测即可。
  - 测试：`tests/test_tools.py`（27 项，mock `agents.tools.DDGS` 的 context manager，`patch("agents.tools.time")` 隔离 sleep——注意不能 `patch("agents.tools.time.sleep")`，那会改到全局 `time` 模块）。

19. **工具调用参数强校验 + 校验失败反馈重试（agents/tools.py + agents/outliner.py）**：模型生成的 `web_search` 工具参数（query）此前用 `json.loads(...).get("query", "")` 解析——非法 JSON 直接抛 `JSONDecodeError` 让 search 节点崩溃、缺字段静默拿空串、类型错（如 `{"query": 123}`）原样传给 `ddgs.text` 行为不可预期。**解法**：`WebSearchArgs`（pydantic）是工具参数 schema 的**单一来源**，`WEB_SEARCH_TOOL.parameters` 由 `WebSearchArgs.model_json_schema()` 导出（minLength/maxLength 约束对模型可见），客户端用同一 model `model_validate_json` 强校验——两侧永不漂移，与 output_validation 的哲学一致。要点：
    - **校验失败反馈重试（同一 search 节点内的多轮规划）**：search 节点阶段一改为 `for arg_attempt in range(MAX_TOOL_ARGS_ATTEMPTS + 1)`（首次 + 最多修正 2 次）：逐条校验 tool_calls 参数，**合法调用立即并行执行**（结果作为 tool 消息）、**非法调用把具体字段错误（字段路径 + 期望类型 + `实际收到：{实际值}`，`format_tool_arg_errors`）作为 tool 消息反馈**给模型，再追加 user 提示"未通过 json 结构校验，按字段路径修正后重新生成"——不静默丢弃、不崩溃。
    - **孤儿 tool 消息不变量不受影响**：重试反馈的 tool 消息紧跟含 `tool_calls` 的 assistant 消息、按 `tool_call_id` 配对，OpenAI 协议合法（每个 assistant 声明的 tool_call 都有响应）；"search 节点每次进入重建干净对话"仅约束**跨**搜索轮次（决策 #1），同一节点内多轮规划的消息累积是 ReAct 正常形态。
    - **耗尽兜底**：`MAX_TOOL_ARGS_ATTEMPTS` 次仍非法 → warning 后 break，**跳过非法调用、只执行累积的合法子集**（`valid_tcs` 跨轮累积，new_queries 从它提取去重保序）；全部非法且无 content → materials 空 → 走补搜/兜底，绝不崩溃。
    - **`_run_search` 防御性强校验**：内部再 `model_validate_json`，非法返回 `{"error": "工具参数校验失败: ..."}` 文本（正常路径由 search 节点提前校验兜底，这里是双保险），不再 `json.loads().get()`。
    - 测试 mock 点：`tests/test_outliner_subagent.py` 的 fake_chat 新增配置 `tool_calls_seq`（逐批弹出 `[(id, args_dict|raw_str)]`，`make_tc_raw` 构造非法参数）驱动重试；默认不设该键则行为与旧版一致（现有 A~H 场景计数不变）。

## 测试方法

- **确定性控制流测试**：在模块层替换 `O.chat` / `O.cached_search`（fake），断言调用次数与返回键。这样不耗 token 就能覆盖所有分支（收敛/补搜/兜底/重试/私有键不泄漏）。注意 v2.3 起还需 mock `O.get_cached_materials`/`O.store_materials`（防连真库，见决策 #15）。
- 参考：`tests/test_outliner_subagent.py`（43 项检查）。核心断言模式：场景 A（素材够，1 轮收敛，chat=3）、B（补搜，chat=5，**断言补搜轮 user 消息带首轮查询记录、首轮不带**）、C（两轮失败兜底，chat=5）、D（提纲两次太短兜底）、E（重试收敛，chat=4）、F（私有键不泄漏，含 `search_history`）、G（主图节点链 {outline, writing, edit} + outline/writing 节点是 `CompiledStateGraph`）、H（并行搜索 3 查询）、I（工具参数强校验，`tool_calls_seq` 配置驱动重试：I1 非法类型→反馈"字段+期望+实际值"重试收敛、I2 非法 JSON→反馈"不是合法 json"、I3 耗尽跳过非法只执行合法子集、I4 `_run_search` 防御返回错误文本）。
- 按章节并行写作：`tests/test_section_writer.py`（22 项检查）——mock 主图全部 LLM（`agents.writing.call_llm`/`agents.section_writer.call_llm`/`agents.review.call_llm`/`agents.outliner.chat`），走完整链路验证：split 调 1 次 → write_section 子图 ×N（计数加锁，per-title 版本号断言、不依赖并行执行顺序）→ merge 按 id 顺序正确 → 审核子智能体多数不过 → 打回只重写问题章节（旧草稿被覆盖、其余保留、专属意见传递）→ 二轮三角色全过 → 子图私有键不泄漏。审校按"轮"计数（每轮 = 3 角色各 1 次，共 2 轮 6 次）。这条测试同时守护「共享通道跨子图边界写回」——若 sections/section_drafts 没写回父图，重写轮会丢 v1。并覆盖 split 输出校验失败：输出不合法 → 带具体字段错误反馈重试 → 耗尽量回退单章节（场景 A/B 直接调 `W.split_sections`）。
- 章节写作子智能体：`tests/test_section_subagent.py`（16 项检查）——mock `agents.section_writer.call_llm`，覆盖首写合格（不触发自检重写，对比旧版无条件反思省 1 次）/ 不合格→重写收敛 / 两次不合格接受 / 审校意见传递 / 要点不做子串检查（避免误判）/ 输出 schema 只暴露 section_drafts。
- 审核子智能体：`tests/test_review_agent.py`（替代 test_editor_retry.py）——mock `agents.review.call_llm`（fake 用 `kw["role"]` 区分角色，Send 并行顺序不保证、断言用集合），子图直调 `build_review_agent().invoke(...)`，覆盖 3 角色各调一次 / 多数通过（2 票）/ 多数不过（failed_sections 按 id 合并、【角色名】前缀、id 升序）/ 分数均值（含四舍五入）/ 单角色校验失败重试（第二次 user_content 含具体字段校验反馈）/ 弃权不拉偏（2 过+1 弃权通过、1 过+1 不过+1 弃权不过、全弃权保守通过）/ revision_count 只 +1 / output_schema 不泄漏私有键。
- JSON 输出强校验：`tests/test_output_validation.py`（25 项检查）——直接传 `llm_call=fake` 单测 `call_json_model`：合法返回 model / 非法 JSON→第二次成功且反馈含"没有通过 json 结构校验" / 缺字段反馈含字段名与"缺少该字段" / 类型错（`score:"abc"`）反馈含"应为整数" / 范围错（`score:500`）反馈含"高于允许上限" / 非 dict 数组反馈含"应为 json 对象" / `llm_call` 返回 None 走 json_invalid / 全失败返回 None 且调用次数==max_retries / `retry_prefix` 透传。
- 人工介入：`tests/test_human_review.py`（22 项检查）——mock `agents.human_review.input`（编程序列）与 `call_llm`，单测节点四种输入（回车/意见重写/粘贴大纲/q 退出）+ 整图开关开走通 + 开关关不触发节点 + 跨进程断点续跑。
- 多模型路由：`tests/test_model_router.py`（60 项检查）——mock `llm.get_client`（fake client）与 `llm.call_with_fallback`（spy），覆盖 role 路由解析 / `__default__` 哨兵→全局默认 / `set_default_model` 覆盖 / 能力过滤（role 链跳过 vs 显式 model 报错）/ fallback 成功与耗尽（保留 `__cause__`）/ 审校 3 个角色 role 正确传递（跑 `build_review_agent().invoke` 断言最近 3 次调用是 edit_lang/edit_logic/edit_fact）/ 旧无参行为不变 / client 懒加载缓存与缺 env 报错 / **失败分类退避重试**（限流同模型重试成功、`retry-after` 头优先、超时重试成功、瞬时耗尽切下一个模型、认证等致命错误不重试、`_classify_llm_error`/`_retry_delay` 单测） / **按失败信息编辑参数**（`context_length_exceeded` 缩小 `adjust["max_tokens"]` 立即重试同一模型、缩到 `MIN_MAX_TOKENS` 仍超才切下一个、其他 400 走 fatal 切模型；用 `httpx.Request/Response` 构造真实 openai 异常、`patch("model_router.time")` 隔离 sleep）。
- 搜索缓存：`tests/test_search_cache.py`（18 项检查）——mock `search_cache.web_search`（临时库隔离，覆盖 `DB_PATH`），覆盖 query 级 miss/命中 / key 规范化（首尾空白、大小写）/ TTL 过期重新搜索 / topic 级 store→hit→过期 miss / `clear()` 清空并返回条数 / **并发单飞（8 线程同 query，真实搜索次数 == 1）** / 空 key 不缓存。
- Web 页面版：`tests/test_web_server.py`（33 项检查）——mock 全部 LLM（`O.chat`/`O.cached_search`/`W.call_llm`/`SW.call_llm`/`R.call_llm`/`HR.call_llm`，mock 套件与 test_section_writer 同一套），TestClient（httpx）驱动后台 worker 线程，覆盖 run→waiting→resume(confirm/revise/replace)→done 状态机（revise 用**谓词等待**防"拿到旧 waiting"竞态、replace 用 `run_until` 的**状态序列**守护"不二次 waiting"）、409/400 校验、cancel（waiting→202、**running→409**）、异常兜底、**run 端点 build_graph 失败→500 清槽回 idle**。**每个用例开头必须 `web_server._reset_for_tests()`**（单槽注册表是进程级全局，cancel→join 线程→清槽→恢复默认模型，否则拿到上个任务状态）。
- **真实 e2e**：跑 `python main.py "题目" --output out.md`，检查日志出现补搜/重试提示、成品是合法 Markdown、质量分正常。注意真实搜索 + LLM 调用可能超过 5 分钟，超时后转后台即可。

## 安全注意事项

- `.env` 含**真实 `DEEPSEEK_API_KEY`**：**绝不在任何输出里回显**，已被 `.gitignore` 忽略，永不提交。
- `.env.example` 是模板，不得放入真实 Key。
- 日志默认写项目根目录 `blog_writer.log`（被 `*.log` 忽略），内容含文章草稿/搜索文本，**不含 API Key**；提交前先 `git status` / `git diff` 确认没有把 `.env`、密钥或日志带进 commit。
