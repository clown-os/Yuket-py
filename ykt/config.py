# -*- coding: utf-8 -*-
"""配置加载。首次运行会把 config_template.ini 复制为 config.ini。"""
from __future__ import annotations

import configparser
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "config_template.ini"


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def _as_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


@dataclass
class Settings:
    # 站点 / 登录
    base_url: str = "https://www.yuketang.cn"
    start_page: str = "/v2/web/index"
    login_timeout: int = 300
    login_selectors: str = ".user-avatar, .avatar, .el-avatar, .user-name, .userInfo, .user-info, .nickname"
    qr_selectors: str = "img[src^='data:image'], canvas, img[src*='qrcode'], img[src*='qr'], .qrcode img"

    # 浏览器
    headless: bool = False
    channel: str = "chrome"
    profile_dir: str = "browser_data"
    proxy: str = ""
    proxy_username: str = ""
    proxy_password: str = ""

    # 播放
    speed: float = 2.0
    mute: bool = True
    ppt_interval: float = 3.0
    start_over: bool = False
    completion_timeout: float = 30.0
    media_timeout: float = 0.0  # 0 = 按视频时长自动计算
    stall_timeout: float = 45.0  # 播放卡住多少秒后判定失败
    workers: int = 4  # 并行播放的标签页数量

    # 重播（本次改良的核心功能）
    replay_enabled: bool = True
    replay_attempts: int = 2
    replay_reload: bool = True
    stop_on_fail: bool = False

    # 答题
    answer_enabled: bool = False
    answer_provider: str = "openai"
    answer_url: str = "https://api.deepseek.com/chat/completions"
    answer_key: str = ""
    answer_model: str = "deepseek-chat"
    answer_auth: str = "bearer"
    answer_timeout: int = 120
    answer_retries: int = 3
    answer_submit: bool = True
    answer_homework: bool = True
    answer_vision: bool = True
    homework_only: bool = False
    homework_index: int = 0  # 1-based；0 = 全部

    # 图文/讨论自动评论
    comment_enabled: bool = False

    # 状态 / 日志
    state_file: str = "state.json"
    log_dir: str = "logs"
    max_items: int = 0



def load_config(path: str | Path = "config.ini") -> configparser.ConfigParser:
    path = Path(path)
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        if TEMPLATE.exists():
            shutil.copy(TEMPLATE, path)
        else:
            path.write_text("", encoding="utf-8")
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(path, encoding="utf-8")
    return cp


def _section(cp: configparser.ConfigParser, name: str) -> dict[str, str]:
    if not cp.has_section(name):
        return {}
    return {k: v for k, v in cp.items(name)}


def build_settings(cp: configparser.ConfigParser, overrides: dict[str, Any] | None = None) -> Settings:
    site = _section(cp, "site")
    browser = _section(cp, "browser")
    playback = _section(cp, "playback")
    replay = _section(cp, "replay")
    answer = _section(cp, "answer")
    comment = _section(cp, "comment")
    state = _section(cp, "state")
    log = _section(cp, "log")

    s = Settings(
        base_url=(site.get("base_url") or "https://www.yuketang.cn").rstrip("/"),
        start_page=site.get("start_page", "/v2/web/index") or "/v2/web/index",
        login_timeout=_as_int(site.get("login_timeout"), 300),
        login_selectors=site.get("login_selectors")
        or ".user-avatar, .avatar, .el-avatar, .user-name, .userInfo, .user-info, .nickname",
        qr_selectors=site.get("qr_selectors")
        or "img[src^='data:image'], canvas, img[src*='qrcode'], img[src*='qr'], .qrcode img",
        headless=_as_bool(browser.get("headless"), False),
        channel=(browser.get("channel") or "chrome").strip(),
        profile_dir=browser.get("profile_dir", "browser_data") or "browser_data",
        proxy=(browser.get("proxy") or "").strip(),
        proxy_username=(browser.get("proxy_username") or "").strip(),
        proxy_password=(browser.get("proxy_password") or "").strip(),
        speed=_as_float(playback.get("speed"), 2.0),
        mute=_as_bool(playback.get("mute"), True),
        ppt_interval=_as_float(playback.get("ppt_interval"), 3.0),
        start_over=_as_bool(playback.get("start_over"), False),
        completion_timeout=_as_float(playback.get("completion_timeout"), 30.0),
        media_timeout=_as_float(playback.get("media_timeout"), 0.0),
        stall_timeout=_as_float(playback.get("stall_timeout"), 45.0),
        workers=max(1, min(8, _as_int(playback.get("workers"), 4))),
        replay_enabled=_as_bool(replay.get("enabled"), True),
        replay_attempts=max(1, _as_int(replay.get("max_attempts"), 2)),
        replay_reload=_as_bool(replay.get("reload_on_retry"), True),
        stop_on_fail=_as_bool(replay.get("stop_on_fail"), False),
        answer_enabled=_as_bool(answer.get("enabled"), False),
        answer_provider=(answer.get("provider") or "openai").strip().lower(),
        answer_url=(answer.get("api_url") or "").strip(),
        answer_key=(answer.get("api_key") or "").strip(),
        answer_model=(answer.get("model") or "deepseek-chat").strip(),
        answer_auth=(answer.get("auth") or "bearer").strip().lower(),
        answer_timeout=_as_int(answer.get("timeout"), 120),
        answer_retries=max(1, _as_int(answer.get("max_retries"), 3)),
        answer_submit=_as_bool(answer.get("auto_submit"), True),
        answer_homework=_as_bool(answer.get("homework"), True),
        answer_vision=_as_bool(answer.get("vision"), True),
        comment_enabled=_as_bool(comment.get("enabled"), False),
        state_file=state.get("file", "state.json") or "state.json",
        log_dir=log.get("dir", "logs") or "logs",
        max_items=_as_int(playback.get("max_items"), 0),
    )

    # 命令行覆盖
    for key, value in (overrides or {}).items():
        if value is not None:
            setattr(s, key, value)

    # 路径统一解析到项目根目录
    s.profile_dir = str(_resolve(s.profile_dir))
    s.state_file = str(_resolve(s.state_file))
    s.log_dir = str(_resolve(s.log_dir))
    return s


def _resolve(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else ROOT / p


def answer_configured(s: Settings) -> bool:
    key = (s.answer_key or "").strip()
    if not s.answer_enabled:
        return False
    if s.answer_provider == "custom":
        return bool(s.answer_url and key != "")
    return bool(s.answer_url and key and "xxxx" not in key.lower())


def start_url(s: Settings) -> str:
    page = s.start_page or "/v2/web/index"
    if page.startswith("http://") or page.startswith("https://"):
        return page
    return s.base_url + ("" if page.startswith("/") else "/") + page
