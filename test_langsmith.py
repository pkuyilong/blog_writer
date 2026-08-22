"""快速测试 LangSmith 集成是否正常工作。"""
import os

# 检查环境变量
tracing_enabled = os.getenv("LANGCHAIN_TRACING_V2", "false").lower() == "true"
api_key = os.getenv("LANGCHAIN_API_KEY")

print("=== LangSmith 集成检查 ===")
print(f"追踪启用：{tracing_enabled}")
print(f"API Key 已设置：{api_key is not None}")

if tracing_enabled and api_key:
    print("✓ LangSmith 追踪已启用，可以正常使用")
else:
    print("⚠ LangSmith 未启用，请设置：")
    print("  export LANGCHAIN_TRACING_V2=true")
    print("  export LANGCHAIN_API_KEY=你的API密钥")
    print("\n详细文档请查看：LANGSMITH.md")
