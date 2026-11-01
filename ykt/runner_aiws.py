# -*- coding: utf-8 -*-
"""ai-workspace/lms-graph 路线（雨课堂新版学习空间）。

此路线的 DOM 变化最频繁，脚本按“导航叶子”推进：
视频/音频 -> 播完校验 -> 下一叶；练习 -> 答题 -> 下一叶。
"""
from __future__ import annotations

import re
import time

from . import js
from .runner import RunnerBase

ROUTE_RE = re.compile(r"/ai-workspace/lms-graph/([^/]+)/([^/]+)/([^/?#]+)")


def leaf_probe(page, title_hint: str = ""):
    """当前叶子完成度探针（学习空间左侧目录）。

    title_hint 用于视频播放后页面可能自动跳到下一节的情况：按标题找回刚才那一节。
    """

    def probe() -> dict:
        try:
            return page.evaluate(js.AI_LEAF_DONE, title_hint or "") or {"has": False, "done": False}
        except Exception:
            return {"has": False, "done": False}

    return probe


def play_aiws_media(page, player, label: str, key: str, course_url: str, state, title_hint: str = "") -> bool:
    """播放学习空间里的视频/音频，并用叶子状态校验是否完成。"""
    used = state.attempts(course_url, key)
    if used > 0:
        # 重播时强制从头开始（第一次播放允许从平台记录的断点继续）
        player.inject()
        if player.wait_ready(30):
            player.seek_start()
    return player.play_with_replay(
        leaf_probe(page, title_hint),
        label=label,
        attempts_used=used,
        reload_cb=lambda: page.reload(wait_until="domcontentloaded"),
        on_retry=lambda n: state.add_attempt(course_url, key),
    )


class AiWorkspaceRunner(RunnerBase):
    def __init__(self, app):
        super().__init__(app)
        self.nav_page = self.page if self._has_nav(self.page) else None

    def run(self) -> None:
        try:
            self.page.evaluate(js.PREVENT_SCREEN_CHECK)
        except Exception:
            pass

        if not ROUTE_RE.search(self.page.url or ""):
            self.log.error("当前不是 ai-workspace 叶子页")
            return

        while True:
            route = self._route(self.page.url or "")
            if not route:
                self.log.info("已离开 ai-workspace，停止")
                return
            self.log.info(f"当前知识点：{route['type']} / {route['leafId']}")
            try:
                ok = self._handle(route)
            except Exception as exc:  # noqa: BLE001
                self.log.warning(f"处理知识点出错：{exc}")
                ok = False
            if not ok:
                self.log.warning("当前知识点未处理成功，继续下一项")

            self.processed += 1
            if self.settings.max_items and self.processed >= self.settings.max_items:
                self.log.info(f"已达到 max_items={self.settings.max_items}，停止")
                return
            if not self._advance():
                self.log.info("没有下一个知识点，结束")
                return

    # ---------------- 处理 ----------------
    def _handle(self, route: dict) -> bool:
        kind = route["type"]
        if kind in ("video", "audio"):
            return self._handle_media(route)
        if kind == "exercise":
            return self._handle_exercise(route)
        self.log.info(f"类型 {kind} 暂不自动处理，跳过")
        self.wait(2)
        return True

    def _handle_media(self, route: dict) -> bool:
        page = self.page
        title = ""
        try:
            title = page.evaluate(js.AI_ACTIVE_LEAF_TITLE) or ""
        except Exception:
            pass
        title = title or f"{route['type']} {route['leafId']}"
        self.log.info(f"开始播放：{title}")

        deadline = time.time() + 25
        ready = False
        while time.time() < deadline:
            try:
                if page.locator("video, audio").first.count() > 0:
                    ready = True
                    break
            except Exception:
                pass
            self.wait(0.5)
        if not ready:
            self.log.warning("未找到视频/音频元素")
            return False

        player = self.make_player(page)
        key = self.item_key("AW", route["classroomId"], route["type"], route["leafId"])
        ok = play_aiws_media(page, player, title, key, self.course_url, self.state, title_hint=title)
        if ok:
            self.mark_done(key)
        else:
            self.handle_failure(key, title)
        return ok


    def _handle_exercise(self, route: dict) -> bool:
        if not self.solver.enabled:
            self.log.info("未启用答题接口，跳过练习")
            return True
        page = self.page
        deadline = time.time() + 20
        frame = None
        while time.time() < deadline and frame is None:
            for f in page.frames:
                try:
                    if f.evaluate("() => !!document.querySelector('.container-problem, .subject-item, .item-body')"):
                        frame = f
                        break
                except Exception:
                    continue
            if frame is None:
                self.wait(0.5)
        if frame is None:
            self.log.warning("未找到练习题目容器")
            return False

        try:
            count = int(frame.evaluate(js.EXERCISE_TABS) or 0)
        except Exception:
            count = 0
        self.log.info(f"练习共 {count or 1} 题")
        for i in range(max(count, 1)):
            if count:
                try:
                    tabs = frame.locator(".subject-item")
                    if tabs.count() == 0:
                        tabs = frame.locator(".problem-index-item, .question-index-item")
                    tabs.nth(i).click()
                    self.wait(1.2)
                except Exception:
                    pass
            self.solve_current_question(frame, f"第 {i + 1} 题")
        return True

    # ---------------- 导航 ----------------
    def _has_nav(self, page) -> bool:
        try:
            return page.locator(".nav-item-leaf-box").first.count() > 0
        except Exception:
            return False

    def _nav_page(self):
        if self.nav_page is not None:
            try:
                if not self.nav_page.is_closed():
                    return self.nav_page
            except Exception:
                pass
        if self._has_nav(self.page):
            self.nav_page = self.page
            return self.page
        return None

    def _advance(self) -> bool:
        # 如果当前在新标签页里，先回到带导航的源页面
        if not self._has_nav(self.page):
            source = self._nav_page()
            if source is None:
                return False
            try:
                if source is not self.page:
                    self.page.close()
                    self.page = source
                    source.bring_to_front()
                    self.wait(1.5)
            except Exception:
                self.page = source

        leaves = []
        try:
            leaves = self.page.evaluate(js.AI_LEAVES) or []
        except Exception:
            pass
        if not leaves:
            self.log.debug("导航列表为空")
            return False

        active = next((i for i, leaf in enumerate(leaves) if leaf.get("active")), -1)
        nxt = active + 1 if active >= 0 else 0
        if nxt >= len(leaves):
            return False

        loc = self.page.locator(".nav-item-leaf-box").nth(nxt)
        try:
            target = loc.locator("xpath=./*").first
            if target.count() == 0:
                target = loc
        except Exception:
            target = loc
        try:
            work = self.click_maybe_new_page(target)
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"点击下一个知识点失败：{exc}")
            return False
        self.wait(2)
        if work is not self.page:
            self.nav_page = self.page
            self.page = work
        try:
            self.page.evaluate(js.PREVENT_SCREEN_CHECK)
        except Exception:
            pass
        return True

    @staticmethod
    def _route(url: str) -> dict | None:
        match = ROUTE_RE.search(url)
        if not match:
            return None
        return {"classroomId": match.group(1), "type": match.group(2), "leafId": match.group(3)}
