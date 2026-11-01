# -*- coding: utf-8 -*-
"""按网站自带的「未完成」列表并行刷课。

流程：
  课程页 → 未完成 tab → 视频筛选 → 读取未完成视频队列
  → 4 个标签页并行打开视频（直接 ai-workspace 地址，不返回上级列表）
  → 进入视频先看完成度：已完成则跳过；未完成则从 00:00 完整播放
  → 播完检测完成度；未完成则整段重播一次
  → 同一个标签页继续下一个视频
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from . import js
from .player import Player
from .runner import RunnerBase, normalize_url

TAB_TEXT = "未完成"
VIDEO_TEXT = "视频"


def has_unfinished_tab(page) -> bool:
    try:
        return page.locator(".rain-tabs__nav-item").filter(has_text=TAB_TEXT).count() > 0
    except Exception:
        return False


@dataclass
class Worker:
    index: int
    page: object
    player: Player | None = None
    item: dict | None = None
    url: str = ""
    phase: str = "idle"  # idle / loading / playing / checking / done
    attempts: int = 0
    load_retries: int = 0
    deadline: float = 0.0
    last_current: float = -1.0
    last_move: float = 0.0
    check_started: float = 0.0


class UnfinishedCourseRunner(RunnerBase):
    def __init__(self, app, course_url: str):
        super().__init__(app)
        self.page_url = course_url
        self.course_url = normalize_url(course_url)
        self.page = app.session.page
        parts = urlsplit(course_url)
        self.origin = f"{parts.scheme}://{parts.netloc}"
        self.classroom_id = self._parse_classroom(course_url)
        self.workers_count = max(1, min(8, int(getattr(app.settings, "workers", 4) or 4)))

    # ---------------- 入口 ----------------
    def run(self) -> None:
        if getattr(self.settings, "homework_only", False):
            self._run_homework()
            return
        self._goto_course()
        self._open_unfinished()
        items = self._read_items()
        self.log.info(f"「未完成」列表中共有 {len(items)} 个视频待完成")

        queue: list[dict] = []
        for item in items:
            key = item.get("key")
            if not key:
                continue
            ukey = f"U|{key}"
            if self.state.has_failed(self.course_url, ukey):
                self.log.debug(f"跳过此前失败项：{item.get('title')}")
                continue
            if self.state.attempts(self.course_url, ukey) >= self.settings.replay_attempts:
                self.log.debug(f"跳过已用完重播次数：{item.get('title')}")
                continue
            queue.append(item)
        if self.settings.max_items and self.settings.max_items > 0:
            queue = queue[: self.settings.max_items]
        if not queue:
            self.log.info("没有需要处理的视频")
            self._run_homework()
            return

        count = min(self.workers_count, len(queue))
        self.log.info(f"启动 {count} 个并行标签页开始刷课")
        context = self.session.context
        assert context is not None
        workers: list[Worker] = []
        for i in range(count):
            page = context.new_page()
            page.set_default_timeout(20000)
            page.on("dialog", self.session._on_dialog)
            workers.append(Worker(index=i + 1, page=page))

        next_index = 0
        try:
            while True:
                for w in workers:
                    if w.phase == "idle" and next_index < len(queue):
                        item = queue[next_index]
                        next_index += 1
                        self._assign(w, item)
                    try:
                        self._step(w)
                    except Exception as exc:  # noqa: BLE001
                        self.log.warning(f"[W{w.index}] 处理异常：{exc}")
                        self._finish(w, mark_failed=False)
                if next_index >= len(queue) and all(w.phase in ("idle", "done") for w in workers):
                    break
                self.page.wait_for_timeout(400)
        finally:
            for w in workers:
                try:
                    if w.player:
                        w.player.stop()
                except Exception:
                    pass
                try:
                    w.page.close()
                except Exception:
                    pass
        self.log.info("本课程未完成视频处理结束")
        self._run_homework()

    def _run_homework(self) -> None:
        if not getattr(self.settings, "answer_homework", True):
            self.log.info("已关闭作业答题（--no-homework）")
            return
        if not self.solver.enabled:
            self.log.info("答题接口未启用，跳过作业（填写 [answer] api_key 后生效）")
            return
        from .runner_homework import HomeworkRunner

        self.log.info("开始处理「未完成 → 作业」")
        HomeworkRunner(self.app, self.page_url, self.classroom_id).run()

    # ---------------- 课程页 / 列表 ----------------
    @staticmethod
    def _parse_classroom(url: str) -> str:
        m = re.search(r"/studentLog/(\d+)", url) or re.search(r"[?&]classroom_id=(\d+)", url)
        return m.group(1) if m else ""

    def _course_url_with_tab(self) -> str:
        url = self.page_url
        if "tab=" in url:
            return re.sub(r"([?&])tab=[^&]*", r"\1tab=unfinished", url)
        return url + ("&" if "?" in url else "?") + "tab=unfinished"

    def _goto_course(self) -> None:
        try:
            if "/v2/web/studentLog/" not in (self.page.url or "") or not has_unfinished_tab(self.page):
                self.log.info("打开课程页…")
                self.page.goto(self.page_url, wait_until="domcontentloaded")
                self.page.wait_for_timeout(3000)
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"打开课程页失败：{exc}")
        if not has_unfinished_tab(self.page):
            raise RuntimeError("当前课程页没有「未完成」标签，请确认页面版本")

    def _open_unfinished(self) -> None:
        try:
            tab = self.page.locator(".rain-tabs__nav-item").filter(has_text=TAB_TEXT).first
            if tab.count() > 0:
                if "rain-tabs__nav-item--active" not in (tab.get_attribute("class") or ""):
                    tab.click()
                self.page.wait_for_timeout(1800)
        except Exception as exc:  # noqa: BLE001
            self.log.debug(f"打开未完成标签失败：{exc}")
        try:
            timeout_tab = self.page.locator(".unfinished-study__timeout-tab").filter(has_text="未超时").first
            if timeout_tab.count() > 0:
                timeout_tab.click()
                self.page.wait_for_timeout(600)
        except Exception:
            pass
        self._select_video_filter()
        for _ in range(12):
            try:
                if self.page.locator(".unfinished-study__type-tab").first.count() > 0:
                    break
            except Exception:
                pass
            self.page.wait_for_timeout(500)

    def _select_video_filter(self) -> None:
        for _ in range(8):
            try:
                tabs = self.page.locator(".unfinished-study__type-tab")
                for i in range(tabs.count()):
                    tab = tabs.nth(i)
                    text = (tab.inner_text() or "").strip()
                    if VIDEO_TEXT in text:
                        if "unfinished-study__type-tab--active" not in (tab.get_attribute("class") or ""):
                            tab.click()
                            self.page.wait_for_timeout(1000)
                        # 确认真的切到了视频筛选，别把作业当成视频刷
                        active = self.page.locator(".unfinished-study__type-tab--active").first
                        if active.count() > 0 and VIDEO_TEXT in (active.inner_text() or ""):
                            return
            except Exception as exc:  # noqa: BLE001
                self.log.debug(f"选择视频筛选失败：{exc}")
            self.page.wait_for_timeout(800)
        self.log.warning("未能确认切换到「视频」筛选，请检查页面")

    def _ensure_unfinished(self) -> None:
        try:
            if "/v2/web/studentLog/" not in (self.page.url or "") or not has_unfinished_tab(self.page):
                self.page.goto(self._course_url_with_tab(), wait_until="domcontentloaded")
                self.page.wait_for_timeout(2500)
            self._open_unfinished()
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"刷新未完成列表失败：{exc}")

    def _read_items(self) -> list[dict]:
        for _ in range(3):
            try:
                self.page.evaluate(
                    "() => { const l = document.querySelector('.unfinished-study__list'); if (l) l.scrollTop = l.scrollHeight; }"
                )
            except Exception:
                pass
            self.page.wait_for_timeout(400)
        try:
            items = self.page.evaluate(
                """() => [...document.querySelectorAll('li.unfinished-study__item')].map(li => ({
                  key: (li.querySelector('.unfinished-study__item-title')?.getAttribute('data-title-key') || '').trim(),
                  title: (li.querySelector('.unfinished-study__item-title')?.innerText || '').trim(),
                  type: (li.querySelector('.unfinished-study__type-text')?.innerText || '').trim(),
                  status: (li.querySelector('.unfinished-study__item-status')?.innerText || '').trim(),
                  time: (li.querySelector('.unfinished-study__item-time')?.innerText || '').replace(/\\s+/g, ' ').trim(),
                  overdue: !!li.querySelector('.unfinished-study__item-time--deadline-overdue')
                })).filter(x => x.title && !x.overdue)"""
            ) or []
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"读取未完成列表失败：{exc}")
            return []
        # 只保留视频，防止页面还停留在作业筛选时把作业塞进视频队列
        return [x for x in items if VIDEO_TEXT in (x.get("type") or "")]

    # ---------------- 视频地址 / 完成度 ----------------
    def _video_url(self, item: dict) -> str | None:
        key = (item.get("key") or "").strip()
        if not key or not self.classroom_id:
            return None
        return f"{self.origin}/ai-workspace/lms-graph/{self.classroom_id}/video/{key}?is_chapter=1"

    def _is_done(self, page) -> bool:
        try:
            text = page.evaluate(js.TOP_PROGRESS) or ""
        except Exception:
            return False
        return ("100%" in text) or ("已完成" in text) or ("已学完" in text)

    # ---------------- 调度 ----------------
    def _assign(self, w: Worker, item: dict) -> None:
        url = self._video_url(item)
        if not url:
            self.log.warning(f"无法构造视频地址，跳过：{item.get('title')}")
            w.phase = "idle"
            return
        w.item = item
        w.url = url
        w.attempts = self.state.attempts(self.course_url, f"U|{item['key']}")
        w.load_retries = 0
        self.log.info(f"[W{w.index}] 开始：{item.get('title')}（列表状态 {item.get('status') or '未完成'}）")
        self._load(w, use_nav=True)

    def _load(self, w: Worker, use_nav: bool = False) -> None:
        assert w.item is not None
        try:
            if w.player:
                w.player.stop()
            clicked = False
            if use_nav:
                # 优先点击视频页左侧目录里的下一个视频，不返回上级列表
                try:
                    if w.page.locator(".leaf-item").count() > 0:
                        leaf = w.page.locator(".leaf-item", has_text=w.item["title"]).first
                        if leaf.count() > 0:
                            leaf.click()
                            w.page.wait_for_timeout(1500)
                            if str(w.item.get("key")) in (w.page.url or ""):
                                clicked = True
                                self.log.debug(f"[W{w.index}] 通过左侧目录切换到：{w.item['title']}")
                except Exception as exc:  # noqa: BLE001
                    self.log.debug(f"左侧目录点击失败，改用地址跳转：{exc}")
            if not clicked:
                w.page.goto(w.url, wait_until="domcontentloaded", timeout=60000)
                w.page.wait_for_timeout(1200)
            w.player = Player(w.page, self.settings, self.log)
            w.player.inject()
            w.phase = "loading"
            w.deadline = time.time() + 30
            w.last_current = -1
            w.last_move = time.time()
        except Exception as exc:  # noqa: BLE001
            self._network_retry(w, f"打开视频页失败：{exc}")

    def _step(self, w: Worker) -> None:
        if w.phase in ("idle", "done") or w.player is None or w.item is None:
            return
        now = time.time()
        if w.phase == "loading":
            st = w.player.status()
            if st.get("error"):
                self._network_retry(w, st["error"].get("message") or "视频加载失败")
                return
            if st.get("found") and (st.get("ready") or 0) >= 2 and (st.get("duration") or 0) > 0:
                if self._is_done(w.page):
                    self._success(w, "进入时已完成")
                    return
                # 要求：进入视频先看状态；未完成则直接从 0 开始完整播放
                w.attempts += 1
                self.state.add_attempt(self.course_url, f"U|{w.item['key']}")
                w.player.seek_start()
                w.player.start()
                w.phase = "playing"
                w.last_current = -1
                w.last_move = now
                return
            if now > w.deadline:
                self._network_retry(w, "等待视频加载超时")
            return

        if w.phase == "playing":
            st = w.player.status()
            if st.get("error"):
                self._play_retry(w, st["error"].get("message") or "播放出错")
                return
            if not st.get("found"):
                return
            duration = float(st.get("duration") or 0)
            current = float(st.get("current") or 0)
            if current > w.last_current + 0.2:
                w.last_current = current
                w.last_move = now
            if st.get("ended") or (duration > 1 and current > 0 and duration - current <= 1):
                self.log.info(f"[W{w.index}] 播完：{w.item.get('title')}，检测完成度…")
                w.phase = "checking"
                w.check_started = now
                return
            if now - w.last_move > self.settings.stall_timeout:
                self._play_retry(w, "播放卡住")
            return

        if w.phase == "checking":
            if self._is_done(w.page):
                self._success(w, "播完检测已完成")
                return
            if now - w.check_started >= self.settings.completion_timeout:
                self._play_retry(w, "播完仍未完成")
            return

    # ---------------- 重试 / 结束 ----------------
    def _network_retry(self, w: Worker, reason: str) -> None:
        if w.load_retries < 2:
            w.load_retries += 1
            self.log.warning(f"[W{w.index}] {w.item.get('title')}：{reason}；重新打开（{w.load_retries}/2）")
            self.page.wait_for_timeout(2000)
            self._load(w)
            return
        self.log.error(f"[W{w.index}] {w.item.get('title')}：视频加载失败（CDN/网络），跳过")
        self._finish(w, mark_failed=False)

    def _play_retry(self, w: Worker, reason: str) -> None:
        assert w.item is not None
        if w.attempts < self.settings.replay_attempts:
            self.log.warning(
                f"[W{w.index}] {w.item.get('title')}：{reason}，从头完整重播"
                f"（第 {w.attempts + 1}/{self.settings.replay_attempts} 次）"
            )
            self._load(w)
            return
        self.log.warning(f"[W{w.index}] {w.item.get('title')}：重播次数用完仍未完成，记录失败")
        self._finish(w, mark_failed=True)

    def _success(self, w: Worker, reason: str) -> None:
        assert w.item is not None
        self.state.clear_item(self.course_url, f"U|{w.item['key']}")
        self.log.info(f"[W{w.index}] ✅ {w.item.get('title')}：{reason}")
        self._finish(w, mark_failed=False)

    def _finish(self, w: Worker, mark_failed: bool) -> None:
        if w.item is not None and mark_failed:
            self.state.mark_failed(self.course_url, f"U|{w.item['key']}", "仍显示未完成")
        try:
            if w.player:
                w.player.stop()
        except Exception:
            pass
        w.item = None
        w.player = None
        w.phase = "idle"
