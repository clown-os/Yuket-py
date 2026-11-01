# -*- coding: utf-8 -*-
"""课程中心：读取课程列表、按名称进入课程。"""
from __future__ import annotations

import time

from .config import Settings

INDEX_PATH = "/v2/web/index"
COURSE_URL_MARKERS = ("/v2/web/studentLog/", "/v2/web/studentContent/", "/pro/lms/", "/ai-workspace/lms-graph/")


def index_url(settings: Settings) -> str:
    return settings.base_url + INDEX_PATH


def list_courses(page, settings: Settings, log) -> list[dict]:
    page.goto(index_url(settings), wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    # 课程卡片是懒加载的，等待出现
    for _ in range(15):
        try:
            if page.locator(".lesson-cardS").first.count() > 0:
                break
        except Exception:
            pass
        page.wait_for_timeout(500)

    courses = page.evaluate(
        """() => [...document.querySelectorAll('.lesson-cardS')].map((el, i) => {
          const name = (el.querySelector('h1')?.innerText || '').trim();
          const text = (el.innerText || '').replace(/\\s+/g, ' ').trim();
          return { index: i, name, text };
        }).filter(x => x.name)"""
    ) or []
    log.info(f"课程中心共 {len(courses)} 门课程")
    return courses


def choose_courses(courses: list[dict], log) -> list[dict]:
    if not courses:
        return []
    print()
    print("=" * 72)
    print("请选择要刷的课程（可多选）")
    print("=" * 72)
    for i, course in enumerate(courses, 1):
        print(f"  [{i:>2}] {course['text']}")
    print("=" * 72)
    print("输入编号或课程名，多个用逗号分隔，例如：1,3 或 数据结构与算法；直接回车表示全部。")
    try:
        raw = input("课程编号：").strip()
    except EOFError:
        return []
    if not raw:
        return courses
    picked: list[dict] = []
    seen: set[str] = set()
    for part in raw.replace("，", ",").replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            n = int(part)
            if 1 <= n <= len(courses):
                course = courses[n - 1]
                if course["name"] not in seen:
                    seen.add(course["name"])
                    picked.append(course)
            else:
                log.warning(f"忽略无效编号：{part}")
            continue
        except ValueError:
            pass
        matches = [c for c in courses if part in c["name"] or part in c["text"]]
        if len(matches) == 1:
            course = matches[0]
            if course["name"] not in seen:
                seen.add(course["name"])
                picked.append(course)
        elif len(matches) > 1:
            log.warning(f"课程名「{part}」匹配到多门，请改用编号")
        else:
            log.warning(f"找不到课程：{part}")
    if not picked:
        log.warning("没有选择有效课程")
    return picked


def open_course_by_name(page, settings: Settings, course: dict, log):
    """回到课程中心，按课程名点击卡片，返回 (课程页, URL)；失败返回 (None, None)。"""
    page.goto(index_url(settings), wait_until="domcontentloaded")
    page.wait_for_timeout(3500)
    name = course["name"]
    cards = page.locator(".lesson-cardS")
    deadline = time.time() + 15
    card = None
    while time.time() < deadline:
        count = cards.count()
        for i in range(count):
            try:
                title = (cards.nth(i).locator("h1").first.inner_text() or "").strip()
            except Exception:
                continue
            if title != name:
                continue
            try:
                card_text = (cards.nth(i).inner_text() or "").replace("\n", " ").strip()
            except Exception:
                card_text = ""
            if course.get("text") and course["text"] in card_text.replace("  ", " "):
                card = cards.nth(i)
                break
            if card is None:
                card = cards.nth(i)  # 同名课程时退化为第一个同名卡片
        if card is not None:
            break
        page.wait_for_timeout(500)

    if card is None:
        log.error(f"课程中心里找不到课程：{name}")
        return None, None

    before_pages = set(page.context.pages)
    before_url = page.url
    card.click()
    deadline = time.time() + 20
    while time.time() < deadline:
        new_pages = [p for p in page.context.pages if p not in before_pages]
        if new_pages:
            target = new_pages[-1]
            try:
                target.wait_for_load_state("domcontentloaded", timeout=20000)
            except Exception:
                pass
            target.wait_for_timeout(2000)
            return target, target.url
        if page.url != before_url and any(m in page.url for m in COURSE_URL_MARKERS):
            page.wait_for_timeout(2000)
            return page, page.url
        page.wait_for_timeout(500)
    log.error(f"进入课程失败：{name}")
    return None, None
