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

- 🤖 三个 Agent 分工协作：调研选题、写作、审校润色
- 🔍 调研 Agent 通过 `web_search` 工具（DuckDuckGo，免费无需 Key）联网搜索真实资料，素材带来源链接
- 🧐 **检索内容审查**：搜索结果先交给 LLM 审查（来源可信度 / 信息含量 / 相关度），剔除营销味、宽泛无信息、脏数据内容，再基于可靠素材生成提纲
- 🎯 **科普定位**：写作要求生活化类比、术语先解释、通俗但有深度；内容以真实素材为准
- 📝 输出**标准 Markdown**：一级标题 + `##` 小节，可直接渲染
- 🔄 审校 Agent 输出质量分（0-100）与修改意见，**不合格自动打回写作节点重写**（最多 2 次）
- 🧩 审校节点使用结构化 JSON 输出，方便程序读取分数/意见
- 📊 可选接入 **LangSmith** 追踪每次 Agent 执行与 LLM 调用（`llm.py` 用 `@traceable` 上报）
- 💻 纯命令行使用，零界面依赖

## 工作流程

```
题目 ──→ [调研/选题 Agent] ──→ 提纲+素材 ──→ [写作 Agent] ──→ 草稿 ──→ [审校/润色 Agent] ──→ 成品文章
                 │                              ↑____________（不合格则打回重写，最多 2 次）____________|
                 └─ 搜索 → LLM审查素材 → 提纲
```

- **调研/选题**（三阶段）：① 让模型一次提出 2-4 个**具体聚焦**的查询，调用 `web_search`（DuckDuckGo，`region=cn-zh`）联网搜索；② 用 `REVIEWER_PROMPT` 让 LLM **审查搜索结果**，剔除营销味/宽泛/脏数据；③ 基于审查后的可靠素材生成提纲。
- **写作**：按提纲扩写成一篇完整的中文**科普文章**（Markdown 格式）；被打回时结合审校意见修改。
- **审校/润色**：检查错别字、语病，给出质量分（0-100）与修改意见，润色后输出全文（保持 Markdown 标题结构）；不合格则打回写作节点重写（最多 2 次），这就是图中的"条件分支 + 循环"。

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
│   ├── researcher.py       # 调研/选题节点（三阶段：搜索 → LLM审查 → 提纲）
│   ├── writer.py           # 写作节点（Markdown 科普文章）
│   └── editor.py           # 审校/润色节点（JSON 结构化输出）
├── graph.py                # LangGraph 编排：调研 → 写作 → 审校（含打回循环）
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

运行时会依次显示各阶段提示：

```
→ 调研/选题中…（可联网搜索）
  🔍 搜索：多智能体系统 定义 基本概念 智能体
  🔍 搜索：多智能体 应用案例 自动驾驶 无人机 机器人
  🧐 审查搜索结果素材…
  基于审查后的素材整理提纲…
→ 写作中…
→ 审校/润色中…
```

最后打印成品文章及质量信息，例如 `成品文章（质量分 90/100，审校 1 次）`。输出为标准 Markdown（一级标题 + `##` 小节），可直接渲染。

> 说明：搜索后端用 DuckDuckGo（免费、无需 Key），指定 `region=cn-zh` 提升中文结果质量，并清洗 DuckDuckGo 偶发的拼接标题。若搜索不可用，调研 Agent 会自动回退到模型自身知识，流程不会中断。

## 工作原理

1. **状态（`state.py`）**：用 `TypedDict` 定义 `ArticleState`，字段在节点间传递：

   | 字段 | 写入者 | 含义 |
   |---|---|---|
   | `topic` | 用户（`main.py`） | 输入的题目 |
   | `outline` | 调研节点 | 提纲与素材 |
   | `draft` | 写作节点 | 草稿 |
   | `final_article` | 审校节点 | 润色后的文章（合格时为成品） |
   | `quality_score` | 审校节点 | 质量分（0-100） |
   | `passed` | 审校节点 | 是否通过质量检查 |
   | `revision_feedback` | 审校节点 | 打回时的修改意见 |
   | `revision_count` | 审校节点 | 已审校次数（控制循环上限） |

2. **编排（`graph.py`）**：用 `StateGraph` 串联节点，审校后通过条件边 `should_continue` 决定结束还是打回重写。

   ```python
   graph.add_node("research", researcher_node)   # 调研
   graph.add_node("write", writer_node)          # 写作
   graph.add_node("edit", editor_node)           # 审校
   graph.add_edge(START, "research")
   graph.add_edge("research", "write")
   graph.add_edge("write", "edit")
   graph.add_conditional_edges("edit", should_continue,
                               {"rewrite": "write", "end": END})
   ```

   `should_continue` 读到 `passed` 为真或 `revision_count` 达到上限（`MAX_REVISIONS = 2`）就结束，否则打回 `write`。

3. **调研（`agents/researcher.py`）**：三阶段——① 把对话交给模型（带 `web_search` 工具），模型一次并行提出 2-4 个具体查询，逐个执行搜索；② 用 `REVIEWER_PROMPT` 让 LLM 逐条审查搜索结果，剔除营销味、宽泛无信息、与主题无关、明显拼接的脏数据；③ 基于审查后的素材，去掉工具，强制模型输出提纲。搜索限制在单轮内，保证收敛、避免无限搜索。

4. **写作（`agents/writer.py`）**：按提纲写成 Markdown 科普文章；被打回重写时把上轮审校意见一并交给模型。

5. **审校（`agents/editor.py`）**：用 `json_mode=True` 让模型输出 JSON（分数/是否合格/意见/润色全文），解析后写入多个状态字段；润色时保持 Markdown 标题结构；解析失败时保守按通过处理，避免卡死循环。

6. **LLM 调用（`llm.py`）**：通过 OpenAI 兼容端点访问 DeepSeek；`call_llm()` 用于一次性问答（可选 JSON 模式），`chat()` 保留完整响应以便读取 `tool_calls`。两个函数都用 `@traceable` 装饰，LangSmith 启用时每次 LLM 调用会上报为独立的 run，便于在追踪面板查看。


## 重大改动记录

以下是项目演进过程中的关键改动，便于回顾每次变更的目的。

### v1.1 — 调研质量优化（搜索 → 审查 → 提纲）

- **检索内容 LLM 审查**（新增 `REVIEWER_PROMPT`）：搜索结果先由 LLM 逐条评估来源可信度、信息含量、相关度，剔除营销味（"一篇就够了""全面爆发"）、宽泛无信息、拼接脏数据，再基于可靠素材生成提纲。解决"搜索内容质量不高、过于宽泛"的问题。
- **科普定位**：写作端要求生活化类比、术语先解释、通俗但有深度；调研端要求"主题 + 具体方面"的聚焦查询词，不再是宽泛的单一主题词。
- **搜索质量**：`web_search` 指定 `region=cn-zh`（中文权威源更靠前），新增 `_clean_title()` 清洗 DuckDuckGo 偶发的多站点标题拼接。
- **Markdown 输出**：写作端必须输出标准 Markdown（一级标题 + `##` 小节），审校润色时保持标题结构，成品可直接渲染。

### v1.0 — 收敛与可观测性

- **调研收敛**：ReAct 循环改为两阶段（一次搜索 → 强制收敛出提纲），消除"搜索轮次用尽"反复搜索、浪费 token 的问题。
- **LangSmith 集成**：新增 `langsmith_config.py`、`.env.example`、`LANGSMITH.md`；`llm.py` 用 `@traceable` 上报每次 LLM 调用，可在 smith.langchain.com 查看完整执行追踪。
- **去 AI 味**：重写写作/审校 prompt，禁用套话与模板结构，要求长短句交错、具体数据落地。

## 学习与扩展方向

这是一个典型的**多 Agent 编排**入门项目，已经用上了 LangGraph 的几个核心能力，可以继续扩展：

- **工具调用（已实现）**：调研节点通过 function calling + ReAct 循环调用搜索工具
- **条件分支与循环（已实现）**：审校不合格打回写作重写（`add_conditional_edges`）
- **结构化输出（已实现）**：审校节点用 JSON 模式输出分数/意见
- **可观测性（已实现）**：已接入 LangSmith，`@traceable` 上报每次 LLM 调用
- **主管-下属架构**：再加一个"主编"Agent 调度多个写手，`langgraph` 有 `prebuilt` 支持
- **流式输出**：`stream=True` 让文章逐段显示
- **素材质量再提升**：对搜索来源做白名单权重（官方/学术源加分）、交叉核对数据

## License

MIT
