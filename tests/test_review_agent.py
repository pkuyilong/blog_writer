"""确定性验证"审核子智能体"(agents/review.py):Send×3 并行角色审校 + 多数表决聚合.

mock R.call_llm(fake 按 kw["role"] 区分角色),不耗 token.覆盖:
  R1 三角色各调一次全通过 / R2 多数不过(合并失败章节,角色前缀)
  R3 多数通过(2 过 1 不过) / R4 分数平均(含四舍五入)
  R5 单角色解析失败重试(第二次带"不是合法 json"提示)
  R7 弃权不拉偏(a 2 过+1 弃权 / b 1 过+1 不过+1 弃权 / c 全弃权保守通过)
  R8 revision_count 只 +1 / R9 output_schema 不泄漏 / R10 角色顺序契约

Send 并行下 3 个 call_llm 调用顺序不保证,角色断言一律用集合.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agents.review as R

DRAFT = "## 标题\n\n正文草稿内容"

passed = []


def check(name, cond, detail=""):
    passed.append((name, cond, detail))
    print(("PASS" if cond else "FAIL"), "-", name, detail)


def review_json(score, passed_flag, failed=None):
    """构造单个角色的合法审校 json 字符串."""
    return json.dumps(
        {"score": score, "passed": passed_flag, "failed_sections": failed or []},
        ensure_ascii=False,
    )


# ---------- fake call_llm:按角色区分响应,记录调用列表 ----------
calls = {"list": [], "per_role": {}}
responder = None  # 每个场景赋值: (role, attempt_index) -> 响应串


def reset_calls():
    calls["list"] = []
    calls["per_role"] = {r: 0 for r in R.REVIEW_ROLE_NAMES}


def fake_call_llm(system, user_content, json_mode=False, **kw):
    role = kw.get("role")
    calls["list"].append({"role": role, "user_content": user_content, "json_mode": json_mode})
    n = calls["per_role"].get(role, 0)
    calls["per_role"][role] = n + 1
    return responder(role, n, user_content)


def make_responder(spec):
    """spec: {role: [按尝试次数排列的响应,...]};缺省角色默认一次合法通过(score 80)."""
    def resp(role, attempt, user_content):
        lst = spec.get(role)
        if lst is None:
            return review_json(80, True)
        return lst[attempt] if attempt < len(lst) else lst[-1]
    return resp


R.call_llm = fake_call_llm
g = R.build_review_agent()


def run_case(spec):
    """设置 responder,重置调用记录,invoke 一次;返回 (out, roles, calls)."""
    global responder
    responder = make_responder(spec)
    reset_calls()
    out = g.invoke({"draft": DRAFT, "revision_count": 0})
    roles = {c["role"] for c in calls["list"]}
    return out, roles, calls


# ===== R1 三角色各调一次、全通过 =====
out, roles, _ = run_case({
    "edit_lang": [review_json(80, True)],
    "edit_logic": [review_json(90, True)],
    "edit_fact": [review_json(70, True)],
})
check("R1 三角色各调一次", len(calls["list"]) == 3, f"calls={len(calls['list'])}")
check("R1 角色集合正确", roles == set(R.REVIEW_ROLE_NAMES), f"{sorted(roles)}")
check("R1 passed=True", out["passed"] is True, f"{out['passed']}")
check("R1 quality_score=均值80", out["quality_score"] == 80, f"{out['quality_score']}")
check("R1 final_article==DRAFT", out["final_article"] == DRAFT, "")
check("R1 failed_sections==[]", out["failed_sections"] == [], f"{out['failed_sections']}")
check("R1 revision_count==1", out["revision_count"] == 1, f"{out['revision_count']}")

# ===== R2 多数不过(2 票不过),合并失败章节 =====
out, _, _ = run_case({
    "edit_lang": [review_json(90, True)],
    "edit_logic": [review_json(40, False, [{"id": 1, "feedback": "结构松散"}])],
    "edit_fact": [review_json(40, False, [
        {"id": 1, "feedback": "数据有误"},
        {"id": 2, "feedback": "来源不明"},
    ])],
})
check("R2 passed=False", out["passed"] is False, f"{out['passed']}")
fs = out["failed_sections"]
ids = [f["id"] for f in fs]
check("R2 失败章节按 id 升序", ids == sorted(ids), f"{ids}")
check("R2 恰两个失败章节", len(fs) == 2, f"{len(fs)}")
fb1 = next(f["feedback"] for f in fs if f["id"] == 1)
fb2 = next(f["feedback"] for f in fs if f["id"] == 2)
check("R2 id=1 含【逻辑结构】与【事实准确性】", "【逻辑结构】" in fb1 and "【事实准确性】" in fb1, fb1)
check("R2 id=2 只含【事实准确性】", "【事实准确性】" in fb2 and "【逻辑结构】" not in fb2, fb2)
check("R2 revision_count==1", out["revision_count"] == 1, f"{out['revision_count']}")

# ===== R3 多数通过(2 过 1 不过) =====
out, _, _ = run_case({
    "edit_lang": [review_json(80, True)],
    "edit_logic": [review_json(90, True)],
    "edit_fact": [review_json(40, False, [{"id": 1, "feedback": "数据有误"}])],
})
check("R3 passed=True", out["passed"] is True, f"{out['passed']}")
check("R3 failed_sections==[]", out["failed_sections"] == [], f"{out['failed_sections']}")
check("R3 final_article==DRAFT", out["final_article"] == DRAFT, "")
check("R3 revision_count==1", out["revision_count"] == 1, f"{out['revision_count']}")

# ===== R4 分数平均(含四舍五入) =====
out, _, _ = run_case({
    "edit_lang": [review_json(85, True)],
    "edit_logic": [review_json(86, True)],
    "edit_fact": [review_json(87, True)],
})
check("R4 均值 85/86/87 -> 86", out["quality_score"] == 86, f"{out['quality_score']}")

out, _, _ = run_case({
    "edit_lang": [review_json(85, True)],
    "edit_logic": [review_json(86, True)],
    "edit_fact": [review_json(88, True)],
})
check("R4 四舍五入 259/3 -> 86", out["quality_score"] == 86, f"{out['quality_score']}")

# ===== R5 单角色解析失败重试 =====
out, _, _ = run_case({
    "edit_lang": ["不是json", review_json(80, True)],
    "edit_logic": [review_json(90, True)],
    "edit_fact": [review_json(70, True)],
})
check("R5 总调用 4 次", len(calls["list"]) == 4, f"{len(calls['list'])}")
lang_calls = [c for c in calls["list"] if c["role"] == "edit_lang"]
check("R5 edit_lang 调 2 次", len(lang_calls) == 2, f"{len(lang_calls)}")
check("R5 第二次 user_content 含\"不是合法 json\"",
      "不是合法 json" in lang_calls[1]["user_content"], "")
check("R5 passed=True", out["passed"] is True, f"{out['passed']}")
check("R5 revision_count==1", out["revision_count"] == 1, f"{out['revision_count']}")

# ===== R7 弃权不拉偏 =====
# R7a:2 过 + 1 弃权
out, _, _ = run_case({
    "edit_lang": ["bad", "bad"],
    "edit_logic": [review_json(90, True)],
    "edit_fact": [review_json(70, True)],
})
check("R7a 总调用 4 次", len(calls["list"]) == 4, f"{len(calls['list'])}")
check("R7a passed=True(2 张显式通过票)", out["passed"] is True, f"{out['passed']}")
check("R7a score 只按有效票均值(90+70)/2=80", out["quality_score"] == 80, f"{out['quality_score']}")
check("R7a failed_sections==[]", out["failed_sections"] == [], f"{out['failed_sections']}")

# R7b:1 过 + 1 不过 + 1 弃权
out, _, _ = run_case({
    "edit_lang": [review_json(90, True)],
    "edit_logic": [review_json(40, False, [{"id": 1, "feedback": "结构松散"}])],
    "edit_fact": ["bad", "bad"],
})
check("R7b passed=False(只有 1 张通过票)", out["passed"] is False, f"{out['passed']}")
fs = out["failed_sections"]
check("R7b 弃权角色不贡献失败章节,只留 logic 的 id=1",
      len(fs) == 1 and fs[0]["id"] == 1 and "【逻辑结构】" in fs[0]["feedback"], f"{fs}")

# R7c:3 角色全弃权(各两次坏串)
out, _, _ = run_case({
    "edit_lang": ["bad", "bad"],
    "edit_logic": ["bad", "bad"],
    "edit_fact": ["bad", "bad"],
})
check("R7c 总调用 6 次", len(calls["list"]) == 6, f"{len(calls['list'])}")
check("R7c passed=True(全弃权保守通过)", out["passed"] is True, f"{out['passed']}")
check("R7c quality_score==0", out["quality_score"] == 0, f"{out['quality_score']}")
check("R7c failed_sections==[]", out["failed_sections"] == [], f"{out['failed_sections']}")
check("R7c final_article==DRAFT", out["final_article"] == DRAFT, "")
check("R7c revision_count==1", out["revision_count"] == 1, f"{out['revision_count']}")

# ===== R8 revision_count 只 +1(独立再跑一次确认) =====
out, _, _ = run_case({})
check("R8 revision_count 只在 aggregate +1", out["revision_count"] == 1, f"{out['revision_count']}")

# ===== R9 output_schema 不泄漏 =====
out, _, _ = run_case({})
expected_keys = {"final_article", "quality_score", "passed", "failed_sections", "revision_count"}
check("R9 output_schema 键精确", set(out.keys()) == expected_keys, f"{sorted(out.keys())}")
for key in ("role_name", "role_reviews", "draft"):
    check(f"R9 私有键 {key} 未泄漏", key not in out, f"{key} 泄漏" if key in out else "")

# ===== R10 角色顺序契约 =====
check("R10 REVIEW_ROLE_NAMES 固定顺序",
      R.REVIEW_ROLE_NAMES == ("edit_lang", "edit_logic", "edit_fact"),
      f"{R.REVIEW_ROLE_NAMES}")

# ===== R11 非 dict 输出防御(_parse_review_output 归一 ValueError,修复前击穿整图) =====
# R11a: 角色输出 JSON 数组(如 [{"score": 60}]) → 解析失败重试 2 次 → 弃权,不崩溃
out, _, _ = run_case({
    "edit_lang": [json.dumps([{"score": 60}])],
    "edit_logic": [review_json(90, True)],
    "edit_fact": [review_json(70, True)],
})
check("R11a 数组输出不崩溃、流程走完", out["passed"] is True, f"passed={out['passed']}")
lang_calls = [c for c in calls["list"] if c["role"] == "edit_lang"]
check("R11a edit_lang 重试 2 次后弃权(passed=None)", len(lang_calls) == 2, f"len={len(lang_calls)}")
check("R11a 弃权角色不参与表决,分数只按有效票均值 80",
      out["quality_score"] == 80, f"score={out['quality_score']}")

# R11b: 角色输出 score:null → 归一 ValueError → 重试 → 弃权,不崩溃
out, _, _ = run_case({
    "edit_lang": [json.dumps({"score": None, "passed": True, "failed_sections": []})],
    "edit_logic": [review_json(90, True)],
    "edit_fact": [review_json(70, True)],
})
check("R11b score:null 不崩溃、流程走完", out["passed"] is True, f"passed={out['passed']}")
lang_calls = [c for c in calls["list"] if c["role"] == "edit_lang"]
check("R11b edit_lang 重试 2 次后弃权(passed=None)", len(lang_calls) == 2, f"len={len(lang_calls)}")
check("R11b 弃权角色不参与表决,分数只按有效票均值 80",
      out["quality_score"] == 80, f"score={out['quality_score']}")

print()
failed = [p for p in passed if not p[1]]
print(f"共 {len(passed)} 项检查,通过 {len(passed) - len(failed)} 项")
sys.exit(1 if failed else 0)
