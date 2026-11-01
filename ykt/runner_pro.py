# -*- coding: utf-8 -*-
"""pro/lms 路线（旧版雨课堂学习页）。

新版页面有 .btn-next，当前页直接播放并翻页；
旧版目录只有 .leaf-detail，点击后通常新开标签页，点击后交给同一个翻页逻辑。
"""
from __future__ import annotations

import time

from . import js
from .runner import RunnerBase, is_done_text
from .state import normalize_url


class ProRunner(RunnerBase):
    def run(self) -> None:
        if self.page.locator(".btn-next").count() > 0:
            self._run_new(self.page)
            return
        self._run_old()

    # ---------------- 新版 ----------------
    def _run_new(self, page) -> None:
        try:
            page.evaluate(js.PREVENT_SCREEN_CHECK)
        except Exception:
            pass
        course_key = normalize_url(page.url)
        self.course_url = course_key
        index = self.state.course(course_key).get("outside", 0)
        if index:
            self.log.info(f"pro/lms 断点：从第 {index + 1} 集继续")

        while True:
            self.wait(2)
            self._dismiss(page)
            try:
                header = page.evaluate(js.PRO_HEADER) or {}
            except Exception:
                header = {}
            name = header.get("name") or f"第 {index + 1} 集"
            type_ = header.get("type") or ""
            status = header.get("status") or ""
            done = "100%" in status or "99%" in status or "98%" in status or "已完成" in status or is_done_text(status)

            self.log.info(f"第 {index + 1} 集：{name}（类型 {type_ or '未知'}，状态 {status or '未知'}）")
            if "shipin" in type_ and not done:
                key = self.item_key("P", index, name)
                if self.should_skip_failed(key):
                    pass
                else:
                    if self._ensure_video(page):
                        player = self.make_player(page)
                        probe = self._pro_probe(page)
                        ok = player.play_with_replay(
                            probe,
                            label=name,
                            attempts_used=self.state.attempts(course_key, key),
                            reload_cb=lambda: page.reload(wait_until="domcontentloaded"),
                            on_retry=lambda n: self.note_attempt(key),
                        )
                        if ok:
                            self.mark_done(key)
                        else:
                            self.handle_failure(key, name)
                        self.processed += 1
                    else:
                        self.log.warning("页面上没有视频元素，跳过本集")
            else:
                self.wait(2)

            if self.settings.max_items and self.processed >= self.settings.max_items:
                self.log.info(f"已达到 max_items={self.settings.max_items}，停止")
                return

            next_btn = page.locator(".btn-next").first
            try:
                if next_btn.count() == 0:
                    self.log.info("没有下一集按钮，课程播放完毕")
                    return
                next_btn.click()
            except Exception as exc:  # noqa: BLE001
                self.log.warning(f"点击下一集失败：{exc}")
                return

            index += 1
            self.state.set_progress(course_key, outside=index, inside=0)
            self.wait(2)
            try:
                page.evaluate(js.PREVENT_SCREEN_CHECK)
            except Exception:
                pass

    def _pro_probe(self, page):
        def probe() -> dict:
            try:
                header = page.evaluate(js.PRO_HEADER) or {}
            except Exception:
                return {"has": False, "done": False}
            status = header.get("status") or ""
            done = "100%" in status or "99%" in status or "98%" in status or "已完成" in status
            return {"has": bool(status), "done": done, "text": status}

        return probe

    def _ensure_video(self, page, timeout: float = 20.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if page.locator("video").first.count() > 0:
                    return True
            except Exception:
                pass
            self.wait(1)
        return False

    def _dismiss(self, page) -> None:
        try:
            page.evaluate(js.DISMISS_POPUPS)
        except Exception:
            pass

    # ---------------- 旧版 ----------------
    def _run_old(self) -> None:
        self.log.info("检测到 pro/lms 旧版目录，寻找视频节点")
        leaves = self.page.locator(".leaf-detail")
        count = leaves.count()
        if count == 0:
            self.log.warning("没有找到 .leaf-detail 节点")
            return
        for i in range(count):
            leaf = leaves.nth(i)
            try:
                icon = leaf.evaluate("el => el.querySelector('i')?.className || ''")
            except Exception:
                icon = ""
            if "shipin" not in icon:
                self.log.debug(f"第 {i + 1} 个节点不是视频，跳过")
                continue
            self.log.info(f"点击第 {i + 1} 个视频节点")
            try:
                work = self.click_maybe_new_page(leaf)
            except Exception as exc:  # noqa: BLE001
                self.log.warning(f"点击失败：{exc}")
                continue
            self.wait(3)
            if work is self.page:
                self._run_new(work)
                return
            try:
                self._run_new(work)
            finally:
                try:
                    work.close()
                except Exception:
                    pass
                try:
                    self.page.bring_to_front()
                except Exception:
                    pass
            return
        self.log.info("旧版目录中没有可刷的视频节点")
