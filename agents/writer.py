from llm import call_llm
from prompts import WRITER_PROMPT
from state import ArticleState


def writer_node(state: ArticleState) -> dict:
    """写作节点：根据提纲写出完整中文文章，写入 state["draft"]。"""
    print("→ 写作中…")
    user_content = f"请根据以下提纲与素材，写一篇完整的中文文章：\n\n{state['outline']}"
    draft = call_llm(WRITER_PROMPT, user_content)
    return {"draft": draft}
