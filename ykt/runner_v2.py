# -*- coding: utf-8 -*-
"""雨课堂 v2/web 路线（课程目录 /studentLog/<id>）。

对应油猴脚本 V2Runner，并增加：
- 播完后校验平台完成状态；
- 未完成则从头重播（刷新 + 拖回 0）；
- 失败次数持久化，避免死循环。
"""
from __future__ import annotations

import time

from . import js
from .player import MediaUnavailable
from .runner import MaxItemsReached, RunnerBase, StopRequested, is_done_text, normalize_url


def exc_message(exc: Exception) -> str:
    return str(exc) or exc.__class__.__name__


class V2Runner(RunnerBase):
    def __init__(self, app):
        super().__init__(app)
        self.outside = 0
        self.inside = 0

    # ---------------- 主循环 ----------------
    def run(self) -> None:
        self._ensure_list_view()
        # 每次运行都从目录开头重新校验：目录状态是完成与否的最终依据，
        # 也能自动修复旧版本因类型识别错误而跳过的卡片。
        self.outside = 0
        self.inside = 0
        self.log.info("从目录开头校验课程（已完成项会自动跳过）")

        while True:
            self._scroll_bottom()
            count = self._count_items()
            if count <= 0:
                self.log.error("未找到课程列表（.logs-list）。请确认当前页面是课程目录页")
                return
            if self.outside >= count:
                self.log.info("课程已刷完")
                self.state.set_progress(self.course_url, outside=count, inside=0)
                return
            if self.settings.max_items and self.processed >= self.settings.max_items:
                self.log.info(f"已达到 max_items={self.settings.max_items}，停止")
                return

            info = self._item_info(self.outside)
            title = (info or {}).get("title") or f"第 {self.outside + 1} 项"
            if not info:
                self.log.warning(f"第 {self.outside + 1} 项未加载，跳过")
                self._advance_outside()
                continue

            key = self.item_key("O", self.outside, title)
            status = info.get("status", "")
            if is_done_text(status):
                self.log.info(f"已完成，跳过：{title}")
                self._advance_outside()
                continue
            if self.should_skip_failed(key):
                self._advance_outside()
                continue

            self.log.info(f"刷课：第 {self.outside + 1}/{count} 项，类型 {info.get('href') or '未知'}，{title}（状态：{status or '未知'}）")
            try:
                ok = self._dispatch_outside(self.outside, info, key)
            except (StopRequested, MaxItemsReached, MediaUnavailable):
                raise
            except Exception as exc:  # noqa: BLE001
                self.log.warning(f"处理「{title}」出错：{exc}")
                ok = False

            if ok:
                self.mark_done(key)
            else:
                self.handle_failure(key, title)
            self.processed += 1
            self._advance_outside()

    # ---------------- 目录 ----------------
    def _ensure_list_view(self) -> None:
        for _ in range(20):
            try:
                if self.page.locator(".logs-list").first.count() > 0:
                    return
            except Exception:
                pass
            self.wait(1)
        self.log.warning("当前页面没有 .logs-list，等待加载…")
        self.wait(5)
        if self.page.locator(".logs-list").first.count() > 0:
            return
        if "/studentCards/" in (self.page.url or ""):
            self.log.info("当前停留在视频页，正在返回课程目录")
            try:
                self.page.go_back(wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
            self.wait(3)
            if self.page.locator(".logs-list").first.count() > 0:
                self.course_url = normalize_url(self.page.url)
                return
        raise RuntimeError("请打开课程目录页（形如 /v2/web/studentLog/<教室ID>），当前 URL：" + self.page.url)

    def _count_items(self) -> int:
        try:
            return int(self.page.evaluate(js.V2_COUNT) or 0)
        except Exception:
            return 0

    def _item_info(self, index: int) -> dict | None:
        try:
            return self.page.evaluate(js.V2_ITEM_INFO, index)
        except Exception:
            return None

    def _advance_outside(self) -> None:
        self.outside += 1
        self.inside = 0
        self.state.set_progress(self.course_url, outside=self.outside, inside=0)

    def _scroll_bottom(self) -> None:
        frequency = (self.outside + 1) // 20 + 1
        for _ in range(frequency):
            try:
                self.page.evaluate("() => { const el = document.querySelector('.viewContainer'); if (el) el.scrollTop = el.scrollHeight; }")
            except Exception:
                pass
            self.wait(0.8)

    # ---------------- 分派 ----------------
    def _dispatch_outside(self, index: int, info: dict, key: str) -> bool:
        href = info.get("href") or ""
        # 新版目录的“线上学习”卡片没有 .tag 图标，内容全在可展开的批量区里
        if not href and (info.get("subInfo") or info.get("expandable")):
            return self._handle_batch(index, info, key)
        if "shipin" in href:
            return self._handle_video(index, info, key)
        if "piliang" in href:
            return self._handle_batch(index, info, key)
        if "ketang" in href:
            return self._handle_classroom(index, info, key)
        if "kejian" in href:
            return self._handle_courseware(index, info, key)
        if "kaoshi" in href:
            self.log.info("考试区域脚本不会处理，跳过")
            return True
        self.log.info(f"非视频/批量/课件/考试，跳过：{info.get('title')}")
        return True

    def _section_locator(self, index: int):
        wrap = self.page.locator(".logs-list > *").nth(index)
        sec = wrap.locator(".content-box section").first
        if sec.count() == 0:
            sec = wrap.locator("section").first
        return sec

    # ---------------- 视频 ----------------
    def _handle_video(self, index: int, info: dict, key: str) -> bool:
        title = info.get("title") or "视频"
        while True:
            sec = self._section_locator(index)
            if sec.count() == 0:
                self.log.warning("未找到课程节点")
                return False
            work = self.click_maybe_new_page(sec)
            self.wait(3)
            try:
                if not self._wait_player(work):
                    self.log.warning("未找到播放器/进度条，返回")
                    return False
                if work.evaluate(js.DEADLINE_PASSED):
                    self.log.warning("该视频已过考核截止时间，播放也不会增加进度，跳过")
                    return True

                player = self.make_player(work)
                probe = self.progress_factory(work, ".progress-wrap .text")
                ok = player.play_with_replay(
                    probe,
                    label=title,
                    attempts_used=self.state.attempts(self.course_url, key),
                    reload_cb=lambda: work.reload(wait_until="domcontentloaded"),
                    on_retry=lambda n: self.note_attempt(key),
                )
            finally:
                self.back_to_list(work)

            if not ok:
                return False
            # 播放器的完成度可能与服务端不同步：回到目录后再以服务端状态复核一次
            if self._wait_item_done(index, 4.0):
                return True
            if self.state.attempts(self.course_url, key) >= self.settings.replay_attempts:
                self.log.warning("播放结束但目录仍未标记完成，且重播次数已用完")
                return False
            self.log.warning("播放结束但课程目录仍未标记完成，重新进入视频页从头播放")

    def _wait_item_done(self, index: int, timeout: float = 4.0) -> bool:
        deadline = time.time() + timeout
        while True:
            info = self._item_info(index)
            if info and is_done_text(info.get("status", "")):
                return True
            if time.time() >= deadline:
                return False
            self.wait(1)

    def _wait_player(self, page, timeout: float = 15.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if page.locator(".progress-wrap, video, audio").first.count() > 0:
                    return True
            except Exception:
                pass
            self.dismiss_popups(page)
            self.wait(0.6)
        return False

    # ---------------- 批量 ----------------
    def _handle_batch(self, index: int, info: dict, key: str) -> bool:
        self._ensure_batch_expanded(index)
        self.wait(0.5)

        count = int(self.page.evaluate(js.V2_ACTIVITY_COUNT, index) or 0)
        if count <= 0:
            self.log.warning("批量区没有活动项，跳过")
            return True

        idx = self.inside
        self.log.info(f"进入批量区：内部进度 {idx + 1}/{count}")
        while idx < count:
            activity = self.page.evaluate(js.V2_ACTIVITY_INFO, [index, idx])
            if not activity:
                idx += 1
                continue
            title = activity.get("title") or f"活动 {idx + 1}"
            item_key = self.item_key("B", index, idx, title)
            if is_done_text(activity.get("status", "")):
                self.log.info(f"已完成，跳过：{title}")
                idx += 1
                self.state.set_progress(self.course_url, inside=idx)
                continue
            if self.should_skip_failed(item_key):
                idx += 1
                self.state.set_progress(self.course_url, inside=idx)
                continue

            self.log.info(f"批量项 {idx + 1}/{count}：{title}")
            self._ensure_batch_expanded(index)
            try:
                ok = self._dispatch_activity(index, idx, activity, item_key)
            except (StopRequested, MaxItemsReached, MediaUnavailable):
                raise
            except Exception as exc:  # noqa: BLE001
                self.log.warning(f"处理批量项「{title}」出错：{exc}")
                ok = False

            if ok:
                self.mark_done(item_key)
            else:
                self.handle_failure(item_key, title)
            idx += 1
            self.state.set_progress(self.course_url, inside=idx)
            self.processed += 1
            if self.settings.max_items and self.processed >= self.settings.max_items:
                self.log.info(f"已达到 max_items={self.settings.max_items}，停止")
                raise MaxItemsReached("达到 max_items")
        return True

    def _dispatch_activity(self, batch_index: int, idx: int, activity: dict, key: str) -> bool:
        href = activity.get("href") or ""
        tag_text = activity.get("tagText") or ""
        if tag_text == "音频":
            return self._play_activity(batch_index, idx, activity, key, kind="音频")
        if "shipin" in href:
            return self._play_activity(batch_index, idx, activity, key, kind="视频")
        if "tuwen" in href or "taolun" in href:
            return self._view_comment(batch_index, idx, activity, key, "图文" if "tuwen" in href else "讨论")
        if "zuoye" in href:
            return self._handle_homework(batch_index, idx, activity, key)
        self.log.info(f"类型未知，跳过：{activity.get('title')}")
        return True

    def _activity_locator(self, batch_index: int, idx: int):
        box = self.page.locator(".logs-list > *").nth(batch_index)
        return box.locator(".leaf_list__wrap .activity__wrap").nth(idx)

    def _ensure_batch_expanded(self, index: int) -> None:
        """批量区在每次从活动页返回后可能重新折叠，点击前确认展开。"""
        wrap = self.page.locator(".logs-list > *").nth(index)
        leaf = wrap.locator(".leaf_list__wrap").first
        try:
            if leaf.count() > 0 and leaf.is_visible():
                return
        except Exception:
            pass
        try:
            expand = wrap.locator(".sub-info .gray span").first
            if expand.count() > 0 and expand.is_visible():
                expand.click()
                self.wait(1.0)
                self.log.debug("已展开批量区")
        except Exception as exc:  # noqa: BLE001
            self.log.debug(f"展开批量区失败：{exc}")

    def _play_activity(self, batch_index: int, idx: int, activity: dict, key: str, kind: str) -> bool:
        title = activity.get("title") or f"{kind} {idx + 1}"
        from .runner_aiws import play_aiws_media

        load_retries = 2
        while True:
            self._ensure_batch_expanded(batch_index)
            loc = self._activity_locator(batch_index, idx)
            if loc.count() == 0:
                return False
            self.log.info(f"开始播放{kind}：{title}")
            work = self.click_maybe_new_page(loc)
            self.wait(2.5)
            external_aiws = work is not self.page and "/ai-workspace/lms-graph/" in (work.url or "")
            load_ok = True
            ok = False
            media_error = None
            try:
                if not self._wait_player(work):
                    load_ok = False
                elif work.evaluate(js.DEADLINE_PASSED):
                    self.log.warning("已过截止时间，跳过")
                    return True
                else:
                    player = self.make_player(work)
                    if external_aiws:
                        self.log.info("活动在新标签页打开（学习空间），使用叶子状态校验完成度")
                        player.inject()
                        # 先确认视频真的加载出来了（CDN 偶尔会超时），避免白白消耗播放次数
                        load_ok = player.wait_ready(25)
                        if load_ok:
                            ok = play_aiws_media(
                                work, player, title, key, self.course_url, self.state, title_hint=title
                            )
                    else:
                        probe = self.progress_factory(work, ".progress-wrap .text")
                        ok = player.play_with_replay(
                            probe,
                            label=f"{kind}：{title}",
                            attempts_used=self.state.attempts(self.course_url, key),
                            reload_cb=lambda: work.reload(wait_until="domcontentloaded"),
                            on_retry=lambda n: self.note_attempt(key),
                        )
            except MediaUnavailable as exc:  # noqa: BLE001
                media_error = exc
            finally:
                self.back_to_list(work)

            if media_error is not None:
                if load_retries > 0:
                    load_retries -= 1
                    self.log.warning(f"{exc_message(media_error)}，重新打开活动重试…")
                    self.wait(3)
                    continue
                raise media_error

            if not load_ok:
                if load_retries > 0:
                    load_retries -= 1
                    self.log.warning("视频没有加载出来（CDN/网络问题），重新打开活动重试…")
                    self.wait(3)
                    continue
                raise MediaUnavailable(
                    "视频加载失败：无法连接视频 CDN。请检查网络，"
                    "或在 config.ini 的 [browser] proxy 里配置代理后重试"
                )

            if not ok:
                return False
            # 学习空间页面的叶子状态已经确认完成，目录页可能还没刷新，直接算成功
            if external_aiws:
                return True
            if self._wait_activity_done(batch_index, idx, 4.0):
                return True
            if self.state.attempts(self.course_url, key) >= self.settings.replay_attempts:
                self.log.warning(f"{title}：目录仍未标记完成，且重播次数已用完")
                return False
            self.log.warning(f"{title}：播放结束但目录仍未标记完成，重新进入从头播放")

    def _wait_activity_done(self, batch_index: int, idx: int, timeout: float = 4.0) -> bool:
        deadline = time.time() + timeout
        while True:
            self._ensure_batch_expanded(batch_index)
            try:
                info = self.page.evaluate(js.V2_ACTIVITY_INFO, [batch_index, idx])
            except Exception:
                info = None
            if info and is_done_text(info.get("status", "")):
                return True
            if time.time() >= deadline:
                return False
            self.wait(1)

    # ---------------- 图文 / 讨论 ----------------
    def _view_comment(self, batch_index: int, idx: int, activity: dict, key: str, kind: str) -> bool:
        loc = self._activity_locator(batch_index, idx)
        if loc.count() == 0:
            return False
        self.log.info(f"打开{kind}：{activity.get('title')}")
        work = self.click_maybe_new_page(loc)
        self.wait(1.5)
        try:
            if not self.settings.comment_enabled:
                self.log.info(f"{kind}已查看（未开启自动评论）")
                self.wait(0.5)
                return True
            try:
                work.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                self.wait(1)
                work.evaluate("() => window.scrollTo(0, 0)")
            except Exception:
                pass
            first = ""
            deadline = time.time() + 15
            while time.time() < deadline and not first:
                try:
                    first = work.evaluate(js.COMMENT_FIRST) or ""
                except Exception:
                    first = ""
                if not first:
                    self.wait(0.5)
            if not first:
                self.log.warning("没有找到已有评论，跳过")
                return True
            sent = work.evaluate(js.COMMENT_SEND, first)
            self.log.info("已发表评论" if sent else "发送按钮不可用")
            return True
        finally:
            self.back_to_list(work)

    # ---------------- 作业 ----------------
    def _handle_homework(self, batch_index: int, idx: int, activity: dict, key: str) -> bool:
        if not self.solver.enabled:
            self.log.info("未启用答题接口，跳过作业")
            return True
        loc = self._activity_locator(batch_index, idx)
        if loc.count() == 0:
            return False
        self.log.info(f"进入作业：{activity.get('title')}")
        work = self.click_maybe_new_page(loc)
        self.wait(2)
        try:
            if not self._wait_selector(work, ".subject-item, .container-problem", 15):
                self.log.warning("未找到作业题目，返回")
                return True
            count = int(work.evaluate(js.EXERCISE_TABS) or 0)
            self.log.info(f"共检测到 {count or 1} 题")
            for i in range(max(count, 1)):
                if count:
                    try:
                        tabs = work.locator(".subject-item.J_order")
                        if tabs.count() == 0:
                            tabs = work.locator(".subject-item")
                        tabs.nth(i).click()
                        self.wait(1.5)
                    except Exception:
                        pass
                self.solve_current_question(work, f"第 {i + 1} 题")
            return True
        finally:
            self.back_to_list(work)

    def _wait_selector(self, page, selector: str, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if page.locator(selector).first.count() > 0:
                    return True
            except Exception:
                pass
            self.wait(0.5)
        return False

    # ---------------- 课堂 ----------------
    def _handle_classroom(self, index: int, info: dict, key: str) -> bool:
        sec = self._section_locator(index)
        if sec.count() == 0:
            return False
        self.log.info(f"进入课堂：{info.get('title')}")
        work = self.click_maybe_new_page(sec)
        self.wait(5)
        try:
            frame = None
            deadline = time.time() + 20
            while time.time() < deadline and frame is None:
                for f in work.frames:
                    try:
                        if f.evaluate("() => !!document.querySelector('video, audio')"):
                            frame = f
                            break
                    except Exception:
                        continue
                if frame is None:
                    self.wait(1)
            if frame is None:
                self.log.warning("未找到课堂 iframe 中的音视频，跳过")
                return True
            player = self.make_player(frame)
            player.start()
            if not player.wait_ready(30):
                self.log.warning("课堂媒体未就绪")
                return True
            st = player.status()
            duration = float(st.get("duration") or 0)
            player.wait_end(max(duration * 2.5 + 30, 120))
            player.stop()
            return True
        finally:
            self.back_to_list(work)

    # ---------------- 课件 ----------------
    def _handle_courseware(self, index: int, info: dict, key: str) -> bool:
        sec = self._section_locator(index)
        if sec.count() == 0:
            return False
        title = info.get("title") or "课件"
        self.log.info(f"打开课件：{title}")
        work = self.click_maybe_new_page(sec)
        self.wait(3)
        try:
            try:
                if work.evaluate(js.CHECK_COURSEWARE_BTN):
                    self.log.info("检测到“查看课件”，已点击")
                    self.wait(2)
            except Exception:
                pass

            class_type = ""
            try:
                if work.locator(".el-card__header").first.count() > 0:
                    class_type = work.locator(".el-card__header").first.inner_text()
            except Exception:
                pass

            if "PPT" in class_type:
                slides = int(work.evaluate(js.PPT_SLIDES) or 0)
                self.log.info(f"开始播放 PPT，共 {slides} 页")
                for i in range(slides):
                    if work.evaluate(js.PPT_CLICK, i):
                        self.log.debug(f"PPT 第 {i + 1}/{slides} 页")
                    self.wait(self.settings.ppt_interval)
                self.wait(self.settings.ppt_interval)
                boxes = work.evaluate(js.VIDEO_BOXES) or []
                for box in boxes:
                    if "已完成" in (box.get("text") or ""):
                        self.log.info(f"第 {box.get('i', 0) + 1} 个课件视频已完成，跳过")
                        continue
                    if work.evaluate(js.VIDEO_BOX_CLICK, box.get("i")):
                        self.wait(2)
                        self._play_courseware_video(work, f"{title} 视频{box.get('i', 0) + 1}", key)
                return True
            else:
                box = work.locator(".video-box").first
                if box.count() == 0:
                    self.log.info("课件没有可播放内容，跳过")
                    return True
                box.click()
                self.wait(2)
                return self._play_courseware_video(work, title, key)
        finally:
            self.back_to_list(work)

    def _play_courseware_video(self, work, label: str, key: str) -> bool:
        def probe() -> dict:
            try:
                info = work.evaluate(js.XT_TIME) or {}
            except Exception:
                return {"has": False, "done": False}
            current = info.get("current") or ""
            total = info.get("total") or ""
            has = bool(total) and total not in ("00:00", "0:00")
            return {"has": has, "done": bool(current and total and current == total), "text": info.get("text", "")}

        player = self.make_player(work)
        return player.play_with_replay(
            probe,
            label=label,
            attempts_used=self.state.attempts(self.course_url, key),
            reload_cb=None,
            on_retry=lambda n: self.note_attempt(key),
        )
