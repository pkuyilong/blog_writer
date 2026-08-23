import argparse
import logging
import os
import sys
import uuid

from langgraph.types import Command

from graph import build_graph
from logging_config import setup_logging

logger = logging.getLogger(__name__)

CHECKPOINT_DB = ".checkpoints/blog_writer.db"
_EXIT_COMMANDS = {"q", "quit", "exit"}
_INTERRUPT_KEY = "__interrupt__"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="文章生成 Agent：给定题目，自动完成 调研 → 写作 → 审校 的文章创作。"
    )
    parser.add_argument(
        "topic",
        nargs="?",
        default=None,
        help="文章题目（中文）；--resume 模式下可不填",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="可选：把成品文章保存到指定文件（如 out.md）",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="输出 DEBUG 级别日志（默认 INFO）",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="可选：日志文件路径（默认项目根目录 blog_writer.log）",
    )
    parser.add_argument(
        "--human-review",
        action="store_true",
        help="大纲生成后暂停，由人工确认/修改大纲后再继续（默认全自动）",
    )
    parser.add_argument(
        "--resume",
        default=None,
        metavar="THREAD_ID",
        help="从断点继续写作（checkpoint 的 thread_id；Ctrl+C/中途退出后可用）。"
        "请与上次运行带相同的 --human-review 参数，保证图结构一致。",
    )
    parser.add_argument(
        "--in-memory",
        action="store_true",
        help="用 MemorySaver 代替 SqliteSaver（进程内、退出即失；仅供对比默认的跨进程持久化）",
    )
    return parser.parse_args()


def _run(args, checkpointer) -> int:
    """装配 checkpointer 后运行完整流程：构建图 →（可选 --resume）→ 交互 invoke → 打印成品。

    checkpointer 由 main() 提供（MemorySaver 实例，或 `with SqliteSaver.from_conn_string(...)`
    解包出的实例）；interrupt() 人工介入与 --resume 断点续跑都依赖它（compile(checkpointer=...)）。
    """
    graph = build_graph(enable_human_review=args.human_review, checkpointer=checkpointer)

    thread_id = args.resume or uuid.uuid4().hex
    config = {"configurable": {"thread_id": thread_id}}

    if args.resume:
        logger.info(f"↩ 从断点恢复：thread_id={thread_id}")
        snap = graph.get_state(config)
        logger.info(f"  快照：待执行 next={snap.next}，已存键={sorted((snap.values or {}).keys())}")
        # 停在 interrupt 处时 invoke(None) 会重新触发该 interrupt，进入下方交互循环
        initial_input = None
    else:
        logger.info(
            f"题目：《{args.topic}》（thread_id={thread_id}；"
            f"中途退出可用 --resume {thread_id} 断点续跑）\n"
        )
        initial_input = {
            "topic": args.topic,
            "sections": [],
            "section_drafts": {},
            "failed_sections": [],
            "revision_count": 0,
        }

    result = _interactive_invoke(graph, config, initial_input, thread_id)

    article = result["final_article"]
    # 成品文章是 stdout 产物（供直接查看 / 重定向保存），不走 logging；
    # 进度/告警日志已由 logging 输出到 stderr 与日志文件，不会混入产物。
    print("\n" + "=" * 50)
    print(f"成品文章（质量分 {result.get('quality_score', 'N/A')}/100，"
          f"审校 {result.get('revision_count', 'N/A')} 次）：")
    print("=" * 50)
    print(article)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(article)
        logger.info(f"已保存到：{args.output}")

    return 0


def _print_outline(payload: dict) -> None:
    """把大纲打印到 stderr（预览，不是产物）。

    交互提示统一走 stderr：main.py 末尾 print 成品文章占用 stdout，
    `> out.md` 重定向时提示不会混进产物（本项目既有约定）。
    """
    print("\n👀 人工审阅大纲（--human-review 已开启）：", file=sys.stderr)
    print("----------------------------------------", file=sys.stderr)
    print(payload.get("outline", ""), file=sys.stderr)
    print("----------------------------------------", file=sys.stderr)


def _print_help() -> None:
    print("  [回车]          确认大纲，继续写作", file=sys.stderr)
    print("  [输入文字]      作为修改意见，重新生成大纲", file=sys.stderr)
    print("  [# 开头的内容]  视为你粘贴的完整新大纲，直接采用", file=sys.stderr)
    print("  [q]             退出（可用 --resume <thread_id> 稍后继续）", file=sys.stderr)


def _resume_payload(cmd: str) -> dict:
    """把用户一行输入翻译成 interrupt 的 resume 载荷（三种 action，与 human_review_node 约定一致）。"""
    cmd = cmd.strip()
    if cmd.startswith("#"):
        return {"action": "replace", "outline": cmd}
    if cmd:
        return {"action": "revise", "feedback": cmd}
    return {"action": "confirm"}


def _interactive_invoke(graph, config, initial_input, thread_id: str) -> dict:
    """统一 invoke 循环：遇到 __interrupt__ 就交互（回车/意见/#粘贴/q），再 Command(resume=...) 续跑。

    全自动路径一次到底；HITL 多轮修改时循环多次（interrupt → resume → 再次 interrupt）。
    """
    inp = initial_input
    while True:
        result = graph.invoke(inp, config)
        if _INTERRUPT_KEY not in result:
            return result
        _print_outline(result[_INTERRUPT_KEY][0].value)
        _print_help()
        try:
            cmd = input()
        except EOFError:
            # stdin 被关闭（如管道提前结束）：按"退出可续跑"处理，避免卡死
            logger.info(f"  stdin 已关闭，进程暂停。可用 --resume {thread_id} 继续")
            sys.exit(0)
        if cmd.strip().lower() in _EXIT_COMMANDS:
            logger.info(f"  已暂停。可用 python main.py --resume {thread_id} 继续")
            sys.exit(0)
        inp = Command(resume=_resume_payload(cmd))


def main() -> int:
    args = parse_args()
    setup_logging(verbose=args.verbose, log_file=args.log_file)

    if not args.resume and not args.topic:
        logger.error('错误：需要题目（python main.py "题目"）或 --resume <thread_id>')
        return 1

    if args.in_memory:
        from langgraph.checkpoint.memory import MemorySaver

        logger.info("使用 MemorySaver（进程内、退出即失；仅供对比学习）")
        return _run(args, MemorySaver())

    # SqliteSaver.from_conn_string 返回 context manager，必须 with 解包取实例；
    # 直接把 context manager 传给 compile(checkpointer=...) 会报 TypeError
    from langgraph.checkpoint.sqlite import SqliteSaver

    os.makedirs(os.path.dirname(CHECKPOINT_DB), exist_ok=True)
    with SqliteSaver.from_conn_string(CHECKPOINT_DB) as checkpointer:
        return _run(args, checkpointer)


if __name__ == "__main__":
    sys.exit(main())
