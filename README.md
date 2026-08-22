# 文章生成 Agent（b_writer）

一个基于 **LangGraph** 的初级多 Agent 项目：给定一个题目，自动完成文章创作。

## 流程

三个 Agent 依次执行，共享一份状态（`ArticleState`）：

```
题目 ──→ [调研/选题 Agent] ──→ 提纲+素材 ──→ [写作 Agent] ──→ 草稿 ──→ [审校/润色 Agent] ──→ 成品文章
                                                    ↑____________（不合格则打回重写，最多 2 次）____________|
```

- **调研/选题**：通过 ReAct 循环调用 `web_search` 工具（DuckDuckGo，无需 API Key）联网搜索真实资料，产出带来源链接的提纲与素材。
- **写作**：按提纲扩写成一篇完整的中文文章；被打回时结合审校意见修改。
- **审校/润色**：检查错别字、语病，给出质量分（0-100）与修改意见，润色后输出全文；不合格则打回写作节点重写（最多 2 次），这就是图中的"条件分支 + 循环"。

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

运行时会依次显示 `→ 调研/选题中…（可联网搜索）`、`→ 写作中…`、`→ 审校/润色中…` 三个阶段提示；调研阶段每发起一次搜索会打印一行 `🔍 搜索：<关键词>`。最后打印成品文章。

> 说明：搜索后端用 DuckDuckGo（免费、无需 Key）。若搜索不可用，调研 Agent 会自动回退到模型自身知识，流程不会中断。

## 项目结构

```
b_writer/
├── requirements.txt        # langgraph, openai
├── state.py                # ArticleState: TypedDict 定义
├── llm.py                  # DeepSeek 调用封装 call_llm()
├── prompts.py              # 三个 Agent 的中文 system prompt
├── agents/
│   ├── __init__.py
│   ├── tools.py           # web_search 联网搜索工具（DuckDuckGo）
│   ├── researcher.py      # 调研/选题节点（ReAct 循环，可调用搜索工具）
│   ├── writer.py          # 写作节点
│   └── editor.py          # 审校/润色节点
├── graph.py                # LangGraph 编排：调研 → 写作 → 审校
└── main.py                 # CLI 入口
```
