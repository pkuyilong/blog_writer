# 文章生成 Agent（b_writer）

一个基于 **LangGraph** 的初级多 Agent 项目：给定一个题目，自动完成文章创作。

## 流程

三个 Agent 依次执行，共享一份状态（`ArticleState`）：

```
题目 ──→ [调研/选题 Agent] ──→ 提纲+素材 ──→ [写作 Agent] ──→ 草稿 ──→ [审校/润色 Agent] ──→ 成品文章
```

- **调研/选题**：围绕题目产出文章提纲与关键素材。
- **写作**：按提纲扩写成一篇完整的中文文章。
- **审校/润色**：检查错别字、语病，润色后输出最终全文。

LLM 通过 **DeepSeek V4 Flash**（`deepseek-v4-flash`，OpenAI 兼容端点）调用。

## 安装

```bash
cd b_writer
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 配置 API Key

```bash
export DEEPSEEK_API_KEY=sk-...
```

## 运行

```bash
python main.py "为什么越来越多的人选择远程办公"
```

可选：把成品保存到文件。

```bash
python main.py "为什么越来越多的人选择远程办公" --output out.md
```

运行时会依次显示 `→ 调研/选题中…`、`→ 写作中…`、`→ 审校/润色中…` 三个阶段提示，最后打印成品文章。

## 项目结构

```
b_writer/
├── requirements.txt        # langgraph, openai
├── state.py                # ArticleState: TypedDict 定义
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
