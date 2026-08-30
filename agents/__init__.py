"""blog_writer 的 Agent 子智能体模块.

- outliner:大纲子智能体(搜索素材 → 审查 → 生成提纲 → 自检 → 补搜/重试/兜底)
- writing:写作子 Agent(拆章 → Send 并行写章 → 合并)
- section_writer:章节写作子智能体(写单章 → 自检 → 条件重写)
- review:审核子智能体(3 角色并行打分 + 多数表决)
- human_review:人工介入节点(interrupt/Command 协议)
- tools:联网搜索工具(web_search)
"""
