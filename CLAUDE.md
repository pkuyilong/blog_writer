# CLAUDE.md — blog_writer 开发说明

基于 **LangGraph** 的中文**科普文章**多 Agent 生成项目：给定题目，自动完成 调研 → 写作 → 审校。LLM 走 **DeepSeek**（OpenAI 兼容端点，**非 Anthropic API**），搜索走 **DuckDuckGo**（免费无需 Key）。面向用户的文档在 [README.md](README.md)。

## 运行

命令、断点续跑、Web 页面版等用法见 README（`python main.py "题目"` / `--human-review` / `--resume` / `--prefs` 等）。

## 结构

- 主图 `graph.py`：`outline(子图) → writing(子图) → edit(子图) → remember`；写作子图内嵌 `split → write_section×N 并行 → merge`，审核子图内 3 角色并行打分 + 多数表决，打回只重写问题章节（≤2 次）。
- 核心模块：`state.py`（共享状态）、`llm.py`+`model_router.py`（LLM 调用 + 多模型路由，调用点带 `role=`）、`prompts.py`（各 Agent 中文 prompt）、`output_validation.py`（JSON 输出强校验）、`agents/`（outliner 大纲子图 / writing 写作子图 / section_writer 章节子图 / review 审核子图 / human_review 人工介入节点）、`search_cache.py`（两级搜索缓存）、`memory_store.py`（长期记忆）、`web_server.py`+`web/`（Web 页面版）。

## 硬性约束（改了必出 bug）

1. **孤儿 tool 消息**：OpenAI 兼容 API 要求 tool 消息必须跟在含 `tool_calls` 的 assistant 消息后。`search` 节点每次进入重建干净对话（`messages=[user:题目]`），跨搜索轮绝不累积消息；补搜轮把 `search_history` 以纯文本拼进 user 消息。
2. **子图私有键不泄漏**（langgraph 1.2.11 实测）；Send 并行触发编译子图必须用 `output_schema` 限定输出，否则子图把输入键原样写回父图普通键会抛 `INVALID_CONCURRENT_GRAPH_UPDATE`。
3. **键名硬约束**：章节写作子图草稿键一律用 `section_text`，禁用 `draft`（否则单章文本覆盖整篇 draft）。
4. **DeepSeek 不支持 `response_format json_schema`**，只能客户端 pydantic 强校验 + 结构化字段错误反馈重试；`call_llm` 默认 `json_mode=True`（仅 Markdown 输出显式关），prompt 须含小写 "json" 字样。
5. **搜索缓存**：topic 缓存命中时 `search_round` 照常 +1（不重置，否则命中坏缓存死循环）；`cached_search` 单飞防并发重复搜索。
6. **人工介入**：`interrupt()`+`Command(resume=...)` 必须配 checkpointer；`--resume` 须带相同 `--human-review`；`SqliteSaver.from_conn_string()` 返回 context manager 需 `with` 解包。

## 测试

`tests/` 下每个文件对应一个子智能体/模块的控制流测试（mock LLM/搜索，不耗 token；mock 点与临时库隔离要求见各测试文件）。真实链路：`python main.py "题目" --output out.md`。

## 安全

`.env` 含真实 `DEEPSEEK_API_KEY`：**不回显、不提交**（已被 `.gitignore` 忽略）；`.env.example` 是模板，不得放入真实 Key；提交前 `git status`/`git diff` 确认不带入密钥或日志。
