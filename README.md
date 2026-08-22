# b_writer — 文章生成 Agent

一个基于 **LangGraph** 的初级多 Agent 项目：给定一个题目，自动完成中文文章的创作。

三个 Agent（调研/选题 → 写作 → 审校/润色）依次协作，共享一份状态。LLM 通过 **DeepSeek V4 Flash**（`deepseek-v4-flash`，OpenAI 兼容端点）调用，调研 Agent 可通过 **DuckDuckGo** 联网搜索真实资料。

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
- [学习与扩展方向](#学习与扩展方向)

## 功能特性

- 🤖 三个 Agent 分工协作：调研选题、写作、审校润色
- 🔍 调研 Agent 通过 `web_search` 工具（DuckDuckGo，免费无需 Key）联网搜索真实资料，素材带来源链接
- 🔄 审校 Agent 输出质量分（0-100）与修改意见，**不合格自动打回写作节点重写**（最多 2 次）
- 🧩 审校节点使用结构化 JSON 输出，方便程序读取分数/意见
- 📝 输出结构完整、数据可核验的中文文章
- 💻 纯命令行使用，零界面依赖

## 工作流程

```
题目 ──→ [调研/选题 Agent] ──→ 提纲+素材 ──→ [写作 Agent] ──→ 草稿 ──→ [审校/润色 Agent] ──→ 成品文章
                                                    ↑____________（不合格则打回重写，最多 2 次）____________|
```

- **调研/选题**：通过 ReAct 循环调用 `web_search` 工具（DuckDuckGo，无需 API Key）联网搜索真实资料，产出带来源链接的提纲与素材。
- **写作**：按提纲扩写成一篇完整的中文文章；被打回时结合审校意见修改。
- **审校/润色**：检查错别字、语病，给出质量分（0-100）与修改意见，润色后输出全文；不合格则打回写作节点重写（最多 2 次），这就是图中的"条件分支 + 循环"。

## 项目结构

```
b_writer/
├── requirements.txt        # langgraph, openai, ddgs
├── state.py                # ArticleState：节点间共享的状态定义
├── llm.py                  # DeepSeek 调用封装 call_llm() / chat()
├── prompts.py              # 三个 Agent 的中文 system prompt
├── agents/
│   ├── __init__.py
│   ├── tools.py            # web_search 联网搜索工具（DuckDuckGo）
│   ├── researcher.py       # 调研/选题节点（ReAct 循环，可调用搜索工具）
│   ├── writer.py           # 写作节点
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

## 运行

```bash
python main.py "为什么越来越多的人选择远程办公"
```

可选：把成品保存到文件。

```bash
python main.py "为什么越来越多的人选择远程办公" --output out.md
```

运行时会依次显示三个阶段提示：

```
→ 调研/选题中…（可联网搜索）
  🔍 搜索：远程办公 趋势 数据 2024 人数增长
  🔍 搜索：远程办公 好处 优点 工作效率 通勤
→ 写作中…
→ 审校/润色中…
```

最后打印成品文章及质量信息，例如 `成品文章（质量分 90/100，审校 1 次）`。

> 说明：搜索后端用 DuckDuckGo（免费、无需 Key）。若搜索不可用，调研 Agent 会自动回退到模型自身知识，流程不会中断。

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

3. **调研（`agents/researcher.py`）**：ReAct 循环——把对话交给模型（带 `web_search` 工具），模型若请求搜索就执行并把结果放回对话，直到模型不再请求工具、直接给出提纲。这是"模型自主决定是否、以及搜什么"的 Agent 行为。

4. **写作（`agents/writer.py`）**：按提纲写作；被打回重写时把上轮审校意见一并交给模型。

5. **审校（`agents/editor.py`）**：用 `json_mode=True` 让模型输出 JSON（分数/是否合格/意见/润色全文），解析后写入多个状态字段；解析失败时保守按通过处理，避免卡死循环。

6. **LLM 调用（`llm.py`）**：通过 OpenAI 兼容端点访问 DeepSeek；`call_llm()` 用于一次性问答（可选 JSON 模式），`chat()` 保留完整响应以便读取 `tool_calls`。

## 常见问题

| 现象 | 原因与解决 |
|---|---|
| `KeyError: 'DEEPSEEK_API_KEY'` | 未设置环境变量，先执行 `export DEEPSEEK_API_KEY=sk-...` |
| `ModuleNotFoundError: langgraph` / `ddgs` | 未安装依赖，执行 `pip install -r requirements.txt` |
| 调研阶段没有触发搜索 | 搜索可能不可用或被限流，模型会回退到自身知识；可重试运行 |
| 输出的文章过短/被截断 | 默认 `max_tokens=16000` 一般够用；如需更长的文章可在 `llm.py` 调高该值 |

## 学习与扩展方向

这是一个典型的**多 Agent 编排**入门项目，已经用上了 LangGraph 的几个核心能力，可以继续扩展：

- **工具调用（已实现）**：调研节点通过 function calling + ReAct 循环调用搜索工具
- **条件分支与循环（已实现）**：审校不合格打回写作重写（`add_conditional_edges`）
- **结构化输出（已实现）**：审校节点用 JSON 模式输出分数/意见
- **主管-下属架构**：再加一个"主编"Agent 调度多个写手，`langgraph` 有 `prebuilt` 支持
- **流式输出**：`stream=True` 让文章逐段显示
- **可观测性**：接入 LangSmith 或 `graph.get_state()` 查看每一步状态

## License

MIT
