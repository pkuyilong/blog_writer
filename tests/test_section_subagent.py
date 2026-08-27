"""确定性验证"章节写作子智能体"(agents/section_writer.py 子图)的控制流.

mock agents.section_writer.call_llm,不耗 token.覆盖:首写合格 / 首写不合格→重写收敛 /
两次不合格接受 / 打回带审校意见传递 / 要点覆盖自检 / 输出 schema 只暴露 section_drafts.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agents.section_writer as SW

passed = []


def check(name, cond, detail=""):
    passed.append((name, cond, detail))
    print(("PASS" if cond else "FAIL"), "-", name, detail)


SECTION = {"id": 2, "title": "引言：远程办公为何兴起", "points": [], "materials": ["数据1"]}
TOPIC = "为什么越来越多的人选择远程办公"

# 长文:通过篇幅自检(>=120 字,## 开头);短文:触发"篇幅不足"
LONG = "## 引言：远程办公为何兴起\n\n" + "这里是一段足够长的章节正文，用于通过篇幅自检。" * 10
SHORT = "太短了，撑不起一个章节"


class FakeLLM:
    """按 system 区分首写/重写,返回预设文本;记录调用次数与 user_content."""

    def __init__(self, write_result, rewrite_result):
        self.write_result = write_result
        self.rewrite_result = rewrite_result
        self.calls = {"write": 0, "rewrite": 0}
        self.last_content = {}

    def __call__(self, system, user_content, json_mode=False, **kw):
        if system == SW.WRITE_SECTION_PROMPT:
            self.calls["write"] += 1
            self.last_content["write"] = user_content
            return self.write_result
        if system == SW.SELF_REVIEW_PROMPT:
            self.calls["rewrite"] += 1
            self.last_content["rewrite"] = user_content
            return self.rewrite_result
        raise AssertionError(f"未知 system: {system[:30]}")


def invoke(fake, feedback="", section=None):
    SW.call_llm = fake
    g = SW.build_section_writer()
    return g.invoke({"section": section or SECTION, "topic": TOPIC, "feedback": feedback})


# S1 首写合格:只调 WRITE_SECTION 1 次,不触发重写;输出只暴露 section_drafts
fake = FakeLLM(LONG, LONG)
out = invoke(fake)
check("S1 首写合格: WRITE_SECTION 调 1 次", fake.calls["write"] == 1, f"write={fake.calls['write']}")
check("S1 首写合格: SELF_REVIEW 不调用（省掉旧版无条件反思）", fake.calls["rewrite"] == 0,
      f"rewrite={fake.calls['rewrite']}")
check("S1 首写合格: 输出 section_drafts 键为 str(id)", out["section_drafts"] == {"2": LONG},
      f"out={out['section_drafts']!r}")
check("S1 首写合格: 输出键只含 section_drafts（output_schema 限定）",
      set(out.keys()) == {"section_drafts"}, f"keys={set(out.keys())}")
check("S1 首写合格: 输入/私有键（topic/write_attempt/self_check_notes）不回传",
      all(k not in out for k in ("topic", "write_attempt", "self_check_notes", "section", "feedback")),
      f"keys={set(out.keys())}")

# S2 首写不合格→重写收敛:WRITE 1 次 + SELF_REVIEW 1 次,输出长文
fake = FakeLLM(SHORT, LONG)
out = invoke(fake)
check("S2 重写收敛: WRITE 1 次 + SELF_REVIEW 1 次（attempt 走到 2）",
      fake.calls["write"] == 1 and fake.calls["rewrite"] == 1,
      f"write={fake.calls['write']} rewrite={fake.calls['rewrite']}")
check("S2 重写收敛: 输出为重写后的长文", out["section_drafts"] == {"2": LONG}, f"out={out['section_drafts']!r}")
check("S2 重写收敛: 重写轮 user_content 含【当前草稿】", "【当前草稿】" in fake.last_content["rewrite"],
      "含【当前草稿】" if "【当前草稿】" in fake.last_content["rewrite"] else "")
check("S2 重写收敛: 重写轮 user_content 含【自检意见】", "【自检意见】" in fake.last_content["rewrite"],
      "含【自检意见】" if "【自检意见】" in fake.last_content["rewrite"] else "")
check("S2 重写收敛: 【自检意见】指出篇幅不足", "篇幅不足" in fake.last_content["rewrite"],
      "含篇幅不足" if "篇幅不足" in fake.last_content["rewrite"] else "")

# S3 两次不合格接受:WRITE 1 + SELF_REVIEW 1,输出短文(达上限接受,不抛错)
fake = FakeLLM(SHORT, SHORT)
out = invoke(fake)
check("S3 两次不合格: WRITE 1 次 + SELF_REVIEW 1 次",
      fake.calls["write"] == 1 and fake.calls["rewrite"] == 1,
      f"write={fake.calls['write']} rewrite={fake.calls['rewrite']}")
check("S3 两次不合格: 输出短文（达上限接受）", out["section_drafts"] == {"2": SHORT},
      f"out={out['section_drafts']!r}")

# S4 打回带审校意见:首写轮 user_content 应含【上轮审校意见】
fake = FakeLLM(LONG, LONG)
out = invoke(fake, feedback="第2章太空泛，需补充具体数据")
check("S4 审校意见: 首写轮 user_content 含【上轮审校意见】", "【上轮审校意见】" in fake.last_content["write"],
      "含审校意见" if "【上轮审校意见】" in fake.last_content["write"] else "")
check("S4 审校意见: 意见内容原样传入", "第2章太空泛" in fake.last_content["write"],
      "含意见内容" if "第2章太空泛" in fake.last_content["write"] else "")

# S5 要点不做覆盖检查:正文长,## 开头但完全不含任何要点 → 首写即合格
#(split 的 points 是描述性写作指令,正文不可能逐字复述,子串匹配必误判--
#  真实 e2e 两次 100% 误判后已移除该检查,内容覆盖交给外部 editor 审校)
SECTION_P = {"id": 3, "title": "效率与平衡", "points": ["远程办公的效率变化与数据支撑"], "materials": []}
LONG_NO_POINT = "## 效率与平衡\n\n" + "这里是一段足够长但完全没有覆盖任何要点的正文。" * 10
fake = FakeLLM(LONG_NO_POINT, LONG_NO_POINT)
out = invoke(fake, section=SECTION_P)
check("S5 要点不检查: 正文不含要点仍首写即合格", fake.calls["write"] == 1 and fake.calls["rewrite"] == 0,
      f"write={fake.calls['write']} rewrite={fake.calls['rewrite']}")
check("S5 要点不检查: 输出为该章草稿", out["section_drafts"] == {"3": LONG_NO_POINT},
      f"out={out['section_drafts']!r}")

print()
failed = [p for p in passed if not p[1]]
print(f"共 {len(passed)} 项检查，通过 {len(passed) - len(failed)} 项")
sys.exit(1 if failed else 0)
