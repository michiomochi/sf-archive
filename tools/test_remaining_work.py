"""検査 remaining_work_check が「起票漏れ」を拒み、「なし」と「行付き」を通すこと。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import remaining_work_check as R  # noqa: E402

BAD_NO_SECTION = "# 完了レポート\n\n## 実施内容\nやった\n"
BAD_UNTICKETED = """## 未確認・残件
- 別 repo の CI が赤いまま（未対応）
"""
GOOD_NONE = """## 未確認・残件
なし
"""
GOOD_NONE_DASH = """## 未確認・残件
- なし
"""
GOOD_TICKETED = """## 未確認・残件
- 別 repo の CI 改善: https://app.notion.com/p/ee485a7664c2c3b879545592474e880a
- 追加の計測: https://app.notion.com/p/00d1b6e252e1e47ffed27af6884d1cb9
"""
BAD_EMPTY = """## 未確認・残件

## 次のタスク
"""


def main():
    checks = 0
    assert R.check(BAD_NO_SECTION), "章が無いのに通した"
    checks += 1
    p = R.check(BAD_UNTICKETED)
    assert p and "起票されていない" in p[0], p
    checks += 1
    assert R.check(GOOD_NONE) == [], R.check(GOOD_NONE)
    checks += 1
    assert R.check(GOOD_NONE_DASH) == [], R.check(GOOD_NONE_DASH)
    checks += 1
    assert R.check(GOOD_TICKETED) == [], R.check(GOOD_TICKETED)
    checks += 1
    assert R.check(BAD_EMPTY), "空の章を通した"
    checks += 1
    print(f"remaining_work: {checks} checks, 0 failed")


if __name__ == "__main__":
    main()
