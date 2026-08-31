"""确定性验证长期记忆(memory_store.py):偏好注入 / remember 写回 / 持久化.

mock 全部 LLM(同 test_section_writer 的 fake 套件), 用临时 SqliteStore 跑完整主图,
不耗 token.覆盖:
  1. store=None 行为不变: 完整链路跑通, SW 首写 user_content 不含【写作偏好】, remember 不写
  2. 偏好注入: save_prefs 后 SW 首写 user_content 与 O.chat 的 OUTLINER 消息都带【写作偏好】
  3. remember 写回: invoke 后 store.get(("topics",), topic) 返回含 quality_score/passed/
     revision_count/draft_tail 的 Item; 再跑一次 OUTLINER 消息带【历史写作记录】
  4. parse_prefs_arg 解析(中英文冒号/逗号混用、空段跳过、空串)
  5. save_prefs 覆盖(第二次生效)
  6. open_store 持久化(同一文件两次打开, 第二次读回第一次条目)
  7. load_topic_history(有记录返回含"质量分", 无记录返回 None)
"""
import json
import os
import shutil
import sys
import tempfile
import threading
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agents.outliner as O
import agents.section_writer as SW
import agents.writing as W
import agents.review as R
import memory_store
from graph import build_graph

passed = []


def check(name, cond, detail=""):
    passed.append((name, cond, detail))
    print(("PASS" if cond else "FAIL"), "-", name, detail)


# ---------- fake 套件(同 test_section_writer) ----------
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
    "## 文章提纲\n"
    "一、引言：为什么远程办公越来越普遍\n"
    "二、远程办公的历史与技术推动\n"
    "三、效率与工作生活平衡\n"
    "四、混合办公的新趋势\n"
    "五、面临的挑战\n"
    "六、结语\n\n"
    "## 关键素材\n"
    "- 远程办公兴起与数据（来源：麦肯锡）\n"
    "- 混合办公成为主流（来源：报告）"
)
REVIEW = (
    "## 素材审查结果\n### 保留\n"
    "- 远程办公数据增长（来源：麦肯锡）\n- 混合办公 3+2 模式（来源：报告）"
)
TOPIC = "远程办公"

outliner_calls = {"chat": 0, "outline_user_contents": []}


def fake_outliner_chat(system, messages, tools=None, **kw):
    outliner_calls["chat"] += 1
    if tools:
        return make_resp(make_msg("", [make_tc("t1", "远程办公 数据")]))
    if system == O.MATERIAL_REVIEW_PROMPT:
        return make_resp(make_msg(REVIEW))
    if system == O.OUTLINER_PROMPT:
        outliner_calls["outline_user_contents"].append(messages[0]["content"])
        return make_resp(make_msg(OUTLINE))
    if system == O.FALLBACK_OUTLINE_PROMPT:
        return make_resp(make_msg("兜底:" + OUTLINE))
    raise AssertionError(f"未知 outliner system: {system[:30]}")


def fake_search(q):
    return f"结果:{q}"


O.chat = fake_outliner_chat
# v2.3 起 outliner 走 cached_search(CLAUDE.md 决策 #15):mock 掉缓存接口,防连真库/真网络
O.cached_search = fake_search
O.get_cached_materials = lambda topic: None  # topic 级缓存恒 miss
O.store_materials = lambda topic, materials: None  # 不写真库

SECTIONS = {
    "sections": [
        {"title": "引言：远程办公为何兴起", "points": [], "materials": ["数据1"]},
        {"title": "效率与平衡", "points": [], "materials": ["数据2"]},
        {"title": "混合办公趋势", "points": [], "materials": ["数据3"]},
    ]
}

split_calls = {"count": 0}


def fake_writer_call_llm(system, user_content, json_mode=False, **kw):
    if system == W.SPLIT_PROMPT:
        split_calls["count"] += 1
        return json.dumps(SECTIONS, ensure_ascii=False)
    raise AssertionError(f"未知 writer system: {system[:30]}")


W.call_llm = fake_writer_call_llm

section_lock = threading.Lock()
sw_write_contents = []


def fake_sw_call_llm(system, user_content, json_mode=False, **kw):
    if system == SW.WRITE_SECTION_PROMPT:
        with section_lock:
            sw_write_contents.append(user_content)
        title = [l for l in user_content.splitlines() if l.startswith("【本章节】")][0]
        title = title.split("标题：", 1)[1]
        # 足够长 + ## 开头,保证首写即通过启发式自检(points 为空,不做要点覆盖)
        return f"## {title}\n\n（{title} 正文）" + "（内容扩充）" * 30
    if system == SW.SELF_REVIEW_PROMPT:
        raise AssertionError("默认场景各章首写即合格，不应触发章节内部重写")
    raise AssertionError(f"未知 section_writer system: {system[:30]}")


SW.call_llm = fake_sw_call_llm


def fake_review_call_llm(system, user_content, json_mode=False, **kw):
    return json.dumps({"score": 90, "passed": True, "failed_sections": []}, ensure_ascii=False)


R.call_llm = fake_review_call_llm


def _reset_captures():
    outliner_calls["outline_user_contents"] = []
    sw_write_contents.clear()


def _invoke(store=None):
    """跑完整主图(可选带 store),返回 result."""
    g = build_graph(store=store)
    return g.invoke({"topic": TOPIC})


# ===== 1. store=None 行为不变 =====
_reset_captures()
result = _invoke(store=None)
check("store=None: 完整链路跑通(passed + final_article 非空)",
      result["passed"] is True and bool(result["final_article"]), f"passed={result.get('passed')}")
check("store=None: SW 首写 user_content 不含【写作偏好】",
      all("【写作偏好（来自长期记忆）】" not in c for c in sw_write_contents), "")
# remember 无 store 可写:store=None 时节点空转(图仍能正常 END)
check("store=None: 无 store 可查, remember 间接证明不写", True, "无 store, 无从查起(节点返回 {})")

# ===== 2. 偏好注入 + remember 写回(同一个临时库) =====
tmp_dir = tempfile.mkdtemp(prefix="bw_memory_")
try:
    db = os.path.join(tmp_dir, "memory.db")
    _reset_captures()
    with memory_store.open_store(db) as store:
        memory_store.save_prefs(store, {"风格": "轻松口语"})
        r2 = _invoke(store=store)
        check("偏好注入: 完整链路跑通", r2["passed"] is True, f"passed={r2.get('passed')}")
        check("偏好注入: SW 首写 user_content 含【写作偏好】+ 风格",
              "【写作偏好（来自长期记忆）】风格：轻松口语" in sw_write_contents[0],
              f"head={sw_write_contents[0][-40:]!r}")
        check("偏好注入: O.chat OUTLINER 消息含同样段落",
              any("【写作偏好（来自长期记忆）】风格：轻松口语" in c
                  for c in outliner_calls["outline_user_contents"]), "")

        # remember 写回
        item = store.get(("topics",), TOPIC)
        check("remember: store 有该 topic 条目", item is not None, "")
        if item is not None:
            v = item.value
            check("remember: value 含 quality_score=90", v.get("quality_score") == 90, f"v={v}")
            check("remember: value 含 passed=True", v.get("passed") is True, f"v={v}")
            check("remember: value 含 revision_count=1", v.get("revision_count") == 1, f"v={v}")
            check("remember: value 含 draft_tail(非空字符串)",
                  isinstance(v.get("draft_tail"), str) and bool(v["draft_tail"]), "")

        # 历史写作记录:第二次 invoke 时 OUTLINER 消息带【历史写作记录】
        _reset_captures()
        r3 = _invoke(store=store)
        check("历史记录: 第二次 invoke 完整跑通", r3["passed"] is True, f"passed={r3.get('passed')}")
        check("历史记录: OUTLINER 消息含【历史写作记录】与质量分",
              any("【历史写作记录】" in c and "质量分" in c for c in outliner_calls["outline_user_contents"]),
              "")

        # load_topic_history 单测
        hist = memory_store.load_topic_history(store, TOPIC)
        check("load_topic_history: 有记录返回含'质量分'", hist is not None and "质量分" in hist, f"hist={hist!r}")
        check("load_topic_history: 无记录返回 None",
              memory_store.load_topic_history(store, "不存在的题目") is None, "")
        check("load_topic_history: store=None 返回 None",
              memory_store.load_topic_history(None, TOPIC) is None, "")

    # ===== 3. parse_prefs_arg =====
    p1 = memory_store.parse_prefs_arg("风格:轻松口语,篇幅：3000字")
    check("parse: 中英文冒号/逗号混用", p1 == {"风格": "轻松口语", "篇幅": "3000字"}, f"p1={p1}")
    check("parse: 空串 → {}", memory_store.parse_prefs_arg("") == {}, "")
    p2 = memory_store.parse_prefs_arg("风格:轻松口语,无冒号段,篇幅:2000")
    check("parse: 无冒号段被跳过", p2 == {"风格": "轻松口语", "篇幅": "2000"}, f"p2={p2}")

    # ===== 4. save_prefs 覆盖 =====
    db2 = os.path.join(tmp_dir, "override.db")
    with memory_store.open_store(db2) as store:
        memory_store.save_prefs(store, {"风格": "A", "篇幅": "1000"})
        memory_store.save_prefs(store, {"风格": "B"})
        item = store.get(("prefs",), "default")
        check("save覆盖: 第二次写入生效", item is not None and item.value == {"风格": "B"},
              f"value={item and item.value}")

    # ===== 5. open_store 持久化(同一文件两次打开) =====
    persist_db = os.path.join(tmp_dir, "persist.db")
    with memory_store.open_store(persist_db) as store:
        store.put(("prefs",), "default", {"风格": "持久"})
    with memory_store.open_store(persist_db) as store2:
        item2 = store2.get(("prefs",), "default")
        check("持久化: 第二次打开读回第一次写入条目",
              item2 is not None and item2.value == {"风格": "持久"}, f"value={item2 and item2.value}")
finally:
    shutil.rmtree(tmp_dir, ignore_errors=True)

print()
failed = [p for p in passed if not p[1]]
print(f"共 {len(passed)} 项检查，通过 {len(passed) - len(failed)} 项")
sys.exit(1 if failed else 0)
