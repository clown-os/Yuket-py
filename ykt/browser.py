# -*- coding: utf-8 -*-
"""Playwright 浏览器会话 + 微信扫码登录。

登录态保存在 browser_data/（持久化 profile），扫码一次后可复用。
"""
from __future__ import annotations

import ctypes
import os
import time
from pathlib import Path

from playwright.sync_api import BrowserContext, Page, TimeoutError as PWTimeoutError, sync_playwright

from .config import Settings, start_url

LOGIN_URL_MARKERS = ("/login", "/passport", "/web?next=", "/web/?next=", "type=3")
COURSE_PAGE_SELECTORS = (".logs-list", ".viewContainer", ".leaf-detail", ".nav-item-leaf-box")


def is_login_url(url: str) -> bool:
    return any(marker in (url or "") for marker in LOGIN_URL_MARKERS)

INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
"""


class BrowserSession:
    def __init__(self, settings: Settings, log):
        self.settings = settings
        self.log = log
        self._pw = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._lock_path: Path | None = None

    # ---------------- 生命周期 ----------------
    def __enter__(self) -> "BrowserSession":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def start(self) -> None:
        s = self.settings
        profile = Path(s.profile_dir)
        profile.mkdir(parents=True, exist_ok=True)
        self._acquire_lock(profile)
        self._pw = sync_playwright().start()

        args = [
            "--autoplay-policy=no-user-gesture-required",
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if s.mute:
            args.append("--mute-audio")

        kwargs = dict(
            user_data_dir=str(profile),
            headless=bool(s.headless),
            args=args,
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            accept_downloads=False,
        )
        if s.proxy:
            proxy = {"server": s.proxy}
            if s.proxy_username:
                proxy["username"] = s.proxy_username
            if s.proxy_password:
                proxy["password"] = s.proxy_password
            kwargs["proxy"] = proxy
            self.log.info(f"使用浏览器代理：{s.proxy}")

        context = None
        if s.channel:
            try:
                context = self._pw.chromium.launch_persistent_context(channel=s.channel, **kwargs)
            except Exception as exc:  # noqa: BLE001
                self.log.debug(f"以 {s.channel} 启动失败，尝试 Playwright 自带 Chromium：{exc}")

        if context is None:
            try:
                context = self._pw.chromium.launch_persistent_context(**kwargs)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    "无法启动浏览器。请先执行：\n"
                    "  python -m playwright install chromium\n"
                    "或在配置里把 browser.channel 改成 chrome / msedge。"
                ) from exc

        context.add_init_script(INIT_SCRIPT)
        context.set_default_timeout(20000)
        context.set_default_navigation_timeout(60000)

        self.context = context
        pages = context.pages
        self.page = pages[0] if pages else context.new_page()
        self.page.on("dialog", self._on_dialog)
        self.log.debug(f"浏览器已启动，profile: {profile}")

    def close(self) -> None:
        try:
            if self.context is not None:
                self.context.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        try:
            if self._lock_path and self._lock_path.exists():
                self._lock_path.unlink()
        except Exception:
            pass

    # ---------------- 单实例锁 ----------------
    def _acquire_lock(self, profile: Path) -> None:
        lock = profile / ".ykt.lock"
        if lock.exists():
            try:
                pid = int(lock.read_text(encoding="utf-8").strip() or "0")
            except Exception:
                pid = 0
            if pid and self._pid_alive(pid):
                raise RuntimeError(
                    f"另一个刷课实例正在运行（PID {pid}）。请先关闭那个窗口/进程，再启动。"
                )
            try:
                lock.unlink()
            except Exception:
                pass
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        self._lock_path = lock

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if os.name == "nt":
            try:
                handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
                if not handle:
                    return False
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            except Exception:
                return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    @staticmethod
    def _on_dialog(dialog) -> None:
        try:
            if dialog.type in ("beforeunload", "alert", "confirm"):
                dialog.accept()
            else:
                dialog.dismiss()
        except Exception:
            pass

    # ---------------- 导航 ----------------
    def goto(self, url: str, wait_until: str = "domcontentloaded") -> Page:
        assert self.page is not None
        self.page.goto(url, wait_until=wait_until)
        self._settle(2.0)
        return self.page

    def _settle(self, seconds: float = 2.0) -> None:
        try:
            self.page.wait_for_timeout(int(seconds * 1000))
        except Exception:
            time.sleep(seconds)

    # ---------------- 登录 ----------------
    def ensure_login(self, target_url: str | None = None) -> Page:
        s = self.settings
        url = target_url or start_url(s)
        try:
            self.goto(url)
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"打开页面失败：{exc}")

        if self._logged_in():
            self.log.info("登录状态有效，跳过扫码")
            return self.page

        self.log.info("未登录：正在打开登录页，请使用微信扫码（也可以让程序自动保存二维码）")
        self._try_open_login_entry()
        deadline = time.time() + s.login_timeout
        saved_qr = False
        last_hint = 0.0
        while time.time() < deadline:
            if self._logged_in():
                self.log.info("登录成功")
                return self.page
            if s.headless and not saved_qr:
                saved_qr = self._save_qr()
            now = time.time()
            if now - last_hint > 15:
                remain = int(deadline - now)
                self.log.info(f"等待微信扫码中…剩余 {remain} 秒")
                last_hint = now
            self.page.wait_for_timeout(2000)

        # 超时后给一次人工确认的机会
        if not s.headless:
            try:
                input("未检测到登录成功。若你已在浏览器中登录，按回车继续；直接回车表示放弃。")
            except EOFError:
                pass
            if self._logged_in():
                self.log.info("登录成功")
                return self.page
        raise RuntimeError("登录超时。请重新运行，并确保在浏览器窗口中完成微信扫码。")

    def _try_open_login_entry(self) -> None:
        """部分学校首页不会自动弹登录框，尝试点击“登录/微信登录”。"""
        assert self.page is not None
        try:
            url = self.page.url
            if "/login" in url:
                # /login/weixinapp 在桌面浏览器会提示“请在微信客户端打开”，回到站点首页
                if "weixinapp" in url:
                    self.log.info("当前为微信内登录页，桌面无法展示二维码，返回站点首页")
                    self.page.goto(
                        self.settings.base_url + self.settings.start_page,
                        wait_until="domcontentloaded",
                    )
                    self.page.wait_for_timeout(2000)
        except Exception:
            pass

        texts = ["微信登录", "扫码登录", "微信扫码登录", "登录"]
        for text in texts:
            try:
                loc = self.page.get_by_text(text, exact=True)
                count = min(loc.count(), 3)
                for i in range(count):
                    item = loc.nth(i)
                    if item.is_visible():
                        item.click(timeout=2000)
                        self.page.wait_for_timeout(1200)
                        self.log.info(f"已点击登录入口：{text}")
                        return
            except Exception:
                continue

    def _save_qr(self) -> bool:
        """无头模式：把二维码区域截图到项目根目录 qrcode.png。"""
        assert self.page is not None
        selectors = [x.strip() for x in self.settings.qr_selectors.split(",") if x.strip()]
        for selector in selectors:
            try:
                loc = self.page.locator(selector).first
                if loc.is_visible():
                    loc.screenshot(path="qrcode.png")
                    self.log.info("二维码已保存到 qrcode.png，请用微信扫码")
                    return True
            except Exception:
                continue
        try:
            self.page.screenshot(path="qrcode.png", full_page=True)
            self.log.info("未定位到二维码元素，已保存整页截图 qrcode.png")
            return True
        except Exception:
            return False

    def _logged_in(self) -> bool:
        assert self.page is not None
        page = self.page
        try:
            url = page.url or ""
        except Exception:
            return False

        # 0) 明确的登录页直接判定为未登录，避免被登录页上的占位头像误导
        if any(marker in url for marker in LOGIN_URL_MARKERS):
            return False
        try:
            title = page.title() or ""
            if "登录" in title and "雨课堂" in title:
                return False
        except Exception:
            pass

        # 1) 页面元素
        selectors = [x.strip() for x in self.settings.login_selectors.split(",") if x.strip()]
        for selector in selectors:
            try:
                if page.locator(selector).first.is_visible(timeout=500):
                    return True
            except Exception:
                continue

        # 2) 课程页特征（登录后才会出现）
        for selector in COURSE_PAGE_SELECTORS:
            try:
                if page.locator(selector).first.count() > 0:
                    return True
            except Exception:
                continue

        # 3) localStorage 中的登录凭据
        try:
            storage = page.evaluate(
                """() => {
                  const out = {};
                  for (let i = 0; i < localStorage.length; i++) {
                    const k = localStorage.key(i);
                    const v = localStorage.getItem(k);
                    if (v && v.length < 2000) out[k] = v;
                  }
                  return out;
                }"""
            )
            for key, value in (storage or {}).items():
                low = key.lower()
                if any(
                    t in low
                    for t in (
                        "token",
                        "passport",
                        "user_info",
                        "userinfo",
                        "user_id",
                        "userid",
                        "uv_id",
                        "university_id",
                    )
                ):
                    if value and value not in ("null", "undefined", "{}", "[]"):
                        return True
        except Exception:
            pass

        # 4) 登录后才下发的会话 cookie
        try:
            for cookie in page.context.cookies():
                if cookie.get("name") in ("sessionid", "session", "sso_sessionid") and len(cookie.get("value") or "") > 10:
                    return True
        except Exception:
            pass

        # 4) URL 不含登录路由 + 页面有课程入口
        if not any(marker in url for marker in LOGIN_URL_MARKERS):
            try:
                body = page.locator("body").inner_text(timeout=1000)
                if "退出登录" in body or "我的课程" in body:
                    return True
            except Exception:
                pass
        return False
