#!/usr/bin/env python3
"""scripts/tests/golden_bodies.json の各 body(カード本文の見本)の最大文字数に上限を置く。

なぜ: 雛形(phase_card.md)に上限があっても、描画後の本文が静かに太れば、毎ターン再送される前置きは重くなる。
golden を作り直す(tools/make_golden.py)と、太った本文も「新しい正解」として通ってしまうので、ここで止める。

規則: 上限は MAX_GOLDEN_BODY(= 削減後の今の最大値)。本文を意図して太らせるなら、この定数を上げる変更を
同じ PR に入れ、PR 本文で理由を書く(レビューで見える形にする)。削って最大値が下がったら、定数も下げる(下げ忘れは落ちる)。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GOLDEN = REPO / "scripts" / "tests" / "golden_bodies.json"
MAX_GOLDEN_BODY = 3710  # 2026-11-12 段10 の削減後の最大値(3,746 から。phase_card.md の成果物の列挙を消した分)(golden 27 件のうち最長)


def lengths(path):
    cases = json.loads(Path(path).read_text(encoding="utf-8"))
    return [(i, c.get("ph"), c.get("g"), len(c["body"])) for i, c in enumerate(cases)]


def over_limit(path, limit=MAX_GOLDEN_BODY):
    return [row for row in lengths(path) if row[3] > limit]


class GoldenLimits(unittest.TestCase):
    def test_no_body_exceeds_the_limit(self):
        over = over_limit(GOLDEN)
        self.assertEqual(over, [], "golden の body が上限 %d 字を超えた(index, 段階, 群, 字数): %s" % (MAX_GOLDEN_BODY, over))

    def test_limit_is_the_current_max_not_slack(self):
        # 上限が緩んだまま残らないように: 最大値が下がったら定数も下げさせる。
        self.assertEqual(max(r[3] for r in lengths(GOLDEN)), MAX_GOLDEN_BODY,
                         "golden の最大の body が MAX_GOLDEN_BODY と違う。削ったなら定数を下げる")

    def test_detects_an_oversized_body(self):
        cases = json.loads(GOLDEN.read_text(encoding="utf-8"))
        cases[0]["body"] += "x" * (MAX_GOLDEN_BODY + 1)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "g.json"
            p.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
            self.assertIn(0, [r[0] for r in over_limit(p)])


if __name__ == "__main__":
    unittest.main(verbosity=1)
