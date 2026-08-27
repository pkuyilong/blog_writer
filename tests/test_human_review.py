"""确定性验证"人工介入(Human-in-the-Loop)"节点 + checkpoint 断点续跑.

mock 所有 LLM(outliner.chat / writer.call_llm / review.call_llm / human_review.call_llm),
不耗 token.人工介入已从节点内同步 input() 升级为 LangGraph 正统 interrupt() + Command(resume).
覆盖:
  - 单测 human_review_node:confirm / revise(回环重写)/ replace / 带 feedback 进入
  - 端到端(MemorySaver):中断→回车确认→完成;revise 多轮;replace;全自动不中断;
    无 checkpointer 时 interrupt 可暂停但不可 resume(学习点)
  - 跨进程(tmp SqliteSaver):中断落盘 → 新进程 resume 跑完(对应 main.py --resume)
"""
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agents.outliner as O
import agents.writing as W
import agents.review as R
import agents.section_writer as SW
import agents.human_review as HR
import main
from graph import build_graph
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

passed = []


def check(name, cond, detail=""):
    passed.append((name, cond, detail))
    print(("PASS" if cond else "FAIL"), "-", name, detail)


# ---------- mock outliner:搜索+审查+提纲 ----------
def make_msg(content="", tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def make_tc(tc_id, query):
    return SimpleNamespace(
        id=tc_id,
        function=SimpleNamespace(name="web_search", arguments=json.dumps({"query": query})),
    )


def make_resp(msg):
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


OUTLINE = (
    "## 文章提纲\n一、引言：远程办公兴起\n二、历史与技术推动\n三、效率与平衡\n"
    "四、混合办公趋势\n五、挑战\n六、结语\n\n## 关键素材\n- 远程办公数据（麦肯锡）\n- 混合办公趋势（报告）"
)
REVIEW = "## 素材审查\n### 保留\n- 远程办公数据增长（来源：麦肯锡）\n- 混合办公 3+2 模式（来源：报告）"


def fake_outliner_chat(system, messages, tools=None, **kw):
    if tools:
        return make_resp(make_msg("", [make_tc("t1", "远程办公")]))
    if system == O.MATERIAL_REVIEW_PROMPT:
        return make_resp(make_msg(REVIEW))
    if system == O.OUTLINER_PROMPT:
        return make_resp(make_msg(OUTLINE))
    if system == O.FALLBACK_OUTLINE_PROMPT:
        return make_resp(make_msg("兜底:" + OUTLINE))
    raise AssertionError(f"未知 outliner system: {system[:30]}")


def fake_search(q):
    return f"结果:{q}"


O.chat = fake_outliner_chat
O.web_search = fake_search

# ---------- mock writer / review ----------
SECTIONS = {"sections": [
    {"title": "引言：远程办公兴起", "points": ["背景"], "materials": ["数据1"]},
    {"title": "效率与平衡", "points": ["效率"], "materials": ["数据2"]},
]}


def fake_writer_call_llm(system, user_content, json_mode=False, **kw):
    if system == W.SPLIT_PROMPT:
        return json.dumps(SECTIONS, ensure_ascii=False)
    # 写章节已下放到 section_writer 子图(SW.call_llm),writer 只负责拆章
    raise AssertionError(f"未知 writer system: {system[:30]}")


def fake_review_call_llm(system, user_content, json_mode=False, **kw):
    # 审核子智能体任意角色(edit_lang/edit_logic/edit_fact)都判通过
    return json.dumps({"score": 90, "passed": True, "failed_sections": []}, ensure_ascii=False)


W.call_llm = fake_writer_call_llm
R.call_llm = fake_review_call_llm

# ---------- mock section_writer.call_llm(write_section 现在是自包含子图) ----------
def fake_sw_call_llm(system, user_content, json_mode=False, **kw):
    if system in (SW.WRITE_SECTION_PROMPT, SW.SELF_REVIEW_PROMPT):
        title = [l for l in user_content.splitlines() if l.startswith("【本章节】")][0].split("标题：", 1)[1]
        # 返回足够长的正文(>=120 字,## 开头),保证通过启发式自检
        return f"## {title}\n\n（正文）" + "（内容扩充）" * 30
    raise AssertionError(f"未知 section_writer system: {system[:30]}")


SW.call_llm = fake_sw_call_llm

# ---------- mock human_review.call_llm(_revise_outline 用) ----------
revised_calls = {"n": 0}


def fake_revise(system, user_content, **kw):
    revised_calls["n"] += 1
    return "重写版：" + OUTLINE + "\n五、补充背景数据章节"


HR.call_llm = fake_revise


def initial_input(topic="远程办公"):
    return {"topic": topic, "sections": [], "section_drafts": {}, "failed_sections": [], "revision_count": 0}


# ===== 单测 human_review_node(patch HR.interrupt) =====
_real_interrupt = HR.interrupt


def _set_resume(payload):
    HR.interrupt = lambda value: payload  # 模拟 interrupt 在 resume 时返回客户端载荷


def _restore_interrupt():
    HR.interrupt = _real_interrupt


# 场景 A:首入无 feedback,resume confirm → 清空 feedback,保留 outline
_set_resume({"action": "confirm"})
ra = HR.human_review_node({"topic": "T", "outline": OUTLINE})
_restore_interrupt()
check("单测A: confirm 返回 outline 且清空 outline_review_feedback",
      ra["outline"] == OUTLINE and ra.get("outline_review_feedback") is None, f"ra={ra}")
check("单测A: 未触发 LLM 重写", revised_calls["n"] == 0, f"n={revised_calls['n']}")

# 场景 B:首入,resume revise → 写 feedback 等回环
_set_resume({"action": "revise", "feedback": "补充背景"})
rb = HR.human_review_node({"topic": "T", "outline": OUTLINE})
_restore_interrupt()
check("单测B: revise 返回 feedback、outline 未重写（回环后由下一轮节点开头重写）",
      rb.get("outline_review_feedback") == "补充背景" and rb["outline"] == OUTLINE, f"rb={rb}")

# 场景 C:首入,resume replace → 直接采用粘贴大纲
_set_resume({"action": "replace", "outline": "# 全新大纲\n一、A\n二、B"})
rc = HR.human_review_node({"topic": "T", "outline": OUTLINE})
_restore_interrupt()
check("单测C: replace 直接采用粘贴大纲、清空 feedback",
      rc["outline"] == "# 全新大纲\n一、A\n二、B" and rc.get("outline_review_feedback") is None, f"rc={rc}")

# 场景 D:带 feedback 进入(回环第二轮)→ 节点开头先调 _revise_outline 重写,再 interrupt 展示
n_before = revised_calls["n"]
_set_resume({"action": "confirm"})
rd = HR.human_review_node({"topic": "T", "outline": OUTLINE, "outline_review_feedback": "补充背景"})
_restore_interrupt()
check("单测D: 带 feedback 进入时先重写大纲（调 _revise_outline）", revised_calls["n"] == n_before + 1, f"n={revised_calls['n']}")
check("单测D: 展示的是重写后大纲，且确认后清空 feedback",
      "重写版" in rd["outline"] and rd.get("outline_review_feedback") is None, f"rd outline head={rd['outline'][:12]!r}")

# ===== 端到端(MemorySaver) =====
# E1:中断 → confirm → 完成
g = build_graph(enable_human_review=True, checkpointer=MemorySaver())
config = {"configurable": {"thread_id": "tid-1"}}
r1 = g.invoke(initial_input(), config)
check("E1: 第一次 invoke 触发中断", "__interrupt__" in r1, f"keys={list(r1.keys())}")
intr = r1["__interrupt__"][0].value
check("E1: 中断载荷是 outline_review 且含大纲", intr.get("type") == "outline_review" and "引言" in intr.get("outline", ""), "")
check("E1: 停在中断处 invoke(None) 重触发同一中断（resume 语义）", "__interrupt__" in g.invoke(None, config), "")
r2 = g.invoke(Command(resume={"action": "confirm"}), config)
check("E1: confirm 后跑完全程且通过", r2.get("passed") is True and bool(r2.get("final_article")), f"passed={r2.get('passed')}")
check("E1: 完成后无中断残留", "__interrupt__" not in r2, "")

# E2:revise 多轮(意见 → 重写 → 再确认)
g2 = build_graph(enable_human_review=True, checkpointer=MemorySaver())
config2 = {"configurable": {"thread_id": "tid-2"}}
s1 = g2.invoke(initial_input(), config2)
check("E2: 首次中断", "__interrupt__" in s1, "")
s2 = g2.invoke(Command(resume={"action": "revise", "feedback": "补充背景数据"}), config2)
check("E2: revise 后再次中断（回环重写再展示）", "__interrupt__" in s2, f"keys={list(s2.keys())}")
check("E2: 第二次中断展示的是重写后大纲", "重写版" in s2["__interrupt__"][0].value.get("outline", ""), "")
s3 = g2.invoke(Command(resume={"action": "confirm"}), config2)
check("E2: 二次确认后跑完全程", s3.get("passed") is True and bool(s3.get("final_article")), f"passed={s3.get('passed')}")

# E3:replace(粘贴完整新大纲)
g3 = build_graph(enable_human_review=True, checkpointer=MemorySaver())
config3 = {"configurable": {"thread_id": "tid-3"}}
t1 = g3.invoke(initial_input(), config3)
t2 = g3.invoke(Command(resume={"action": "replace", "outline": "# 用户粘贴的全新大纲\n一、A\n二、B"}), config3)
check("E3: replace 直接采用，不二次中断", "__interrupt__" not in t2 and t2.get("passed") is True, f"keys={list(t2.keys())}")

# E4:全自动(enable_human_review=False)不中断
g4 = build_graph(enable_human_review=False, checkpointer=MemorySaver())
config4 = {"configurable": {"thread_id": "tid-4"}}
a1 = g4.invoke(initial_input(), config4)
check("E4: 全自动一次跑完、无中断", "__interrupt__" not in a1 and a1.get("passed") is True, f"keys={list(a1.keys())}")

# E5:无 checkpointer 时 interrupt 仍返回 __interrupt__(可暂停,但状态不持久化,不可 resume)
g5 = build_graph(enable_human_review=True)  # 无 checkpointer
r5 = g5.invoke(initial_input())
check("E5: 无 checkpointer 时 interrupt 仍返回 __interrupt__（可暂停，但不可 resume）",
      "__interrupt__" in r5, f"keys={list(r5.keys())}")

# ===== 跨进程断点续跑(tmp SqliteSaver,对应 main.py --resume) =====
DB = "/tmp/blog_writer_test_hr.db"
THREAD = "cross-tid"
if os.path.exists(DB):
    os.remove(DB)
with SqliteSaver.from_conn_string(DB) as cp:
    gc = build_graph(enable_human_review=True, checkpointer=cp)
    cc = {"configurable": {"thread_id": THREAD}}
    c1 = gc.invoke(initial_input(), cc)
    check("跨进程: 进程1 中断", "__interrupt__" in c1, "")
# 退出 with(连接关闭)→ 进程级"中断后退出"
with SqliteSaver.from_conn_string(DB) as cp:
    gc = build_graph(enable_human_review=True, checkpointer=cp)
    cc = {"configurable": {"thread_id": THREAD}}
    snap = gc.get_state(cc)
    check("跨进程: 新进程 get_state 看到待执行节点（next 非空）", bool(snap.next), f"next={snap.next}")
    c2 = gc.invoke(Command(resume={"action": "confirm"}), cc)
    check("跨进程: 从 checkpoint 续跑完成", c2.get("passed") is True and bool(c2.get("final_article")), f"passed={c2.get('passed')}")
if os.path.exists(DB):
    os.remove(DB)

# ===== 回归守护:--in-memory 本次恢复,并新增与 --resume 互斥保护 =====
# parse_args() 读 sys.argv(无 argv 参数),这里临时替换再恢复
_old_argv = sys.argv
sys.argv = ["main.py", "--in-memory"]
try:
    args_m = main.parse_args()
finally:
    sys.argv = _old_argv
check("main: --in-memory 解析正常", args_m.in_memory is True and args_m.resume is None,
      f"in_memory={args_m.in_memory} resume={args_m.resume}")

print()
failed = [p for p in passed if not p[1]]
print(f"共 {len(passed)} 项检查，通过 {len(passed) - len(failed)} 项")
sys.exit(1 if failed else 0)
