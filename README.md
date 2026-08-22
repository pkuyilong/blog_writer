# b_writer — 文章生成 Agent

一个基于 **LangGraph** 的初级多 Agent 项目：给定一个题目，自动完成中文文章的创作。

三个 Agent（调研/选题 → 写作 → 审校/润色）依次协作，共享一份状态，LLM 通过 **DeepSeek V4 Flash**（`deepseek-v4-flash`，OpenAI 兼容端点）调用。

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
- 📝 输出结构完整的中文文章
- 🔗 LangGraph 状态机编排，节点共享 `ArticleState`
- 💻 纯命令行使用，零界面依赖
- 🚀 基于 DeepSeek V4 Flash，价格低、速度快

## 工作流程

```
题目 ──→ [调研/选题 Agent] ──→ 提纲+素材 ──→ [写作 Agent] ──→ 草稿 ──→ [审校/润色 Agent] ──→ 成品文章
```

- **调研/选题**：围绕题目产出文章提纲与关键素材。
- **写作**：按提纲扩写成一篇完整的中文文章。
- **审校/润色**：检查错别字、语病，润色后输出最终全文。

## 项目结构

```
b_writer/
├── requirements.txt        # langgraph, openai
├── state.py                # ArticleState：节点间共享的状态定义
├── llm.py                  # DeepSeek 调用封装 call_llm()
├── prompts.py              # 三个 Agent 的中文 system prompt
├── agents/
│   ├── __init__.py
│   ├── researcher.py       # 调研/选题节点
│   ├── writer.py           # 写作节点
│   └── editor.py           # 审校/润色节点
├── graph.py                # LangGraph 编排：调研 → 写作 → 审校
└── main.py                 # CLI 入口
```

## 环境要求

- Python 3.10 及以上
- 一个可用的 DeepSeek API Key（[DeepSeek 开放平台](https://platform.deepseek.com) 申请）

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
→ 调研/选题中…
→ 写作中…
→ 审校/润色中…
```

最后打印成品文章。

## 工作原理

1. **状态（`state.py`）**：用 `TypedDict` 定义 `ArticleState`，四个字段在节点间传递：

   | 字段 | 写入者 | 含义 |
   |---|---|---|
   | `topic` | 用户（`main.py`） | 输入的题目 |
   | `outline` | 调研节点 | 提纲与素材 |
   | `draft` | 写作节点 | 草稿 |
   | `final_article` | 审校节点 | 成品文章 |

2. **编排（`graph.py`）**：用 `StateGraph` 把三个节点连成线性链，`invoke()` 一次跑完。

   ```python
   graph.add_node("research", researcher_node)   # 调研
   graph.add_node("write", writer_node)          # 写作
   graph.add_node("edit", editor_node)           # 审校
   graph.add_edge(START, "research")
   graph.add_edge("research", "write")
   graph.add_edge("write", "edit")
   graph.add_edge("edit", END)
   ```

3. **节点（`agents/*.py`）**：每个节点是一个普通 Python 函数 `(state) -> dict`，读取上游字段、调用一次 `call_llm()`、把结果写入自己的字段。

4. **LLM 调用（`llm.py`）**：通过 OpenAI 兼容端点访问 DeepSeek，`system` 提示作为消息中的一条角色消息传入。

## 常见问题

| 现象 | 原因与解决 |
|---|---|
| `KeyError: 'DEEPSEEK_API_KEY'` | 未设置环境变量，先执行 `export DEEPSEEK_API_KEY=sk-...` |
| `ModuleNotFoundError: langgraph` | 未安装依赖，执行 `pip install -r requirements.txt` |
| 输出的文章过短/被截断 | 默认 `max_tokens=16000` 一般够用；如需更长的文章可在 `llm.py` 调高该值 |

## 学习与扩展方向

这是一个典型的**多 Agent 编排**入门项目，围绕它可以继续学习：

- **条件分支与循环**：给审校加质量判断，不合格打回写作重写（`add_conditional_edges`）
- **工具调用**：让调研节点调用联网搜索工具获取真实素材（function calling / ReAct 循环）
- **结构化输出**：让节点输出 JSON / Pydantic 对象而非自由文本，方便下游程序处理
- **流式输出**：`stream=True` 让文章逐段显示
- **可观测性**：接入 LangSmith 或 `graph.get_state()` 查看每一步状态

## License

MIT
