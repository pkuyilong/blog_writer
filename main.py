import argparse
import logging
import sys

from graph import build_graph
from logging_config import setup_logging

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="文章生成 Agent：给定题目，自动完成 调研 → 写作 → 审校 的文章创作。"
    )
    parser.add_argument("topic", help="文章题目（中文）")
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
        help="可选：日志文件路径（默认项目根目录 b_writer.log）",
    )
    parser.add_argument(
        "--human-review",
        action="store_true",
        help="大纲生成后暂停，由人工确认/修改大纲后再继续（默认全自动）",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    setup_logging(verbose=args.verbose, log_file=args.log_file)

    if not args.topic.strip():
        logger.error('错误：题目不能为空。用法：python main.py "你的文章题目"')
        return 1

    logger.info(f"题目：《{args.topic}》\n")

    graph = build_graph(enable_human_review=args.human_review)
    result = graph.invoke(
        {
            "topic": args.topic,
            "sections": [],
            "section_drafts": {},
            "failed_sections": [],
            "revision_count": 0,
        }
    )

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


if __name__ == "__main__":
    sys.exit(main())
