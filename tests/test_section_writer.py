"""确定性验证"按章节并发写作"(Send map-reduce 到 section_writer 子图)+ 只重写问题章节.

mock 所有 LLM(outliner.chat / writer.call_llm / section_writer.call_llm / review.call_llm),
不耗 token.覆盖:outline 产出 → split 拆章 → 并行写 N 章(每章是自包含子图,首写即合格,
省掉旧版无条件反思轮)→ merge → 审核子智能体首轮不过(三角色并行,多数表决 1 过 2 不过)
→ 打回只重写问题章节(带合并后的角色意见)→ 重写后再审全过通过.
额外覆盖 split 输出校验失败:输出不合法 → 带具体字段错误反馈重试 → 耗尽量回退单章节.
"""
import json
import os
import sys
import threading
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agents.outliner as O
import agents.section_writer as SW
import agents.writing as W
import agents.review as R
import prompts
from graph import build_graph

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

outliner_calls = {"chat": 0, "search": 0}


def fake_outliner_chat(system, messages, tools=None, **kw):
    outliner_calls["chat"] += 1
    if tools:
        return make_resp(make_msg("", [make_tc("t1", "远程办公 数据")]))
    if system == O.MATERIAL_REVIEW_PROMPT:
        return make_resp(make_msg(REVIEW))
    if system == O.OUTLINER_PROMPT:
        return make_resp(make_msg(OUTLINE))
    if system == O.FALLBACK_OUTLINE_PROMPT:
        return make_resp(make_msg("兜底:" + OUTLINE))
    raise AssertionError(f"未知 outliner system: {system[:30]}")


def fake_search(q):
    outliner_calls["search"] += 1
    return f"结果:{q}"


O.chat = fake_outliner_chat
# v2.3 起 outliner 走 cached_search(CLAUDE.md 决策 #15):mock 掉缓存接口,防连真库/真网络
O.cached_search = fake_search
O.get_cached_materials = lambda topic: None  # topic 级缓存恒 miss
O.store_materials = lambda topic, materials: None  # 不写真库

# ---------- mock writer.call_llm(只负责 split;写章节已下放到 section_writer) ----------
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
    # 若在 writer 里再遇到写作/反思提示,说明下放不彻底,直接暴露
    raise AssertionError(f"未知 writer system: {system[:30]}")


W.call_llm = fake_writer_call_llm

# ---------- mock section_writer.call_llm(每章按标题维护版本号,不依赖并行执行顺序) ----------
section_lock = threading.Lock()
sw_calls = {"write": 0, "self_review": 0}
section_versions = {}  # title -> 已写版本号(第 1 次=首写,第 2 次=打回重写)
sw_write_contents = []  # 每次 write 的 user_content,用于断言审校意见传递


def fake_sw_call_llm(system, user_content, json_mode=False, **kw):
    if system == SW.WRITE_SECTION_PROMPT:
        with section_lock:
            sw_calls["write"] += 1
        title = [l for l in user_content.splitlines() if l.startswith("【本章节】")][0]
        title = title.split("标题：", 1)[1]
        with section_lock:
            v = section_versions.get(title, 0) + 1
            section_versions[title] = v
            sw_write_contents.append(user_content)
        # 足够长 + ## 开头,保证首写即通过启发式自检(points 为空,不做要点覆盖)
        return f"## {title}\n\n（{title} 正文 v{v}）" + "（内容扩充）" * 30
    if system == SW.SELF_REVIEW_PROMPT:
        with section_lock:
            sw_calls["self_review"] += 1
        raise AssertionError("默认场景各章首写即合格，不应触发章节内部重写")
    raise AssertionError(f"未知 section_writer system: {system[:30]}")


SW.call_llm = fake_sw_call_llm

# ---------- mock review.call_llm:每轮三角色并行;第 1 轮多数不过,第 2 轮全过 ----------
# 审核子智能体每轮审校 = 3 次 call_llm(edit_lang/edit_logic/edit_fact,并行顺序不保证).
# 按累计调用次数换算轮次:n // 3,第 1-3 次=第 1 轮,第 4-6 次=第 2 轮.
review_calls = {"n": 0}


def fake_review_call_llm(system, user_content, json_mode=False, **kw):
    role = kw.get("role", "edit_lang")
    n = review_calls["n"]
    review_calls["n"] += 1
    round_n = n // 3
    if round_n == 0:
        # 第 1 轮:语言编辑判过,逻辑/事实判不过并标记章节 1 → 显式通过票 1 < 2,多数不过
        if role == "edit_lang":
            return json.dumps({"score": 88, "passed": True, "failed_sections": []}, ensure_ascii=False)
        return json.dumps(
            {
                "score": 50,
                "passed": False,
                "failed_sections": [{"id": 1, "feedback": "第2章太空泛，需补充具体数据"}],
            },
            ensure_ascii=False,
        )
    # 第 2 轮:三角色全过 → 显式通过票 3 >= 2,通过
    return json.dumps({"score": 90, "passed": True, "failed_sections": []}, ensure_ascii=False)


R.call_llm = fake_review_call_llm

# ---------- 跑完整主图 ----------
g = build_graph()
result = g.invoke({"topic": "远程办公"})

check("主图: 审校共 6 次（2 轮 × 三角色并行）", review_calls["n"] == 6, f"review_calls={review_calls['n']}")
check("主图: split 只调 1 次（打回复用已拆章节）", split_calls["count"] == 1,
      f"split_calls={split_calls['count']}")
check("主图: write_section 子图共写 4 次（首轮 3 章 + 打回只重写 1 章）",
      sw_calls["write"] == 4, f"sw_write={sw_calls['write']}")
check("主图: 各章首写即合格，内部 SELF_REVIEW 0 次（省掉旧版无条件反思 4 次）",
      sw_calls["self_review"] == 0, f"sw_self_review={sw_calls['self_review']}")
check("主图: 打回后通过、质量分正常", result["passed"] and result["quality_score"] == 90,
      f"passed={result['passed']} score={result['quality_score']}")
check("主图: final_article 非空且含 Markdown 标题", bool(result["final_article"].strip())
      and result["final_article"].startswith("## "), f"head={result['final_article'][:40]!r}")
check("主图: 最终稿包含 3 个章节", result["final_article"].count("## ") == 3,
      f"chapters={result['final_article'].count('## ')}")

draft = result["final_article"]
check("打回: 问题章节(效率与平衡)是重写版 v2", "（效率与平衡 正文 v2）" in draft,
      "含 v2" if "（效率与平衡 正文 v2）" in draft else f"draft={draft!r}")
check("打回: 未出问题章节保留首写版 v1", "（引言：远程办公为何兴起 正文 v1）" in draft
      and "（混合办公趋势 正文 v1）" in draft, "含两章 v1" if "（引言：远程办公为何兴起 正文 v1）" in draft
      and "（混合办公趋势 正文 v1）" in draft else "")
check("打回: 问题章节旧版(v1)已被覆盖", "（效率与平衡 正文 v1）" not in draft,
      "仍含 v1" if "（效率与平衡 正文 v1）" in draft else "")
check("打回: 重写轮(第 4 次写)收到合并后的角色意见", "【上轮审校意见】" in sw_write_contents[3]
      and "第2章太空泛" in sw_write_contents[3],
      "含意见" if len(sw_write_contents) > 3 and "【上轮审校意见】" in sw_write_contents[3] else "")

# 子图私有键不泄漏回父图 state
for key in ("write_attempt", "section_text", "self_check_notes", "section", "feedback"):
    check(f"父图: 子图私有键 {key} 未泄漏", key not in result, f"{key} 泄漏" if key in result else "")

# 回归守护:SPLIT_PROMPT 必须含小写 "json"(DeepSeek json_object 模式要求 prompt 里
# 出现小写 json 才生效;原版只有大写 JSON,本次修复补上,防回归)
check("SPLIT_PROMPT 含小写 json（DeepSeek json_object 模式要求）",
      "json" in prompts.SPLIT_PROMPT, f"prompts.SPLIT_PROMPT 无小写 json")

# ===== split 输出校验失败 → 带具体字段错误反馈重试 → 耗尽量回退单章节 =====
OUTLINE_INPUT = {"topic": "远程办公", "outline": OUTLINE, "revision_count": 0}

# 场景 A: 第一次输出不合法(非法 JSON) → 反馈重试 → 第二次合法 → 成功
split_retry = {"n": 0, "user_contents": []}
def fake_split_retry(system, user_content, json_mode=False, **kw):
    if system == W.SPLIT_PROMPT:
        split_retry["n"] += 1
        split_retry["user_contents"].append(user_content)
        return "not json" if split_retry["n"] == 1 else json.dumps(SECTIONS, ensure_ascii=False)
    raise AssertionError(f"未知 writer system: {system[:30]}")
W.call_llm = fake_split_retry
out = W.split_sections(OUTLINE_INPUT)
check("split重试: 不合法→重试成功, 调 2 次", split_retry["n"] == 2, f"n={split_retry['n']}")
check("split重试: 第二次 user_content 含具体校验反馈",
      "没有通过 json 结构校验" in split_retry["user_contents"][1], "")
check("split重试: 成功后 3 章节并补 id",
      len(out["sections"]) == 3 and out["sections"][0]["id"] == 0 and "title" in out["sections"][0],
      f"n={len(out['sections'])}")

# 场景 B: 两次都输出不合法 → 耗尽回退单章节(原兜底保留)
split_bad = {"n": 0}
def fake_split_bad(system, user_content, json_mode=False, **kw):
    if system == W.SPLIT_PROMPT:
        split_bad["n"] += 1
        return "not json"
    raise AssertionError(f"未知 writer system: {system[:30]}")
W.call_llm = fake_split_bad
out = W.split_sections(OUTLINE_INPUT)
check("split回退: 两次失败后回退单章节", split_bad["n"] == 2 and len(out["sections"]) == 1,
      f"n={split_bad['n']} sections={len(out['sections'])}")
check("split回退: 单章节 title==topic 且 id==0",
      out["sections"][0]["title"] == "远程办公" and out["sections"][0]["id"] == 0, "")

print()
failed = [p for p in passed if not p[1]]
print(f"共 {len(passed)} 项检查，通过 {len(passed) - len(failed)} 项")
sys.exit(1 if failed else 0)
