"""运行 Phase 1 验证：Setting1 + 一个完整官方 family。"""

from __future__ import annotations

from pathlib import Path

import run_phaseA_setting1 as setting1_runner


def main() -> int:
    setting1_runner.OUTPUT_DIR = (
        Path(__file__).resolve().parents[1]
        / "results"
        / "phase1_setting1_validation"
    )
    setting1_runner.RUN_LABEL = "Phase1_Setting1_Validation"
    setting1_runner.ANALYSIS_KEY = "phase1_validation_analysis"
    return setting1_runner.main()


if __name__ == "__main__":
    raise SystemExit(main())