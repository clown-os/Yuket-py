# -*- coding: utf-8 -*-
"""Runner 公共基类与路由分发。"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from . import js
from .browser import BrowserSession
from .config import Settings
from .player import Player
from .solver import AnswerClient
from .state import StateStore, normalize_url


class StopRequested(RuntimeError):
    """配置了 stop_on_fail，遇到无法完成的项时停止。"""


class MaxItemsReached(RuntimeError):
    """达到 --max-items 限制，正常结束。"""


@dataclass
class App:
    session: BrowserSession
    settings: Settings
    log: Any
    state: StateStore
    solver: AnswerClient

def is_done_text(text: str) -> bool:
    """与油猴脚本 checkCompletionStatus 一致。"""
    if not text:
        return False
    text = str(text)
    if "已完成" in text or "已读" in text:
        return True
    if "未开始" in text or "未读" in text or "进行中" in text:
        return False
    match = re.search(r"(\d+)\s*/\s*(\d+)", text)
    if match:
        cur, total = int(match.group(1)), int(match.group(2))
        return total > 0 and cur == total
    return False


class RunnerBase:
    def __init__(self, app: App):
        self.app = app
        self.session = app.session
        self.settings = app.settings
        self.log = app.log
        self.state = app.state
        self.solver = app.solver
        self.page = app.session.page
        self.course_url = normalize_url(self.page.url)
        self.processed = 0

    # ---------------- 通用 ----------------
    def dismiss_popups(self, page=None) -> None:
        try:
            (page or self.page).evaluate(js.DISMISS_POPUPS)
        except Exception:
            pass

    def wait(self, seconds: float) -> None:
        try:
            self.page.wait_for_timeout(int(seconds * 1000))
        except Exception:
            time.sleep(seconds)

    def click_maybe_new_page(self, locator, timeout: float = 5.0):
        """点击后可能新开标签页，也可能当前页路由跳转。返回实际承载内容的 Page。"""
        context = self.session.context
        assert context is not None
        before = set(context.pages)
        before_url = self.page.url
        locator.click()
        deadline = time.time() + timeout
        while time.time() < deadline:
            new_pages = [p for p in context.pages if p not in before]
            if new_pages:
                new_page = new_pages[-1]
                try:
                    new_page.wait_for_load_state("domcontentloaded", timeout=20000)
                except Exception:
                    pass
                return new_page
            if self.page.url != before_url:
                return self.page
            for selector in (".progress-wrap", "video", "audio", ".subject-item", ".container-problem", ".xt_video_player_current_time_display"):
                try:
                    if self.page.locator(selector).first.count() > 0:
                        return self.page
                except Exception:
                    pass
            self.page.wait_for_timeout(250)
        return self.page

    def back_to_list(self, work_page, list_selector: str = ".logs-list", course_url: str | None = None) -> None:
        """从视频/课件/作业页返回目录页。"""
        if work_page is not self.page:
            try:
                work_page.close()
            except Exception:
                pass
            try:
                self.page.bring_to_front()
            except Exception:
                pass
            self.wait(0.5)
            # 子页面（例如学习空间）播放完成后，目录页不会自动刷新状态；
            # 重新加载一次，才能读到服务端最新的完成情况。
            try:
                self.page.reload(wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:  # noqa: BLE001
                self.log.debug(f"刷新目录页失败：{exc}")
            deadline = time.time() + 15
            while time.time() < deadline:
                try:
                    if self.page.locator(list_selector).first.count() > 0:
                        return
                except Exception:
                    pass
                self.wait(0.5)
            return

        try:
            work_page.go_back(wait_until="domcontentloaded", timeout=15000)
        except Exception:
            try:
                work_page.evaluate("history.back()")
            except Exception:
                pass
        deadline = time.time() + 12
        while time.time() < deadline:
            try:
                if work_page.locator(list_selector).first.count() > 0:
                    return
            except Exception:
                pass
            self.wait(0.5)

        target = course_url or self.course_url
        if target and normalize_url(work_page.url) != normalize_url(target):
            self.log.debug(f"历史返回失败，直接打开课程目录：{target}")
            try:
                work_page.goto(target, wait_until="domcontentloaded")
            except Exception:
                pass
            self.wait(2)

    def progress_factory(self, page=None, selector: str = ".progress-wrap .text"):
        page = page or self.page

        def probe() -> dict:
            try:
                return page.evaluate(js.PROGRESS_STATE, selector) or {"has": False, "done": False}
            except Exception:
                return {"has": False, "done": False}

        return probe

    def make_player(self, page=None) -> Player:
        return Player(page or self.page, self.settings, self.log)

    def item_key(self, *parts: Any) -> str:
        return "|".join(str(p) for p in parts)

    def mark_done(self, key: str) -> None:
        self.state.clear_item(self.course_url, key)

    def note_attempt(self, key: str) -> None:
        self.state.add_attempt(self.course_url, key)

    def note_failed(self, key: str, reason: str) -> None:
        self.state.mark_failed(self.course_url, key, reason)
        self.log.warning(f"记录失败项：{reason}（可在 state.json 中清除后重试）")

    # ---------------- 答题 ----------------
    def solve_current_question(self, page, label: str = "") -> bool:
        """提取当前题目 -> 调接口 -> 点选项 -> 提交。返回是否处理成功。"""
        if not self.solver.enabled:
            self.log.info("未启用答题接口，跳过题目")
            return False
        try:
            state = page.evaluate(js.EXERCISE_EXTRACT) or {}
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"提取题目失败：{exc}")
            return False
        if not state.get("ok"):
            self.log.warning("未找到题目容器")
            return False
        if state.get("disabled") or state.get("answered"):
            self.log.info(f"{label or '当前题目'}：已完成，跳过")
            return True

        options = state.get("options") or []
        text = state.get("text") or ""
        if not options:
            self.log.warning(f"{label or '当前题目'}：未找到选项，无法自动作答")
            return False
        if len(text) < 5:
            self.log.warning(f"{label or '当前题目'}：题干过短，跳过")
            return False

        self.log.info(f"[AI] 请求答题接口（{len(options)} 个选项）…")
        indices = self.solver.solve(text, options)
        if not indices:
            self.log.warning(f"{label or '当前题目'}：未得到有效答案，跳过")
            return False
        try:
            clicked = page.evaluate(js.EXERCISE_CLICK, indices)
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"点击选项失败：{exc}")
            return False
        if not clicked:
            self.log.warning("没有点到任何选项")
            return False
        self.log.info(f"已选择 {clicked} 个选项")
        self.wait(0.4)
        if self.settings.answer_submit:
            try:
                if page.evaluate(js.EXERCISE_SUBMIT):
                    self.log.info("已点击提交/保存")
                else:
                    self.log.warning("未找到提交按钮，请手动提交")
            except Exception as exc:  # noqa: BLE001
                self.log.warning(f"提交失败：{exc}")
        self.wait(1.2)
        return True

    def should_skip_failed(self, key: str) -> bool:
        if self.state.has_failed(self.course_url, key):
            self.log.info("此前已重播到上限仍失败，跳过该项（清除 state.json 可重试）")
            return True
        if self.state.attempts(self.course_url, key) >= self.settings.replay_attempts:
            self.log.info("该项已用完重播次数，跳过")
            return True
        return False

    def handle_failure(self, key: str, label: str) -> None:
        self.note_failed(key, label)
        if self.settings.stop_on_fail:
            raise StopRequested(f"{label} 未能完成，已按配置停止")


def run(app: App) -> None:
    """按当前 URL 选择路线，逻辑对应 yuketang.js 的 start()。"""
    page = app.session.page
    url = page.url or ""
    path = url.split("?")[0]
    log = app.log

    if "/ai-workspace/lms-graph/" in path:
        from .runner_aiws import AiWorkspaceRunner

        log.info("匹配路线：ai-workspace/lms-graph")
        AiWorkspaceRunner(app).run()
        return

    if "/v2/web/" in path or "/studentCards/" in path:
        from .runner_v2 import V2Runner

        log.info("匹配路线：v2/web")
        V2Runner(app).run()
        return

    if "/pro/lms/" in path:
        from .runner_pro import ProRunner

        log.info("匹配路线：pro/lms")
        ProRunner(app).run()
        return

    log.error(
        "当前页面不是可识别的刷课页面。请打开以下任一页面后重试：\n"
        "  - 课程目录：https://<学校域名>/v2/web/studentLog/<教室ID>\n"
        "  - 旧版目录：https://<学校域名>/pro/lms/...\n"
        "  - 学习空间：https://<学校域名>/ai-workspace/lms-graph/..."
    )
