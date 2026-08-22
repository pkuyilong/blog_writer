# CLAUDE.md — b_writer 开发说明

给 Claude Code（以及后续开发者）看的项目说明。面向用户的文档在 [README.md](README.md)，本文件侧重**代码结构、关键决策、踩过的坑**。

## 项目概览

`b_writer` 是一个基于 **LangGraph** 的中文**科普文章**生成多 Agent 项目：给定一个中文题目，自动完成 调研 → 写作 → 审校 的创作。

- LLM：**DeepSeek V4 Flash**（`deepseek-v4-flash`），通过 **OpenAI 兼容端点**调用（`base_url=https://api.deepseek.com`）。**不是 Anthropic API**，别按 Claude SDK 写代码。
- 搜索：**DuckDuckGo**（`ddgs` 库，免费无需 Key），`region=cn-zh` 提升中文结果质量。
- 主图节点：`outline`（子图）→ `write` → `edit`（条件边打回重写，最多 2 次）。

## 常用命令

```bash
# 运行（题目必填）
python main.py "为什么越来越多的人选择远程办公"
python main.py "为什么越来越多的人选择远程办公" --output out.md

# 用虚拟环境
.venv/bin/python main.py "题目"

# 确定性控制流测试（mock chat/web_search，不耗 token）
.venv/bin/python /Users/power/.claude/jobs/4cc34a38/tmp/test_outliner_subagent.py
```

## 架构与数据流

```
主图（graph.py）：START → outline(子图) → write → edit →（should_continue）→ end / rewrite(write)
                                                            └─ passed 为假且 revision_count < MAX_REVISIONS(2) 时打回

outline 子图（agents/outliner.py，自包含）：
  START → search → should_search_again ── 素材够 → generate → should_retry ── 合格 → END
        （搜索+审查）  ├─ 素材不足且未达上限 → search（补搜）         ├─ 不合格且未达上限 → generate（换提示重试）
                      └─ 素材不足且达上限 → fallback                 └─ 不合格且达上限 → fallback
  每条路径最终都保证返回可用的 outline（fallback 兜底）。
```

## 核心模块职责

| 文件 | 职责 |
|---|---|
| `main.py` | CLI 入口：解析题目/输出文件，invoke 主图，打印成品 |
| `graph.py` | 主图编排：`outline → write → edit`，`should_continue` 决定打回重写 |
| `state.py` | `ArticleState`（TypedDict）：主图共享状态，**不含 materials**（素材只在子图内部流动） |
| `llm.py` | DeepSeek 调用封装：`call_llm()`（一次性问答，可选 JSON 模式）/ `chat()`（返回完整响应以便读 `tool_calls`），均 `@traceable` 上报 LangSmith |
| `prompts.py` | 各 Agent 中文 system prompt：RESEARCHER / REVIEWER / OUTLINER / FALLBACK_OUTLINE / WRITER / EDITOR |
| `agents/tools.py` | `web_search()` + `WEB_SEARCH_TOOL`（OpenAI 兼容 function schema） |
| `agents/outliner.py` | **大纲子智能体**：自包含子图（搜索→审查→生成→自检→补搜/重试/兜底） |
| `agents/writer.py` | 写作节点：按提纲写 Markdown 科普文；被打回时带上轮审校意见 |
| `agents/editor.py` | 审校节点：`json_mode=True` 输出 JSON（score/passed/feedback/revised_article） |
| `langsmith_config.py` | LangSmith 配置：读 `.env`、校验环境变量、设置项目名 |

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

5. **JSON 模式要求 prompt 里出现 "json" 字样**：DeepSeek 的 `response_format={"type":"json_object"}` 要求 prompt 中包含 "json" 才生效（`EDITOR_PROMPT` 里已满足）。解析失败时 `editor_node` **保守按通过处理**（`passed=True`），避免把流程卡进死循环。

6. **ReAct 工具调用**：`chat()` 返回完整响应，`agents/outliner.py` 里手动执行 `tool_calls`（逐条 `web_search`），再把 assistant 消息（含 tool_calls）+ tool 结果放回对话交给审查阶段。

7. **DeepSeek 是 OpenAI 兼容格式**：system 提示是 messages 里的一条消息，不是顶层参数。`call_llm` 和 `chat` 都遵守这个约定。

## 测试方法

- **确定性控制流测试**：在模块层替换 `O.chat` / `O.web_search`（fake），断言调用次数与返回键。这样不耗 token 就能覆盖所有分支（收敛/补搜/兜底/重试/私有键不泄漏）。
- 参考：`/Users/power/.claude/jobs/4cc34a38/tmp/test_outliner_subagent.py`（19 项检查）。核心断言模式：场景 A（素材够，1 轮收敛，chat=3）、B（补搜，chat=5）、C（两轮失败兜底，chat=5）、D（提纲两次太短兜底）、E（重试收敛，chat=4）、F（私有键不泄漏）、G（主图节点链 + outline 节点是 `CompiledStateGraph`）。
- **真实 e2e**：跑 `python main.py "题目" --output out.md`，检查日志出现补搜/重试提示、成品是合法 Markdown、质量分正常。注意真实搜索 + LLM 调用可能超过 5 分钟，超时后转后台即可。

## 安全注意事项

- `.env` 含**真实 `DEEPSEEK_API_KEY`**：**绝不在任何输出里回显**，已被 `.gitignore` 忽略，永不提交。
- `.env.example` 是模板，不得放入真实 Key。
- 提交前先 `git status` / `git diff` 确认没有把 `.env` 或密钥带进 commit。
