# -*- coding: utf-8 -*-
"""日期推导 / 幂等 / 渲染幂等的回归测试。

运行：python -m unittest discover -s tests -v
"""

import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build  # noqa: E402

BJ = timezone(timedelta(hours=8))


def bj(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=BJ)


class TargetDateTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TARGET_DATE", None)

    def assert_target(self, moment, expected):
        got, overridden = build.resolve_target_date(moment)
        self.assertEqual(got, expected, f"{moment} 推导错误")
        self.assertFalse(overridden)

    def test_primary_window(self):
        # 19:47 正常触发：归档当天
        self.assert_target(bj(2026, 11, 5, 19, 47), build.date(2026, 11, 5))

    def test_before_page_ready(self):
        # 19:39 手动跑：当天还没播完，归档昨天
        self.assert_target(bj(2026, 11, 5, 19, 39), build.date(2026, 11, 4))

    def test_cron_delayed_7_hours(self):
        # 实测的延迟：次日 02:53 才跑，仍应归档 11/4，而不是 11/5
        self.assert_target(bj(2026, 11, 5, 2, 53), build.date(2026, 11, 4))

    def test_overnight_catchup(self):
        self.assert_target(bj(2026, 11, 5, 0, 7), build.date(2026, 11, 4))
        self.assert_target(bj(2026, 11, 5, 7, 23), build.date(2026, 11, 4))

    def test_evening_fallback(self):
        self.assert_target(bj(2026, 11, 5, 21, 43), build.date(2026, 11, 5))

    def test_explicit_override(self):
        got, overridden = build.resolve_target_date(bj(2026, 11, 5, 2, 53), "2026-10-28")
        self.assertEqual(got, build.date(2026, 10, 28))
        self.assertTrue(overridden)

    def test_delay_minutes(self):
        # 19:47 → -3 分钟；次日 02:53 → +423 分钟
        for moment, expected in [
            (bj(2026, 11, 5, 19, 47), -3),
            (bj(2026, 11, 5, 2, 53), 423),
        ]:
            target, _ = build.resolve_target_date(moment)
            delay = int((moment - build.due_moment(target)).total_seconds() // 60)
            self.assertEqual(delay, expected)


class WaitForItemsTests(unittest.TestCase):
    def test_no_fallback_to_previous_days(self):
        """页面一直没就绪时，只能返回 pending，绝不能回退去抓前几天。"""
        seen = []

        def fake_fetch(target):
            seen.append(target)
            return None, []

        with mock.patch.object(build, "fetch_xwlb_items", fake_fetch), \
             mock.patch.object(build.time, "sleep", lambda *_: None):
            fl, items, err = build.wait_for_items(build.date(2026, 11, 5), 0)
        self.assertEqual(items, [])
        self.assertIsNone(err)       # 页面未就绪，不是硬故障
        self.assertTrue(all(d == build.date(2026, 11, 5) for d in seen))

    def test_poll_until_ready(self):
        calls = {"n": 0}

        def fake_fetch(target):
            calls["n"] += 1
            if calls["n"] < 2:
                return None, []
            return "https://tv.cctv.com/2026/11/05/full.shtml", [{"title": "A", "url": "u"}]

        with mock.patch.object(build, "fetch_xwlb_items", fake_fetch), \
             mock.patch.object(build.time, "sleep", lambda *_: None):
            fl, items, err = build.wait_for_items(build.date(2026, 11, 5), 6)
        self.assertEqual(len(items), 1)
        self.assertIsNone(err)
        self.assertEqual(calls["n"], 2)


class RenderTests(unittest.TestCase):
    def test_readme_roundtrip_keeps_history(self):
        """README 被脚本反复重写后，历史归档条目不能丢、不能重复。"""
        entries = [
            {"date_cn": "2026年11月4日", "path": "2026年/11月/04.html", "count": 16},
            {"date_cn": "2026年11月3日", "path": "2026年/11月/03.html", "count": 13},
        ]
        with mock.patch.object(build, "README_PATH", Path(__file__).with_name("_tmp_readme.md")):
            try:
                build.README_PATH.write_text(build.render_readme(entries), encoding="utf-8")
                recovered = build.collect_entries()
                self.assertEqual(len(recovered), 2)
                self.assertEqual(recovered["2026年11月4日"]["count"], 16)
                # 文档章节必须留在 README 里，不会被下一次运行抹掉
                self.assertIn("定时与延迟", build.README_PATH.read_text(encoding="utf-8"))
            finally:
                build.README_PATH.unlink(missing_ok=True)

    def test_day_html_renders(self):
        html = build.render_day_html(
            build.date(2026, 11, 5), "https://tv.cctv.com/x", [{"title": "新闻", "url": "https://x"}]
        )
        self.assertIn("2026年11月5日", html)
        self.assertIn("共 1 条", html)


class MainFlowTests(unittest.TestCase):
    """端到端：在临时目录里跑 main()，不联网、不写真实仓库。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        shutil.copy(Path(__file__).resolve().parents[1] / "README.md", self.root / "README.md")
        self.out = self.root / "github_output.txt"
        for target, value in [
            ("REPO_ROOT", self.root),
            ("STATE_PATH", self.root / "state" / "delivered.json"),
            ("INDEX_PATH", self.root / "index.html"),
            ("README_PATH", self.root / "README.md"),
        ]:
            p = mock.patch.object(build, target, value)
            p.start()
            self.addCleanup(p.stop)

        self.fake_items = [{"title": "新闻一条", "url": "https://tv.cctv.com/a"},
                           {"title": "新闻二条", "url": "https://tv.cctv.com/b"}]
        p = mock.patch.object(build, "fetch_xwlb_items",
                              lambda target: ("https://tv.cctv.com/full", self.fake_items))
        p.start()
        self.addCleanup(p.stop)

    def run_main(self, moment, **env):
        base = {
            "GITHUB_OUTPUT": str(self.out),
            "RUN_MODE": "normal", "DRY_RUN": "false", "WAIT_MINUTES": "0",
            "FEISHU_WEBHOOK": "", "FEISHU_SECRET": "", "TARGET_DATE": "",
        }
        base.update({k: str(v) for k, v in env.items()})
        with mock.patch.dict(os.environ, base, clear=False), \
             mock.patch.object(build, "now_bj", lambda: moment):
            self.out.unlink(missing_ok=True)
            build.main()
        result = {}
        if self.out.exists():
            for line in self.out.read_text(encoding="utf-8").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    result[k] = v
        return result

    def test_first_run_then_duplicate_is_skipped(self):
        first = self.run_main(bj(2026, 11, 5, 19, 47), TARGET_DATE="2026-11-05")
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["target_date"], "2026-11-05")
        self.assertEqual(first["count"], "2")
        self.assertEqual(first["delay_minutes"], "-3")
        self.assertTrue((self.root / "2026年" / "11月" / "05.html").is_file())
        self.assertIn("2026年11月5日", (self.root / "README.md").read_text(encoding="utf-8"))
        state = build.load_state()
        self.assertTrue(state["days"]["2026-11-05"]["done"])
        # 测试里没配 webhook，因此不应谎报「已推送」
        self.assertFalse(state["days"]["2026-11-05"]["notified"])

        second = self.run_main(bj(2026, 11, 5, 20, 17), TARGET_DATE="2026-11-05")
        self.assertEqual(second["status"], "skip")

    def test_delayed_cron_still_archives_correct_day(self):
        # 模拟实测的「次日 02:53 才跑」：不能推 11/5，也不能回退到 11/3
        out = self.run_main(bj(2026, 11, 5, 2, 53))
        self.assertEqual(out["target_date"], "2026-11-04")
        self.assertEqual(out["delay_minutes"], "423")
        self.assertTrue((self.root / "2026年" / "11月" / "04.html").is_file())

    def test_dry_run_does_not_notify_or_persist_state(self):
        out = self.run_main(bj(2026, 11, 5, 19, 47), DRY_RUN="true", TARGET_DATE="2026-11-05")
        self.assertEqual(out["status"], "dry")
        self.assertFalse(build.STATE_PATH.exists())

    def test_page_not_ready_returns_pending_without_writing(self):
        with mock.patch.object(build, "fetch_xwlb_items", lambda target: (None, [])):
            out = self.run_main(bj(2026, 11, 5, 19, 47), TARGET_DATE="2026-11-05")
        self.assertEqual(out["status"], "pending")
        self.assertFalse((self.root / "2026年" / "11月" / "05.html").exists())
        self.assertFalse(build.STATE_PATH.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
