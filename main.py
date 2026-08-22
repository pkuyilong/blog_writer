import argparse
import sys

from graph import build_graph


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
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not args.topic.strip():
        print('错误：题目不能为空。用法：python main.py "你的文章题目"', file=sys.stderr)
        return 1

    print(f"题目：《{args.topic}》\n")

    graph = build_graph()
    result = graph.invoke({"topic": args.topic})

    article = result["final_article"]
    print("\n" + "=" * 50)
    print("成品文章：")
    print("=" * 50)
    print(article)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(article)
        print(f"\n已保存到：{args.output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
