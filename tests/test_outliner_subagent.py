"""确定性验证"自包含大纲子智能体"控制流(mock chat / web_search,不耗 token).

子图内部 = 搜索素材 → 审查 → 生成提纲 → 自检,
覆盖:素材够收敛 / 素材不足补搜 / 补搜到上限兜底 / 提纲重试 / 私有键不泄漏 / 主图节点链.
"""
import json
import os
import sys
import threading
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agents.outliner as O
from graph import build_graph

# 审查结果:素材够用(长度足,无失败特征词)
MATERIALS = (
    "## 素材审查结果\n### 保留\n- 多智能体系统由多个自主智能体组成，通过通信协作完成单智能体难以完成的任务（来源：维基百科）"
    "\n- 典型应用：自动驾驶车队协同、多机器人编队（来源：论文）"
)
# 审查结果:素材不足(太短)
SHORT_REVIEW = "## 素材审查结果\n### 保留\n（无）"
# 搜索失败提示
SEARCH_FAIL = "搜索没有返回结果，请换个关键词或基于自身知识作答。"
# 可用提纲(远超 60 字)
GOOD_OUTLINE = (
    "## 文章提纲\n"
    "一、引言：为什么多智能体越来越受关注\n"
    "二、多智能体系统的定义与基本概念\n"
    "三、多智能体与单体智能体的区别\n"
    "四、核心原理：通信、协作与博弈\n"
    "五、应用场景：自动驾驶、机器人、金融\n"
    "六、面临的挑战与未来展望\n"
    "七、结语\n\n"
    "## 关键素材\n"
    "- 多智能体系统由多个自主智能体组成（来源：维基百科）\n"
    "- 典型应用：自动驾驶车队协同（来源：论文）"
)

passed = []


def check(name, cond, detail=""):
    passed.append((name, cond, detail))
    print(("PASS" if cond else "FAIL"), "-", name, detail)


def make_msg(content="", tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def make_tc(tc_id, query):
    return SimpleNamespace(
        id=tc_id,
        function=SimpleNamespace(name="web_search", arguments=json.dumps({"query": query})),
    )


def make_resp(msg):
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def setup(cfg):
    """配置化 mock.cfg: {review_texts: [每轮审查结果], outline_seq: [每次生成结果], fallback_outline, search_result, n_queries: 每轮查询数}"""
    calls = {"chat": 0, "search": 0, "chat_messages": []}
    search_lock = threading.Lock()  # 并行搜索时计数需加锁
    review_q = list(cfg["review_texts"])
    outline_q = list(cfg.get("outline_seq", [GOOD_OUTLINE]))
    fallback = cfg.get("fallback_outline", "兜底:" + GOOD_OUTLINE)
    n_queries = cfg.get("n_queries", 1)

    def fake_chat(system, messages, tools=None, **kw):
        calls["chat"] += 1
        calls["chat_messages"].append(messages)
        if tools:  # 搜索阶段:一次返回 n_queries 个查询
            return make_resp(
                make_msg("", [make_tc(f"t{calls['chat']}_{i}", f"查询{i}") for i in range(n_queries)])
            )
        if system == O.MATERIAL_REVIEW_PROMPT:
            return make_resp(make_msg(review_q.pop(0) if review_q else ""))
        if system == O.OUTLINER_PROMPT:
            return make_resp(make_msg(outline_q.pop(0) if outline_q else "短"))
        if system == O.FALLBACK_OUTLINE_PROMPT:
            return make_resp(make_msg(fallback))
        raise AssertionError(f"未知 system: {system[:30]}")

    def fake_search(q):
        with search_lock:
            calls["search"] += 1
        return cfg.get("search_result", f"结果:{q}")

    # v2.3 起 _run_search 走 cached_search(query 级缓存, 见 search_cache.py):
    # 测试 mock 到 O.cached_search; 同时 mock 掉 topic 级缓存的读写(否则连真库/走缓存)
    O.chat = fake_chat
    O.cached_search = fake_search
    O.get_cached_materials = lambda t: None  # 不命中 topic 缓存, 走完整搜索流程
    O.store_materials = lambda t, m: None  # 不写真库
    return calls


# ---- 场景 A:素材够 → 1 轮搜索 → 提纲合格 → 收敛 ----
calls = setup({"review_texts": [MATERIALS], "outline_seq": [GOOD_OUTLINE]})
out = O.build_outliner().invoke({"topic": "多智能体"})
check("A: 素材够，1 轮搜索 + 1 次生成即收敛", calls["chat"] == 3, f"chat={calls['chat']}")
check("A: 搜索执行 1 次（mock 每轮 1 个查询）", calls["search"] == 1, f"search={calls['search']}")
check("A: 返回可用大纲", O._usable(out["outline"]))
print()

# ---- 场景 B:素材不足 → 补搜(第 2 轮素材够)→ 收敛 ----
calls = setup({"review_texts": [SHORT_REVIEW, MATERIALS], "outline_seq": [GOOD_OUTLINE]})
out = O.build_outliner().invoke({"topic": "多智能体"})
check("B: 素材不足触发补搜（2 轮搜索）", calls["chat"] == 5, f"chat={calls['chat']}")
check("B: 两轮共搜索 2 次（mock 每轮 1 个查询）", calls["search"] == 2, f"search={calls['search']}")
check("B: 返回可用大纲", O._usable(out["outline"]))
# 补搜上下文:补搜轮(第 3 次 chat)的 user 消息应带第一轮查询记录,首轮不带
b_msgs = calls["chat_messages"]
check("B: 补搜轮 user 消息带第一轮查询记录",
      "【上一轮搜索记录】" in b_msgs[2][0]["content"] and "- 查询0" in b_msgs[2][0]["content"],
      f"content={b_msgs[2][0]['content'][:80]!r}")
check("B: 首轮 user 消息不带补搜历史上下文", "【上一轮搜索记录】" not in b_msgs[0][0]["content"])
print()

# ---- 场景 C:两轮素材都不足(搜索失败)→ 自身知识兜底 ----
calls = setup({"review_texts": [SHORT_REVIEW, SHORT_REVIEW], "search_result": SEARCH_FAIL})
out = O.build_outliner().invoke({"topic": "多智能体"})
check("C: 补搜到上限后走兜底", calls["chat"] == 5, f"chat={calls['chat']}")
check("C: 兜底产出可用大纲", O._usable(out["outline"]))
print()

# ---- 场景 D:素材够但提纲两次都短 → 重试超限 → 兜底 ----
calls = setup({"review_texts": [MATERIALS], "outline_seq": ["短", "短"]})
out = O.build_outliner().invoke({"topic": "多智能体"})
check("D: 提纲两次不合格后走兜底", calls["chat"] == 5, f"chat={calls['chat']}")
check("D: 兜底产出可用大纲", O._usable(out["outline"]))
print()

# ---- 场景 E:素材够,提纲第一次短第二次长 → 重试收敛 ----
calls = setup({"review_texts": [MATERIALS], "outline_seq": ["短", GOOD_OUTLINE]})
out = O.build_outliner().invoke({"topic": "多智能体"})
check("E: 提纲首次过短触发重试后收敛", calls["chat"] == 4, f"chat={calls['chat']}")
check("E: 返回可用大纲", O._usable(out["outline"]))
print()

# ---- 场景 F:嵌入父图,私有键不泄漏回父图 ----
from typing import TypedDict
from langgraph.graph import StateGraph, START, END

class Parent(TypedDict):
    topic: str
    outline: str

calls = setup({"review_texts": [MATERIALS], "outline_seq": [GOOD_OUTLINE]})
parent = StateGraph(Parent)
parent.add_node("outline", O.build_outliner())
parent.add_edge(START, "outline")
parent.add_edge("outline", END)
g = parent.compile()
out3 = g.invoke({"topic": "多智能体", "outline": ""})
check("F: 父图拿到可用 outline", O._usable(out3.get("outline", "")))
check("F: 私有键(search_round/outline_attempt/materials/search_history)不泄漏",
      not any(k in out3 for k in ("search_round", "outline_attempt", "materials", "search_history")),
      f"keys={sorted(out3)}")
print()

# ---- 场景 G:主图节点链 ----
gr = build_graph().get_graph()
edges = [(e.source, e.target) for e in gr.edges]
nodes = set(gr.nodes)
check("G: 主图无 research 节点", "research" not in nodes, f"nodes={sorted(nodes)}")
# 注意:get_graph() 静态渲染不展开 Send 条件边,且 split/fan_out/merge 已封装进
# writing 子图内部(主图只暴露 outline/writing/edit);因此只断言节点集 + 可靠的静态
# 直连边;运行时全链路由 test_section_writer.py 的 invoke 验证
check("G: 主图节点链完整", {"outline", "writing", "edit"} <= nodes,
      f"nodes={sorted(nodes)}")
check("G: 主图不再有 split/write_section/merge 节点",
      not ({"split", "write_section", "merge"} & nodes), f"nodes={sorted(nodes)}")
check("G: 存在 START→outline 边", ("__start__", "outline") in edges, f"edges={edges}")
check("G: 存在 outline→writing 边", ("outline", "writing") in edges)
from langgraph.graph.state import CompiledStateGraph
check("G: outline 节点是编译子图", isinstance(gr.nodes["outline"].data, CompiledStateGraph),
      f"type={type(gr.nodes['outline'].data).__name__}")
check("G: writing 节点是编译子图", isinstance(gr.nodes["writing"].data, CompiledStateGraph),
      f"type={type(gr.nodes['writing'].data).__name__}")
print()

# ---- 场景 H:单轮 3 个查询并行搜索 → 收敛 ----
calls = setup({"review_texts": [MATERIALS], "outline_seq": [GOOD_OUTLINE], "n_queries": 3})
out = O.build_outliner().invoke({"topic": "多智能体"})
check("H: 3 个查询并行执行（search=3）", calls["search"] == 3, f"search={calls['search']}")
check("H: 并行后 chat 仍按 搜索/审查/生成 3 次收敛", calls["chat"] == 3, f"chat={calls['chat']}")
check("H: 并行搜索后返回可用大纲", O._usable(out["outline"]))
check("H: 并行搜索结果落入 materials", O._materials_ok(out.get("materials", "")), f"len={len(out.get('materials', ''))}")
print()

failed = [p for p in passed if not p[1]]
print(f"共 {len(passed)} 项检查，通过 {len(passed) - len(failed)} 项")
sys.exit(1 if failed else 0)
