"""统一日志配置：让所有模块用标准 logging 替代 print。

- 终端（stderr）：简洁格式，只显示消息本体（保留各节点原有的 emoji/缩进观感），
  默认 INFO，`main.py --verbose` 时降到 DEBUG。
- 文件（默认 b_writer.log，已在 .gitignore 忽略）：带时间戳/级别/模块名，
  始终记录 DEBUG，便于事后调试追踪。
- stdout 保留给成品文章输出（main.py 的 print）：日志固定走 stderr，
  `python main.py "题目" > out.md` 重定向时日志不会混进产物。

用法：入口（main.py）调用 `setup_logging(verbose=..., log_file=...)`；各模块用
`logger = logging.getLogger(__name__)` 输出 info / warning / error。
"""
import logging
import sys
from pathlib import Path

DEFAULT_LOG_FILE = "b_writer.log"
# 终端不显示级别前缀：消息里自带的 emoji/缩进已经表达语义，保持 CLI 观感
_CONSOLE_FORMAT = "%(message)s"
_FILE_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging(verbose: bool = False, log_file: str | None = None) -> None:
    """配置全局日志：stderr 简洁输出 + 文件详细输出。幂等，可多次调用。

    Args:
        verbose: True 时终端显示级别降到 DEBUG（默认 INFO）。
        log_file: 日志文件路径；None 时写项目根目录的 b_writer.log。
    """
    # 根 logger 恒放行 DEBUG，具体显示级别由各 handler 把关：
    # 终端按 verbose 决定（INFO/DEBUG），文件始终记录 DEBUG（便于追踪）。
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter(_CONSOLE_FORMAT))

    path = Path(log_file) if log_file else _default_log_path()
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(_FILE_FORMAT))

    # 替换旧 handler（幂等：避免重复输出、重复打开文件句柄）
    for h in root.handlers[:]:
        root.removeHandler(h)
        h.close()
    root.addHandler(console)
    root.addHandler(fh)


def _default_log_path() -> Path:
    return Path(__file__).resolve().parent / DEFAULT_LOG_FILE
