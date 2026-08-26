# CLAUDE.md — blog_writer 开发说明

给 Claude Code（以及后续开发者）看的项目说明。面向用户的文档在 [README.md](README.md)，本文件侧重**代码结构、关键决策、踩过的坑**。

## 项目概览

`blog_writer` 是一个基于 **LangGraph** 的中文**科普文章**生成多 Agent 项目：给定一个中文题目，自动完成 调研 → 写作 → 审校 的创作。

- LLM：**DeepSeek V4 Flash**（`deepseek-v4-flash`），通过 **OpenAI 兼容端点**调用（`base_url=https://api.deepseek.com`）。**不是 Anthropic API**，别按 Claude SDK 写代码。
- 多模型路由（`model_router.py`）：所有 LLM 调用按**角色**（research/outline/split/write/edit/revise_outline）路由到候选模型链，调用失败自动 fallback 切下一个；`--model` 可覆盖全局默认。目前只注册 DeepSeek，加第二个模型只需在注册表加一个 `ModelSpec`（环境里已有 `DASHSCOPE_API_KEY`/Qwen 可用）。
- 搜索：**DuckDuckGo**（`ddgs` 库，免费无需 Key），`region=cn-zh` 提升中文结果质量。**两级搜索缓存**（`search_cache.py`）：query 级缓存 `web_search` 原始结果 + topic 级缓存审查后素材（SQLite `.cache/` 跨进程复用，TTL 7 天，`--clear-search-cache` 清空）。
- 主图节点：`outline`（子图）→ `writing`（写作子 Agent 子图，内部 `split` → `write_section`（子图，Send 并行 ×N）→ `merge`）→ `edit`（条件边打回只重写问题章节，最多 2 次）。
- 搜索：outliner 子图内 **ThreadPool 并行**（`MAX_PARALLEL_SEARCHES=4`）；写作按章节 **Send 图级并行**。

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

# 确定性控制流测试（mock chat/cached_search，不耗 token）
.venv/bin/python /Users/power/.claude/jobs/4cc34a38/tmp/test_outliner_subagent.py
```

## 架构与数据流

```
主图（graph.py）：START → outline(子图) → [human_review?] → writing(写作子Agent子图) → edit
                                                                    ↑                  │
                                                    rewrite（只重写问题章节）←（should_continue）┘
  · human_review（可选，--human-review 开关经条件边启用）：大纲后 interrupt() 暂停，把大纲交给 main.py 交互循环；
    回车确认 / 意见重写 / #粘贴后 Command(resume=...) 续跑；有意见（outline_review_feedback）经 route_review 条件边回环
    重写再二次确认；默认关闭完全不执行
  · edit：passed 为假且 revision_count < MAX_REVISIONS(2) → 打回 writing（只重写 failed_sections）
```

writing 子图（agents/writing.py，自包含，挂 writing 节点）：
  START → split →(fan_out：Send×N 或 "merge")→ write_section(子图) → merge → END
  · split：LLM 把 outline 文本拆成 [{title, points, materials}]（revision_count>0 时复用，不重复调 LLM）
  · fan_out（split 的条件边）：返回 [Send("write_section", ...)×N]；打回时只 Send failed_sections 里的章节
  · write_section：并行写单章，返回 {"section_drafts": {id: text}}（reducer {**a,**b} 聚合，重写覆盖同 id）
  · merge：按 id 升序拼装各章节草稿 → draft
  · output_schema 只写回 {draft, sections, section_drafts}：sections/section_drafts 是跨重写循环的共享通道

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
| `llm.py` | LLM 调用统一封装（消息形状 + `@traceable`）：`call_llm()`（一次性问答，可选 JSON 模式）/ `chat()`（返回完整响应以便读 `tool_calls`）；新增 keyword-only 的 `model`/`role` 参数，委托 model_router 选模型与兜底 |
| `model_router.py` | **多模型路由**：`ModelSpec` 注册表（MODEL_REGISTRY）+ `ROLE_MODEL_MAP`（role→候选模型链，`__default__` 哨兵跟随全局默认）+ `resolve_chain()`（显式 model > role 链 > 全局默认，能力过滤）+ `call_with_fallback()`（失败切下一个模型）+ `get_client()`（按 provider 懒加载缓存）；`ModelRoutingError` |
| `prompts.py` | 各 Agent 中文 system prompt：RESEARCHER / REVIEWER / OUTLINER / FALLBACK_OUTLINE / REVISE_OUTLINE / SPLIT / WRITE_SECTION / EDITOR |
| `agents/tools.py` | `web_search()` + `WEB_SEARCH_TOOL`（OpenAI 兼容 function schema） |
| `search_cache.py` | **两级搜索缓存**：`search_cache` 表（query→web_search 原始结果）+ `topic_materials` 表（topic→审查后素材），SQLite 跨进程复用；`cached_search`（query 级，单飞防并发重复搜索）/ `get_cached_materials`+`store_materials`（topic 级）/ `clear()`（供 `--clear-search-cache`）；TTL 惰性失效 |
| `agents/outliner.py` | **大纲子智能体**：自包含子图（搜索→审查→生成→自检→补搜/重试/兜底）；搜索多查询 ThreadPool 并行 |
| `agents/writing.py` | **写作子 Agent**（自包含子图，挂 `writing` 节点）：内部 `split_sections`（拆章，打回复用）/ `fan_out_write`（条件边，返回 `[Send]` 或 "merge"）/ `merge_sections`（按 id 升序拼装）；`output_schema` 只写回 `draft`/`sections`/`section_drafts`；**不含写作 LLM** |
| `agents/section_writer.py` | **章节写作子智能体**（自包含子图，挂 `write_section` 节点）：`write`（初稿/重写）→ `self_check`（启发式自检：篇幅/标题/要点）→ `should_rewrite`（条件重写，`MAX_SECTION_ATTEMPTS` 上限）→ `emit`（输出 `section_drafts`）；`output_schema` 限定子图只输出该键 |
| `agents/editor.py` | 审校节点：`json_mode=True` 输出 JSON（score/passed/revised_article/**failed_sections**[{id,feedback}]）；解析失败重试 `EDITOR_MAX_RETRIES` 次，耗尽才保守通过 |
| `agents/human_review.py` | **人工介入节点**：`interrupt()` 暂停把大纲交给客户端，按 `Command(resume=...)` 的 action（confirm/revise/replace）决定下一步；revise 时把 `outline_review_feedback` 写进 state、经 `route_review` 条件边回环，节点开头按意见重写（REVISE_OUTLINE_PROMPT）再二次确认；`build_graph(enable_human_review=True)` 经条件边启用 |
| `langsmith_config.py` | LangSmith 配置：读 `.env`、校验环境变量、设置项目名 |
| `logging_config.py` | 统一日志：所有模块用 `logging` 替代 print；stderr 简洁输出（保留 emoji 观感）+ 文件（默认 `blog_writer.log`）详细追踪；`--verbose` 控制终端级别；`setup_logging()` 幂等 |

## 关键设计决策与踩过的坑（务必阅读）

1. **孤儿 tool 消息问题（最坑）**：OpenAI 兼容 API 要求 tool 消息必须跟在含 `tool_calls` 的 assistant 消息后面。程序**主动**发起的补搜如果往上一轮对话里追加 tool 消息（没有对应的 assistant tool_calls）会直接 400。**解法：`search` 节点每次进入都重新构建干净对话**（`messages = [user: 题目]`），搜索轮次之间绝不累积消息。改这块时务必保持这个不变量。

2. **子图私有键不泄漏（langgraph 1.2.11 实测）**：子图作为父图节点时，`OutlineState` 里的私有键（`search_round` / `outline_attempt` / `materials`）**不会写回父图 `ArticleState`**，且私有计数能跨子图内部自循环正确递增。所以补搜/重试计数放子图私有，不用加到 `ArticleState`。若升级 langgraph，需回归验证这一点。

3. **失败分类路由**：素材问题（搜索失败/审查太差）和提纲问题（太短）**分开处理**——素材不足→补搜，提纲不合格→重试，不要混为一谈。阈值：
   - `MAX_ATTEMPTS = 2`（提纲生成 1 次 + 重试 1 次）
   - `MAX_SEARCH_ROUNDS = 2`（先搜 1 轮 + 补搜 1 轮）
   - `MIN_OUTLINE_LEN = 60`（提纲低于此长度视为不可用）
   - `MIN_MATERIALS_LEN = 40`（素材低于此长度视为不足）
   - `FAILURE_MARKERS`：素材/审查文本中含这些词视为"没搜到"

4. **审查失败降级**：`search` 节点里，LLM 审查结果不可用（`_materials_ok` 为假）时**退回原始搜索结果**作为素材——这样即使审查坏了，后续 `should_search_again` 还能基于真实内容判断是否补搜。

5. **JSON 模式要求 prompt 里出现 "json" 字样**：DeepSeek 的 `response_format={"type":"json_object"}` 要求 prompt 中包含 "json" 才生效（`EDITOR_PROMPT` 里已满足）。解析失败时 `editor_node` **重试**（`EDITOR_MAX_RETRIES=2`，重试提示含"不是合法 JSON"），重试耗尽才**保守按通过处理**（`passed=True`），避免把流程卡进死循环。`revision_count` 只在节点执行时 +1，不因重试多计。

6. **ReAct 工具调用**：`chat()` 返回完整响应，`agents/outliner.py` 里手动执行 `tool_calls`（逐条 `web_search`），再把 assistant 消息（含 tool_calls）+ tool 结果放回对话交给审查阶段。

7. **DeepSeek 是 OpenAI 兼容格式**：system 提示是 messages 里的一条消息，不是顶层参数。`call_llm` 和 `chat` 都遵守这个约定。

8. **Send 并行写作（按章节）**（封装在 `agents/writing.py` 写作子 Agent 子图内部，语义不变）：`split` 的条件边 `fan_out_write` 返回 `[Send("write_section", {section_id, section, ...})×N]`，下一 superstep 并行执行同一节点多次；`write_section → merge` 由内置屏障同步，**所有分支写完才触发 merge**。`section_drafts` 用 `Annotated[dict, _merge_dicts]` 聚合——重写章节时同 id 覆盖旧草稿（reducer 语义已验证）。打回时 `fan_out_write` 只看 `failed_sections`（`[{id, feedback}]`，每章带专属修改意见）只 Send 问题章节并把该章意见传给 `write_section`，`split` 在 `revision_count>0` 时直接复用 sections **不再调 LLM**。

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
    - **不做"要点覆盖"子串检查（真实 e2e 两次 100% 误判后移除）**：split 生成的 `points` 是**描述性写作指令**（如"用生活化场景对比早高峰通勤与在家办公的差异"），正文几乎不可能逐字复述——任何固定长度阈值都会误判，两次真实 e2e 中 7 章全部被误判"未覆盖要点"、白白各多付一次重写调用，还把错误意见塞回模型。**要点覆盖是"内容层"检查，与 LLM 自由表达天生不兼容，交给外部 editor 审校**；子图自检只保留篇幅/标题两个确定性检查（宁可漏检，不可误检）。

12. **Send 并行触发编译子图：`output_schema` 限定输出是硬要求**（langgraph 1.2.11 实测踩坑）：fan_out 的 Send payload `{section, topic, feedback}` 直接是子图输入 state；多个子图实例同一 superstep 并行结束时，若子图把输入键（如 topic）**原样写回父图**，父图 topic 是普通键（LastValue），同一 superstep 收到多个值会抛 `INVALID_CONCURRENT_GRAPH_UPDATE`。**解法：`StateGraph(SectionWriterState, output_schema=SectionWriterOutput)` 把子图 output_channels 限定为 `section_drafts`**，冲突从根上消除（父图零改动）。若想给子图加新输出，改 `SectionWriterOutput` 即可。
    - **重构后双层 output_schema**：写作链收进 `writing` 子图后形成两层限定——`section_writer` 限 `section_drafts`（写作子图内并行实例只写 reducer 键）、`writing` 限 `{draft, sections, section_drafts}`（对主图是单实例顺序节点，一次性 apply_writes）。Send 并行只发生在写作子图内部，主图永不出现并发写。

13. **子图内部键名避免与父图通道重名**：`ArticleState` 与 `writing` 子图 `WritingState` 都有 `draft` 通道，章节级草稿键若命名 `draft`，会把单章文本写回并覆盖整篇 draft、破坏 merge 结果。**硬约束：章节写作子图一律用 `section_text`**（`SectionWriterState` 顶部注释列禁用名）。子图私有键（`section_text`/`write_attempt`/`self_check_notes`）与父图键零重叠，由 output_schema 保证不写回父图。

14. **多模型路由（model_router.py）——角色路由 + fallback 链 + `--model` 覆盖**：8 个 LLM 调用点各带 `role=`，路由表 `ROLE_MODEL_MAP` 把 role 映射到候选模型链，单次调用失败自动切下一个。要点：
    - **`__default__` 哨兵是 `--model` 生效的关键**：role 链默认都指向哨兵（=跟随全局默认模型），于是 `--model X` 一处切换让所有角色切到 X；将来某环节想用更强模型只改那一个 role 的链（如 `{"edit": ["deepseek-reasoner", DEFAULT_MODEL]}`）。guard：哨兵字符串不能作为真实模型名。
    - **resolve 优先级**：显式 `model=` > role 链 > 全局默认；`json_mode`/`tools` 会带 `required_capabilities`，role 链里能力不符的模型**静默跳过**、显式 model 能力不符**直接报错**。
    - **fallback 是"API/传输异常"层**（默认 `openai.OpenAIError`，含超时/限流/连接错误），与 editor 的 JSON 解析重试（"返回内容非法"层）**两层正交**——前者切模型、后者同模型重试，互不影响。
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

## 测试方法

- **确定性控制流测试**：在模块层替换 `O.chat` / `O.cached_search`（fake），断言调用次数与返回键。这样不耗 token 就能覆盖所有分支（收敛/补搜/兜底/重试/私有键不泄漏）。注意 v2.3 起还需 mock `O.get_cached_materials`/`O.store_materials`（防连真库，见决策 #15）。
- 参考：`/Users/power/.claude/jobs/4cc34a38/tmp/test_outliner_subagent.py`（25 项检查）。核心断言模式：场景 A（素材够，1 轮收敛，chat=3）、B（补搜，chat=5）、C（两轮失败兜底，chat=5）、D（提纲两次太短兜底）、E（重试收敛，chat=4）、F（私有键不泄漏）、G（主图节点链 {outline, writing, edit} + outline/writing 节点是 `CompiledStateGraph`）、H（并行搜索 3 查询）。
- 按章节并行写作：`/Users/power/.claude/jobs/4cc34a38/tmp/test_section_writer.py`（16 项检查）——mock 主图全部 LLM（`agents.writing.call_llm`/`agents.section_writer.call_llm`/`agents.editor.call_llm`/`agents.outliner.chat`），走完整链路验证：split 调 1 次 → write_section 子图 ×N（计数加锁，per-title 版本号断言、不依赖并行执行顺序）→ merge 按 id 顺序正确 → 审校失败 → 打回只重写问题章节（旧草稿被覆盖、其余保留、专属意见传递）→ 子图私有键不泄漏。这条测试同时守护「共享通道跨子图边界写回」——若 sections/section_drafts 没写回父图，重写轮会丢 v1。
- 章节写作子智能体：`/Users/power/.claude/jobs/4cc34a38/tmp/test_section_subagent.py`（16 项检查）——mock `agents.section_writer.call_llm`，覆盖首写合格（不触发自检重写，对比旧版无条件反思省 1 次）/ 不合格→重写收敛 / 两次不合格接受 / 审校意见传递 / 要点不做子串检查（避免误判）/ 输出 schema 只暴露 section_drafts。
- 审校解析重试：`/Users/power/.claude/jobs/4cc34a38/tmp/test_editor_retry.py`（13 项检查）——mock `agents.editor.call_llm`，覆盖首出合法（调 1 次）/ 失败一次后成功（调 2 次、第二次 user_content 含"不是合法 JSON"）/ 连续失败到上限保守通过。
- 人工介入：`/Users/power/.claude/jobs/4cc34a38/tmp/test_human_review.py`（21 项检查）——mock `agents.human_review.input`（编程序列）与 `call_llm`，单测节点四种输入（回车/意见重写/粘贴大纲/q 退出）+ 整图开关开走通 + 开关关不触发节点 + 跨进程断点续跑。
- 多模型路由：`/Users/power/.claude/jobs/4cc34a38/tmp/test_model_router.py`（24 项检查）——mock `llm.get_client`（fake client）与 `llm.call_with_fallback`（spy），覆盖 role 路由解析 / `__default__` 哨兵→全局默认 / `set_default_model` 覆盖 / 能力过滤（role 链跳过 vs 显式 model 报错）/ fallback 成功与耗尽（保留 `__cause__`）/ 8 个调用点 role 正确传递 / 旧无参行为不变 / client 懒加载缓存与缺 env 报错。
- 搜索缓存：`/Users/power/.claude/jobs/4cc34a38/tmp/test_search_cache.py`（18 项检查）——mock `search_cache.web_search`（临时库隔离，覆盖 `DB_PATH`），覆盖 query 级 miss/命中 / key 规范化（首尾空白、大小写）/ TTL 过期重新搜索 / topic 级 store→hit→过期 miss / `clear()` 清空并返回条数 / **并发单飞（8 线程同 query，真实搜索次数 == 1）** / 空 key 不缓存。
- **真实 e2e**：跑 `python main.py "题目" --output out.md`，检查日志出现补搜/重试提示、成品是合法 Markdown、质量分正常。注意真实搜索 + LLM 调用可能超过 5 分钟，超时后转后台即可。

## 安全注意事项

- `.env` 含**真实 `DEEPSEEK_API_KEY`**：**绝不在任何输出里回显**，已被 `.gitignore` 忽略，永不提交。
- `.env.example` 是模板，不得放入真实 Key。
- 日志默认写项目根目录 `blog_writer.log`（被 `*.log` 忽略），内容含文章草稿/搜索文本，**不含 API Key**；提交前先 `git status` / `git diff` 确认没有把 `.env`、密钥或日志带进 commit。
