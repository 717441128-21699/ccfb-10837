"""全量核对入口：引擎实控人 vs 仿真站预设答案。"""
from __future__ import annotations

import _bootstrap  # noqa: F401

from equity.analysis.verify import print_report, run_verification


def main() -> int:
    report = run_verification(save=True)
    print_report(report)
    return 0 if report.mismatched == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
