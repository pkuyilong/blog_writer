"""LLM JSON 输出强约束 + 校验失败反馈重试的通用封装.

背景:DeepSeek 官方 API 不支持服务端 response_format={"type":"json_schema"}
(会报 unavailable), 只能在客户端强约束. 本模块做法:
call_llm(默认 json_mode=True) → pydantic model_validate_json() 强校验 → 校验失败把
**结构化字段错误**(字段路径 + 原因)拼进 user_content 反馈给模型重试 → 耗尽返回
None, 由调用点决定兜底(审校弃权 / 拆章回退单章节).

与 model_router 的"API/传输异常"层(超时/限流/连接错误)两层正交:
- model_router 管"请求发不出去/发出去没回来", 按失败原因退避/切模型;
- 本模块管"请求回来了但内容不合法", 按校验错误反馈给模型重试.
两个输出 pydantic model(ReviewOutput/SplitOutput)是校验 schema 的**单一来源**:
prompts.py 用 model_json_schema() 导出文本嵌进 prompt 做强约束, 这里用它做客户端
强校验, 两侧永不漂移.
"""

import json
import logging
from typing import Callable, TypeVar

from pydantic import BaseModel, Field, ValidationError

from llm import call_llm

logger = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)

# pydantic v2 常见校验错误 type → 中文说明(拼进重试反馈给模型看).
_TYPE_HINTS = {
    "json_invalid": "不是合法的 json 文本",
    "missing": "缺少该字段（必须提供）",
    "int_type": "应为整数",
    "int_parsing": "应为整数（不能是字符串/文本，需输出数字本身）",
    "int_from_float": "应为整数（不接受小数）",
    "bool_type": "应为 true/false",
    "string_type": "应为字符串",
    "list_type": "应为数组",
    "dict_type": "应为对象",
    "model_type": "应为 json 对象",
    "greater_than_equal": "数值过小（低于允许下限）",
    "less_than_equal": "数值过大（高于允许上限）",
    "string_too_short": "长度过短（query 不能为空或太短）",
    "string_too_long": "长度过长（超出允许上限）",
}


# ===== 输出 pydantic model(JSON Schema 强约束的单一来源) =====

class FailedSection(BaseModel):
    """审校 JSON 里单个问题章节(与 state.py 的 Section 语义对齐)."""

    id: int = Field(description="章节编号。文章按 ## 小节组织，从 0 开始编号")
    feedback: str = Field(description="本角色视角下该章节的具体修改意见，只讲该章节自己的问题")


class ReviewOutput(BaseModel):
    """单个审校角色的输出 schema(语言/逻辑/事实 3 角色共用)."""

    score: int = Field(ge=0, le=100, description="本角色视角下的文章质量分，0 到 100 的整数")
    passed: bool = Field(description="本角色是否判定合格")
    failed_sections: list[FailedSection] = Field(
        description="passed 为 false 时列出需要重写的问题章节；passed 为 true 时为空数组 []"
    )


class SplitSection(BaseModel):
    """拆章 JSON 里的单个章节. 不含 id:id 由 split_sections 用 enumerate 程序补(与 state.py 的 Section 语义一致)."""

    title: str = Field(description="具体、贴切的章节标题（章节名 + 一句话点出重点）")
    points: list[str] = Field(description="本章要讲清楚的要点")
    materials: list[str] = Field(description="本章要用到的具体素材（数据/案例/来源）")


class SplitOutput(BaseModel):
    """拆章输出 schema:按提纲顺序的章节列表."""

    sections: list[SplitSection] = Field(description="按提纲顺序拆分的章节列表")


# ===== 校验错误格式化 + 重试反馈拼装 =====

def _loc_and_hint(err: dict) -> tuple[str, str]:
    """从单条 pydantic 校验错误取「字段路径 + 中文原因」, 供两个 formatter 共用.

    路径用 . 连接(嵌套如 failed_sections.0.feedback);空路径(非法 JSON / 非对象)
    记作"根对象";未收录的 type 原样输出.
    """
    loc = ".".join(str(x) for x in err["loc"]) or "根对象"
    hint = _TYPE_HINTS.get(err["type"], err["type"])
    return loc, hint


def _format_validation_errors(exc: ValidationError, limit: int = 10) -> list[str]:
    """把 exc.errors() 格式化成「字段路径 + 原因」的中文行,供拼进重试 user_content.

    超过 limit 条时提示先修复前几条.
    """
    lines = []
    errs = exc.errors()
    for err in errs[:limit]:
        loc, hint = _loc_and_hint(err)
        lines.append(f"- {loc}: {hint}（原始信息：{err.get('msg', '')}）")
    if len(errs) > limit:
        lines.append(f"- ……（共 {len(errs)} 处问题，请先修复以上 {limit} 处）")
    return lines


def format_tool_arg_errors(arguments: str, exc: ValidationError) -> str:
    """把工具参数校验错误格式化成「字段 + 期望 + 实际值」的中文行, 供作为 tool 消息反馈给模型.

    与 _format_validation_errors 的区别:额外解析原始 arguments 把字段的**实际值**带上
    (满足"哪个字段/期望什么/实际给了什么");非法 JSON 时取不到实际值, 只提示根对象.
    """
    try:
        actual = json.loads(arguments)
    except Exception:
        actual = None
    lines = []
    for err in exc.errors()[:5]:
        loc, hint = _loc_and_hint(err)
        got = ""
        if isinstance(actual, dict):
            key = err["loc"][0] if err["loc"] else None
            if key is not None and key in actual:
                got = f"（实际收到：{json.dumps(actual[key], ensure_ascii=False)}）"
        lines.append(f"- {loc}: {hint}{got}")
    return "\n".join(lines)


def tool_arg_error_content(arguments: str, exc: ValidationError) -> str:
    """构造「工具参数校验失败」的 tool 消息内容(把具体字段错误反馈给模型重新生成).

    与 format_tool_arg_errors 配套, 供 outliner.search 把非法工具调用转成带具体错误的
    tool 消息(而非静默丢弃/抛异常), 错误消息的 JSON 结构只此一份.
    """
    return json.dumps(
        {"error": "工具参数校验失败: " + format_tool_arg_errors(arguments, exc)},
        ensure_ascii=False,
    )


def _build_retry_content(base: str, error_lines: list[str], prefix: str) -> str:
    """把校验错误拼进原 user_content,构成下一次调用的提示(首行含小写 json 满足 DeepSeek 约束)."""
    block = "\n".join(error_lines)
    return (
        f"{base}\n\n"
        f"【{prefix}】你上一次输出的内容没有通过 json 结构校验，已被丢弃。"
        "请严格只输出一个符合要求的 json 对象，并按以下字段路径逐一修复：\n"
        f"{block}\n"
        "只输出完整、合法且符合要求的 json 对象本身，不要夹杂任何解释文字。"
    )


def call_json_model(
    system: str,
    user_content: str,
    model_cls: type[M],
    *,
    role: str | None = None,
    max_retries: int = 2,
    max_tokens: int = 16000,
    retry_prefix: str = "请重新输出",
    llm_call: Callable = call_llm,
) -> M | None:
    """一次 json_mode LLM + pydantic 强校验;校验失败带具体错误重试;耗尽返回 None.

    参数对齐 call_llm(system/user_content/role/max_tokens);call_llm 默认已启用
    json 模式,无需显式传 json_mode.
    llm_call 默认绑定模块级 call_llm,调用点应显式传自己的模块级 call_llm 引用,
    让测试里 R.call_llm = fake / W.call_llm = fake 的模块级替换继续生效.
    """
    base = user_content
    for attempt in range(1, max_retries + 1):
        raw = llm_call(system, user_content, role=role, max_tokens=max_tokens)  # call_llm 默认 json_mode=True
        if not isinstance(raw, str):
            raw = ""  # content 可能为 None(DeepSeek json 模式偶发空 content), 交给 json_invalid 走重试
        try:
            return model_cls.model_validate_json(raw)
        except ValidationError as exc:  # 同时覆盖 非法JSON(json_invalid) 与 合法但不合模型
            detail = _format_validation_errors(exc)
            logger.warning(
                f"  ⚠ json 输出校验失败（第 {attempt}/{max_retries} 次）："
                + " | ".join(detail)
            )
            if attempt < max_retries:
                user_content = _build_retry_content(base, detail, retry_prefix)
    return None
