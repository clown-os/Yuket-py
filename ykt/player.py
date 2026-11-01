# -*- coding: utf-8 -*-
"""视频/音频播放控制与“播完但未上报完成”的重播逻辑。

重播策略（对应需求 3）：
  1. 视频自然播放到结尾；
  2. 在 completion_timeout 秒内轮询平台“是否完成”的标记；
  3. 若始终未完成，则刷新页面（或直接把进度拖回 0），从头再播；
  4. 每个视频最多播放 replay_attempts 次，仍失败则记录并继续下一项。
"""
from __future__ import annotations

import time
from typing import Callable

from . import js
from .config import Settings

# play_once 的返回值
DONE = "done"
UNVERIFIED = "unverified"  # 页面没有完成度标记，无法验证；按完成处理，避免死循环
UNCONFIRMED = "unconfirmed"  # 有标记但超时未变成完成 -> 需要重播
NO_MEDIA = "no_media"
TIMEOUT = "timeout"
NETWORK = "network"


class MediaUnavailable(RuntimeError):
    """视频源无法加载（CDN/网络），不应计入重播次数，也不应标记为已完成。"""


class Player:
    def __init__(self, page, settings: Settings, log):
        self.page = page  # Page 或 Frame
        self.settings = settings
        self.log = log
        self._injected = False
        self._last_inject_url = ""
        self._media_error = None

    # ---------------- 基础 ----------------
    def _wait(self, ms: float) -> None:
        try:
            self.page.wait_for_timeout(int(ms))
        except Exception:
            time.sleep(ms / 1000.0)

    def _eval(self, expression: str, arg=None):
        try:
            if arg is None:
                return self.page.evaluate(expression)
            return self.page.evaluate(expression, arg)
        except Exception:
            return None

    def inject(self) -> None:
        # 控制器本身是幂等的；页面刷新后 window.__ykt 会丢失，所以每次都重新注入
        self._eval(js.MEDIA_CONTROLLER)
        self._injected = True
        self._last_inject_url = getattr(self.page, "url", "")

    def invalidate(self) -> None:
        """页面刷新/跳转后调用。"""
        self._injected = False
        self._last_inject_url = ""
        self._media_error = None

    def status(self) -> dict:
        data = self._eval(
            "() => ({has: !!window.__ykt, st: window.__ykt ? window.__ykt.status() : null})"
        )
        if not data:
            return {"found": False}
        if not data.get("has"):
            # 页面在播放期间刷新过：重新注入并恢复自动播放
            self.inject()
            self._eval(
                js.MEDIA_START,
                {"rate": float(self.settings.speed), "mute": bool(self.settings.mute)},
            )
            return {"found": False}
        return data.get("st") or {"found": False}

    def start(self) -> None:
        self.inject()
        self._eval(
            js.MEDIA_START,
            {"rate": float(self.settings.speed), "mute": bool(self.settings.mute)},
        )
        # 播放器自带倍速 UI 时点一下，部分学校页面会重置 native 倍速
        self._eval(js.MEDIA_CLICK_SPEED, float(self.settings.speed))

    def stop(self) -> None:
        self._eval(js.MEDIA_STOP)
        self._injected = False
        self._last_inject_url = ""

    def _restart_controller(self) -> None:
        self.inject()
        self._eval(js.MEDIA_START, {"rate": float(self.settings.speed), "mute": bool(self.settings.mute)})

    def seek_start(self, attempts: int = 10) -> bool:
        """把播放位置拖回 0；流媒体 seekable 未就绪时会重试。"""
        self.log.info("重新从头播放（进度拖回 00:00）")
        for _ in range(max(1, attempts)):
            self._eval(js.MEDIA_SEEK_START)
            self._wait(300)
            st = self.status()
            if st.get("found") and float(st.get("current") or 0) <= 3:
                return True
        self.log.warning("无法把播放位置拖回开头，本次重播可能从断点继续")
        return False

    def dismiss_popups(self) -> None:
        self._eval(js.DISMISS_POPUPS)

    # ---------------- 等待 ----------------
    def diagnose(self) -> dict:
        data = self._eval(
            """() => {
              const v = document.querySelector('video, audio');
              if (!v) return { none: true, url: location.href };
              return {
                url: location.href,
                src: String(v.currentSrc || v.src || '').slice(0, 160),
                readyState: v.readyState,
                networkState: v.networkState,
                error: v.error ? { code: v.error.code, message: v.error.message } : null,
                currentTime: v.currentTime,
                duration: v.duration,
                paused: v.paused
              };
            }"""
        )
        return data or {}

    def wait_ready(self, timeout: float = 30.0) -> bool:
        self.inject()
        started = time.time()
        deadline = started + timeout
        last_log = 0.0
        nudged = False
        error_logged = False
        while time.time() < deadline:
            st = self.status()
            if st.get("found"):
                err = st.get("error")
                if err:
                    self._media_error = err
                    if not error_logged:
                        self.log.warning(
                            f"视频加载失败：{err.get('message') or err}（检查网络/CDN，或配置 [browser] proxy）"
                        )
                        error_logged = True
                    return False
                if (st.get("ready") or 0) >= 2 and (st.get("duration") or 0) > 0:
                    return True
                if (st.get("networkState") or 0) == 3:
                    if not error_logged:
                        self.log.warning("视频没有可用数据源（networkState=NO_SOURCE）")
                        error_logged = True
            elapsed = time.time() - started
            if not nudged and elapsed > 5:
                # 有些页面要等自定义播放器初始化；点一下播放按钮/视频区域触发加载
                self._eval(
                    """() => {
                      const btn = document.querySelector('.xt_video_bit_play_btn');
                      if (btn && (btn.offsetWidth || btn.offsetHeight)) { btn.click(); return true; }
                      const v = document.querySelector('video');
                      if (v) { try { v.click(); v.play().catch(() => {}); } catch (e) {} }
                      return false;
                    }"""
                )
                nudged = True
            if time.time() - last_log > 5:
                self.log.info(f"等待播放器加载…（{int(elapsed)} 秒）")
                last_log = time.time()
            self._wait(500)
        return False

    def wait_end(self, limit: float) -> bool:
        """等待播放到结尾。返回 True=已结束，False=超时。"""
        start = time.time()
        last_log = 0.0
        last_pct = -1
        last_current = -1.0
        last_move = time.time()
        while time.time() - start < limit:
            st = self.status()
            if not st.get("found"):
                # 播放器可能被关闭/切换，短暂等待后继续
                self._wait(500)
                continue
            if st.get("error"):
                self._media_error = st["error"]
                self.log.warning(f"视频播放出错：{st['error']}")
                return False
            duration = float(st.get("duration") or 0)
            current = float(st.get("current") or 0)
            if st.get("ended") or (duration > 1 and current > 0 and duration - current <= 1):
                return True
            if duration > 1 and current >= 0:
                pct = int(current * 100 / duration)
                if pct >= last_pct + 10 or (last_pct < 0 and pct > 0):
                    self.log.debug(f"播放进度 {pct}%（{self._fmt(current)} / {self._fmt(duration)}）")
                    last_pct = pct
            now = time.time()
            if current > last_current + 0.2:
                last_current = current
                last_move = now
            if now - last_log > 10:
                self.dismiss_popups()
                if now - last_move > 15:
                    self.log.info(f"等待播放中…当前 {self._fmt(current)} / {self._fmt(duration)}")
                last_log = now
            if now - last_move > max(30.0, self.settings.stall_timeout):
                self.log.warning(f"播放进度超过 {int(now - last_move)} 秒没有前进，判定卡住")
                latest = self.status()
                if latest.get("error"):
                    self._media_error = latest["error"]
                return False
            self._wait(700)
        return False

    def wait_progress(self, probe: Callable[[], dict], timeout: float) -> str:
        """播放结束后等待平台标记完成。"""
        deadline = time.time() + max(3.0, timeout)
        saw_signal = False
        last_text = ""
        while time.time() < deadline:
            state = probe() or {}
            if state.get("has"):
                saw_signal = True
                last_text = str(state.get("text") or "")
                if state.get("done"):
                    return DONE
            self.dismiss_popups()
            self._wait(1000)
        if saw_signal:
            self.log.debug(f"完成标记未变化：{last_text!r}")
            return UNCONFIRMED
        return UNVERIFIED

    # ---------------- 单次播放 ----------------
    def play_once(self, probe: Callable[[], dict], start_over: bool | None = None) -> str:
        self._media_error = None
        self.inject()
        self.start()
        if not self.wait_ready(30):
            detail = self.diagnose()
            self.log.warning(f"播放器没有就绪：{detail}")
            self.stop()
            return NETWORK if (self._media_error or detail.get("error")) else NO_MEDIA

        if start_over is None:
            start_over = self.settings.start_over
        if start_over:
            # 平台按“实际观看时长”累计进度，从断点续播往往不会真正完成
            self.seek_start()

        st = self.status()
        duration = float(st.get("duration") or 0)
        if self.settings.media_timeout > 0:
            limit = float(self.settings.media_timeout)
        else:
            limit = max(duration * 2.5 + 30, 90) if duration > 1 else 180
            limit = min(limit, 4 * 3600)

        ok = self.wait_end(limit)
        if not ok:
            if self._media_error:
                self.stop()
                return NETWORK
            self.log.warning(f"等待播放结束超时（上限 {int(limit)} 秒），可能被暂停或网络中断")
            self.stop()
            return TIMEOUT

        result = self.wait_progress(probe, self.settings.completion_timeout)
        self.stop()
        if result == UNVERIFIED:
            self.log.debug("页面没有完成度标记，按已完成处理")
        return result

    def _fmt(self, seconds: float) -> str:
        seconds = int(max(0, seconds))
        return f"{seconds // 60:02d}:{seconds % 60:02d}"

    # ---------------- 重播 ----------------
    def play_with_replay(
        self,
        probe_factory: Callable[[], dict],
        label: str,
        attempts_used: int = 0,
        reload_cb: Callable[[], None] | None = None,
        on_retry: Callable[[int], None] | None = None,
        start_over: bool | None = None,
    ) -> bool:
        """返回是否已被平台确认完成。

        attempts_used: 该视频此前已经播放过的次数（跨运行、跨目录重进累计）。
        """
        max_attempts = max(1, self.settings.replay_attempts)
        attempt = attempts_used
        while attempt < max_attempts:
            attempt += 1
            result = self.play_once(probe_factory, start_over=start_over)
            if result in (DONE, UNVERIFIED):
                if result == UNVERIFIED:
                    self.log.warning(f"{label}：无法确认完成状态，继续下一项")
                else:
                    self.log.info(f"{label}：已完成")
                return True
            if result == NETWORK:
                # 视频源根本没加载出来：重开页面由上层负责，不能当作“播完了”处理
                raise MediaUnavailable(
                    f"{label}：视频源无法加载（CDN/网络），请检查网络或配置 [browser] proxy"
                )
            if result == NO_MEDIA:
                return False
            # 只有真正开始播放并结束（或卡住）的尝试才计数，避免网络失败白白消耗次数
            if on_retry:
                on_retry(attempt)
            if attempt >= max_attempts:
                break
            if not self.settings.replay_enabled:
                self.log.warning(f"{label}：未检测到完成，且未开启重播")
                return False
            if result == TIMEOUT:
                self.log.warning(
                    f"{label}：播放中断/卡住，准备重新打开播放器重试"
                    f"（第 {attempt + 1}/{max_attempts} 次）"
                )
            else:
                self.log.warning(
                    f"{label}：视频已播完但平台未记为完成，准备从头重播"
                    f"（第 {attempt + 1}/{max_attempts} 次）"
                )
            if reload_cb and self.settings.replay_reload:
                try:
                    reload_cb()
                except Exception as exc:  # noqa: BLE001
                    self.log.debug(f"刷新播放页失败：{exc}")
                    self._wait(3000)
                self.invalidate()
                if not self.wait_ready(30):
                    latest = self.diagnose()
                    if self._media_error or latest.get("error") or latest.get("networkState") == 3:
                        raise MediaUnavailable(
                            f"{label}：刷新后视频源无法加载（CDN/网络），请检查网络或配置 [browser] proxy"
                        )
                    self.log.warning(f"{label}：刷新后未找到播放器")
                    continue
                # 刷新期间平台可能已经补上了进度，先确认一次，避免白播
                late = probe_factory() or {}
                if late.get("done"):
                    self.log.info(f"{label}：刷新后已显示完成，无需重播")
                    self.stop()
                    return True
                self.start()
                self.seek_start()
            else:
                self.start()
                self.seek_start()
            self._wait(1500)

        self.log.warning(f"{label}：重播 {max_attempts} 次后仍未完成")
        return False
