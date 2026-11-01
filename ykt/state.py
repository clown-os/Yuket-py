# -*- coding: utf-8 -*-
"""断点续刷与失败计数。

结构：
{
  "courses": {
    "<course key>": {
      "outside": 3,              # v2 目录当前刷到第几集（0 基）
      "inside": 0,               # 批量区内部进度
      "attempts": {"<item>": 2}, # 每个视频已经播放的次数
      "failed": {"<item>": "原因"},
      "done": ["<item>"]
    }
  }
}
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


class StateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data: dict = {"courses": {}}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(self.data.get("courses"), dict):
                self.data = {"courses": {}}
        except Exception:
            self.data = {"courses": {}}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def course(self, url: str) -> dict:
        key = normalize_url(url)
        courses = self.data.setdefault("courses", {})
        return courses.setdefault(
            key, {"outside": 0, "inside": 0, "attempts": {}, "failed": {}, "done": []}
        )

    def reset_course(self, url: str) -> dict:
        key = normalize_url(url)
        courses = self.data.setdefault("courses", {})
        courses[key] = {"outside": 0, "inside": 0, "attempts": {}, "failed": {}, "done": []}
        self.save()
        return courses[key]

    # ---- 单项状态 ----
    def attempts(self, url: str, item: str) -> int:
        return int(self.course(url).get("attempts", {}).get(item, 0))

    def add_attempt(self, url: str, item: str) -> int:
        c = self.course(url)
        n = int(c.setdefault("attempts", {}).get(item, 0)) + 1
        c["attempts"][item] = n
        self.save()
        return n

    def mark_failed(self, url: str, item: str, reason: str = "") -> None:
        c = self.course(url)
        c.setdefault("failed", {})[item] = reason or "未完成"
        self.save()

    def clear_item(self, url: str, item: str) -> None:
        c = self.course(url)
        c.setdefault("attempts", {}).pop(item, None)
        c.setdefault("failed", {}).pop(item, None)
        if item not in c.setdefault("done", []):
            c["done"].append(item)
        self.save()

    def has_failed(self, url: str, item: str) -> bool:
        return item in self.course(url).get("failed", {})

    # ---- 进度 ----
    def get_progress(self, url: str) -> tuple[int, int]:
        c = self.course(url)
        return int(c.get("outside", 0)), int(c.get("inside", 0))

    def set_progress(self, url: str, outside: int | None = None, inside: int | None = None) -> None:
        c = self.course(url)
        if outside is not None:
            c["outside"] = int(outside)
        if inside is not None:
            c["inside"] = int(inside)
        self.save()
