# LangSmith 可观测性使用指南

LangSmith 是 LangGraph 官方可观测性平台，用于追踪 agent 执行过程、状态变化、token 使用情况、性能分析等。

## 为什么需要 LangSmith？

- **追踪执行过程**：查看每个节点何时执行、消耗多少 token、输出什么内容
- **调试问题**：定位 agent 产出质量问题、搜索失败、循环异常等
- **性能分析**：分析每个节点的执行时间和 token 使用情况
- **数据导出**：导出追踪数据进行二次分析或展示

## 快速开始

### 1. 注册 LangSmith 账号并获取 API Key

1. 访问 https://smith.langchain.com/ 注册账号
2. 登录后进入 **API Keys** 页面
3. 创建一个新的 API Key，复制下来（格式如 `sk-...`）

### 2. 配置环境变量

**临时配置（当前终端会话）**：
```bash
export LANGCHAIN_TRACING_V2=true
export LANGCHAIN_API_KEY=你的API密钥
export LANGCHAIN_PROJECT=b_writer  # 可选，默认为 'default'
```

**永久配置（推荐）**：

**Zsh（macOS 默认）**：
```bash
echo 'export LANGCHAIN_TRACING_V2=true' >> ~/.zshrc
echo 'export LANGCHAIN_API_KEY=你的API密钥' >> ~/.zshrc
echo 'export LANGCHAIN_PROJECT=b_writer' >> ~/.zshrc
source ~/.zshrc
```

**Bash**：
```bash
echo 'export LANGCHAIN_TRACING_V2=true' >> ~/.bashrc
echo 'export LANGCHAIN_API_KEY=你的API密钥' >> ~/.bashrc
echo 'export LANGCHAIN_PROJECT=b_writer' >> ~/.bashrc
source ~/.bashrc
```

### 3. 运行项目并查看追踪

```bash
cd b_writer
python main.py "为什么越来越多的人选择远程办公"
```

运行时你会看到 LangSmith 启用的提示：
```
✓ LangSmith 追踪已启用：项目=b_writer，会话=run_12345_1695555555
  访问 https://smith.langchain.com/organization 查看追踪数据
  如需调整项目名称，设置环境变量：LANGCHAIN_PROJECT=你的项目名
```

## 如何查看追踪数据

### 方法 1：在 LangSmith 网页界面查看

1. 登录 https://smith.langchain.com/
2. 在左侧导航栏选择你的项目（如 `b_writer`）
3. 你会看到所有运行过的 trace，点击可以查看：
   - **完整执行流程**：每个节点的执行顺序和时间
   - **输入输出**：每个节点的输入 state 和输出值
   - **中间过程**：搜索查询、审校分数、质量分等
   - **Token 使用**：每个节点消耗的 token 总量
   - **性能指标**：执行时长、速率限制等

### 方法 2：使用 LangSmith CLI 工具

安装 LangSmith CLI：
```bash
pip install langsmith-cli
```

查看最近的追踪：
```bash
langsmith traces --project b_writer
```

## 配置选项

### 自定义项目名称

默认项目名为 `b_writer`，可以在代码中修改：

```python
from langsmith_config import setup_langsmith
tracer = setup_langsmith(project_name="我的文章生成项目")
```

或通过环境变量：
```bash
export LANGCHAIN_PROJECT=我的文章生成项目
```

### 会话命名

默认会话名为当前进程 ID 加时间戳，如 `run_12345_1695555555`。你可以自定义：

```python
tracer = setup_langsmith(session_name="测试会话")
```

## 常见问题

### Q: 追踪没有出现？

**检查项**：
1. 确认 `LANGCHAIN_TRACING_V2=true` 已设置
2. 确认 `LANGCHAIN_API_KEY` 已设置且正确
3. 确认已安装 `langgraph-langsmith`：`pip list | grep langsmith`
4. 重新运行项目（LangSmith 会为每次运行创建新的 trace）

### Q: Token 使用量很高？

- LangSmith 会记录每个节点的完整输入输出，长文章会消耗较多 token
- 可以在 LangSmith 界面中过滤查看
- 定期清理旧追踪数据以节省存储

### Q: 如何禁用 LangSmith？

设置：
```bash
export LANGCHAIN_TRACING_V2=false
```

或在代码中不调用 `setup_langsmith()`。

### Q: 是否需要付费？

LangSmith 有免费额度：
- **免费用户**：每月 10,000 次追踪
- **付费用户**：更高额度和高级功能

详细信息请访问：https://smith.langchain.com/pricing

## 常用操作

### 查看某个 trace 的详细信息

在 LangSmith 网页界面，点击某个 trace → **Details**：
- **Trace 事件**：节点执行顺序
- **Input/Output**：state 的输入输出
- **Span**：子节点或函数调用
- **Events**：搜索查询、API 调用等中间事件

### 导出追踪数据

1. 在 LangSmith 界面选择 trace
2. 点击 **Export** → 选择格式（JSON/CSV）
3. 下载到本地进行分析

### 过滤和搜索

- 在搜索框输入关键词（如题目、搜索查询、审校分数）
- 使用过滤器：按时间范围、项目、状态筛选

## 最佳实践

1. **为不同项目创建不同的 LangSmith 项目**：便于区分和查询
2. **定期清理旧追踪**：避免占用过多存储
3. **使用会话名称命名重要运行**：如 `run_2026-08-22_第一次测试`
4. **记录运行参数**：在会话名称或额外信息中记录题目、模型等参数

## 相关链接

- [LangSmith 官方文档](https://docs.smith.langchain.com/)
- [LangGraph 可观测性指南](https://langchain-ai.github.io/langgraph/concepts/persistence/#langsmith)
- [LangSmith 定价](https://smith.langchain.com/pricing)
