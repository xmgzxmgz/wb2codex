"""命令行入口。"""
import os
import sys

from . import __version__, exporter, importer

USAGE = """wb2codex — WorkBuddy → Codex 会话迁移工具

用法:
  wb2codex list [--limit N] [--project KW]
      列出 WorkBuddy 本地会话

  wb2codex export [--out DIR] [--since YYYY-MM-DD] [--limit N]
                  [--keep-reasoning] [--keep-reminders]
      导出为 Codex rollout 格式（默认写到 ./codex-import）

  wb2codex import <目录> [--dry-run]
      把导出目录里的所有 rollout 投影进 Codex 历史库
      （自动补 thread_settings_applied 行、重排 ordinal、对齐投影游标）

  wb2codex import <单个 rollout.jsonl> [--dry-run]
      只投影一个会话

  wb2codex --version
      显示版本

示例:
  wb2codex list --limit 20
  wb2codex export --out ./codex-import
  wb2codex import ./codex-import --dry-run
  wb2codex import ./codex-import

注意:
  导入前请完全退出 Codex / ChatGPT 桌面版，否则数据库可能被覆盖。
  首次运行会自动备份 state_5.sqlite 与 thread_history_1.sqlite。
"""

# 直接透传给 exporter 的子命令
EXPORT_CMDS = ("list", "export", "export-all", "to-claude")


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if args[0] in ("-V", "--version", "version"):
        print(f"wb2codex {__version__}")
        return 0

    cmd, rest = args[0], args[1:]

    if cmd in EXPORT_CMDS:
        sys.argv = ["wb2codex", cmd] + rest
        return exporter.main()

    if cmd in ("import", "project"):
        target = rest[0] if rest and not rest[0].startswith("-") else None
        extra = rest[1:] if target else rest
        if not target:
            print("用法: wb2codex import <目录或 rollout 文件> [--dry-run]", file=sys.stderr)
            return 1
        if os.path.isdir(target):
            sys.argv = ["wb2codex", "--all", target] + extra
        else:
            sys.argv = ["wb2codex", target] + extra
        return importer.main()

    print(f"未知命令: {cmd}\n", file=sys.stderr)
    print(USAGE)
    return 1


if __name__ == "__main__":
    sys.exit(main())
