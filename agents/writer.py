from llm import call_llm
from prompts import WRITER_PROMPT
from state import ArticleState


def writer_node(state: ArticleState) -> dict:
    """写作节点：根据提纲写出完整中文文章，写入 state["draft"]。

    若被打回重写（存在上轮审校意见），则把修改意见一并交给模型。
    """
    feedback = state.get("revision_feedback", "")
    if feedback and feedback != "无需修改":
        print("→ 写作中（结合审校意见修改）…")
        user_content = (
            f"请根据以下提纲与素材，写一篇完整的中文文章：\n\n{state['outline']}\n\n"
            f"【上轮审校意见】{feedback}\n"
            "请逐条针对上述意见修改，并输出修改后的完整文章。"
        )
    else:
        print("→ 写作中…")
        user_content = f"请根据以下提纲与素材，写一篇完整的中文文章：\n\n{state['outline']}"

    draft = call_llm(WRITER_PROMPT, user_content)
    return {"draft": draft}
