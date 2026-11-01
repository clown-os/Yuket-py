# -*- coding: utf-8 -*-
"""「未完成 → 作业」AI 答题（支持视觉模型）。

作业题目在 iframe 内，题干/选项可能是图片或自定义字体：
  - 每道题截取题目容器 -> 发给视觉模型 -> 解析 A/B/C/D -> 点选项；
  - 全部答完点提交；
  - 作业页可能触发雨课堂「安全核验」，工具不会绕过，会在有界面模式下等待你手动完成。
"""
from __future__ import annotations

import base64
import time
from urllib.parse import urlsplit

from .state import normalize_url

SAFE_TEXT = "安全核验"


class HomeworkRunner:
    def __init__(self, app, course_url: str, classroom_id: str):
        self.app = app
        self.settings = app.settings
        self.log = app.log
        self.state = app.state
        self.solver = app.solver
        self.page = app.session.page
        self.page_url = course_url
        self.course_url = normalize_url(course_url)
        parts = urlsplit(course_url)
        self.origin = f"{parts.scheme}://{parts.netloc}"
        self.classroom_id = classroom_id
        self.processed = 0

    # ---------------- 入口 ----------------
    def run(self) -> None:
        if not self.solver.enabled:
            self.log.info("答题接口未启用，跳过作业（请填写 [answer] api_key）")
            return
        self._goto_course()
        self._open_homework_filter()
        items = self._read_items()
        idx = int(getattr(self.settings, "homework_index", 0) or 0)
        if idx > 0:
            items = items[idx - 1 : idx]
        if self.settings.max_items and self.settings.max_items > 0:
            items = items[: self.settings.max_items]
        self.log.info(f"「未完成」列表中共有 {len(items)} 个作业待处理")
        for i, item in enumerate(items, 1):
            key = (item.get("key") or "").strip()
            if not key:
                continue
            ukey = f"H|{key}"
            if self.state.has_failed(self.course_url, ukey):
                self.log.debug(f"跳过此前失败的作业：{item.get('title')}")
                continue
            title = item.get("title") or f"作业 {i}"
            self.log.info(f"[H {i}/{len(items)}] 作业：{title}（{item.get('status') or '未开始'}）")
            self._ensure_homework_list()
            if not self._item_present(key):
                self.log.info("列表中已不存在，跳过")
                continue
            try:
                ok = self._process(item)
            except Exception as exc:  # noqa: BLE001
                self.log.exception(f"处理作业「{title}」出错：{exc}")
                ok = False
            self.processed += 1
            if ok:
                self.state.clear_item(self.course_url, ukey)
                self.log.info(f"[H {i}/{len(items)}] ✅ {title}：已提交")
            else:
                # 作业不做永久失败标记：网络/AI 失败后可以直接重跑
                self.log.warning(f"[H {i}/{len(items)}] {title}：未提交/未完成，保留在列表中")
        self.log.info("作业处理结束")

    # ---------------- 课程页 / 作业列表 ----------------
    def _course_url_with_tab(self) -> str:
        url = self.page_url
        if "tab=" in url:
            import re

            return re.sub(r"([?&])tab=[^&]*", r"\1tab=unfinished", url)
        return url + ("&" if "?" in url else "?") + "tab=unfinished"

    def _goto_course(self) -> None:
        try:
            if "/v2/web/studentLog/" not in (self.page.url or ""):
                self.page.goto(self.page_url, wait_until="domcontentloaded")
                self.page.wait_for_timeout(2500)
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"打开课程页失败：{exc}")

    def _open_homework_filter(self) -> None:
        try:
            tab = self.page.locator(".rain-tabs__nav-item").filter(has_text="未完成").first
            if tab.count() > 0:
                tab.click()
                self.page.wait_for_timeout(1800)
        except Exception:
            pass
        try:
            timeout_tab = self.page.locator(".unfinished-study__timeout-tab").filter(has_text="未超时").first
            if timeout_tab.count() > 0:
                timeout_tab.click()
                self.page.wait_for_timeout(500)
        except Exception:
            pass
        for _ in range(8):
            try:
                tabs = self.page.locator(".unfinished-study__type-tab")
                clicked = False
                for i in range(tabs.count()):
                    tab = tabs.nth(i)
                    if "作业" in (tab.inner_text() or ""):
                        if "unfinished-study__type-tab--active" not in (tab.get_attribute("class") or ""):
                            tab.click()
                            self.page.wait_for_timeout(1000)
                        clicked = True
                        break
                if clicked:
                    active = self.page.locator(".unfinished-study__type-tab--active").first
                    if active.count() > 0 and "作业" in (active.inner_text() or ""):
                        return
            except Exception as exc:  # noqa: BLE001
                self.log.debug(f"选择作业筛选失败：{exc}")
            self.page.wait_for_timeout(800)
        self.log.warning("未能确认切换到「作业」筛选，请检查页面")

    def _ensure_homework_list(self) -> None:
        try:
            self.page.goto(self._course_url_with_tab(), wait_until="domcontentloaded")
            self.page.wait_for_timeout(2200)
            self._open_homework_filter()
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"刷新作业列表失败：{exc}")

    def _read_items(self) -> list[dict]:
        try:
            items = self.page.evaluate(
                """() => [...document.querySelectorAll('li.unfinished-study__item')].map(li => ({
                  key: (li.querySelector('.unfinished-study__item-title')?.getAttribute('data-title-key') || '').trim(),
                  title: (li.querySelector('.unfinished-study__item-title')?.innerText || '').trim(),
                  type: (li.querySelector('.unfinished-study__type-text')?.innerText || '').trim(),
                  status: (li.querySelector('.unfinished-study__item-status')?.innerText || '').trim(),
                  overdue: !!li.querySelector('.unfinished-study__item-time--deadline-overdue')
                })).filter(x => x.title && !x.overdue)"""
            ) or []
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"读取作业列表失败：{exc}")
            return []
        return [x for x in items if "作业" in (x.get("type") or "")]

    def _item_present(self, key: str) -> bool:
        return any((x.get("key") or "") == key for x in self._read_items())

    # ---------------- 单个作业 ----------------
    def _exercise_url(self, key: str) -> str:
        return f"{self.origin}/ai-workspace/lms-graph/{self.classroom_id}/exercise/{key}?is_chapter=1"

    def _process(self, item: dict) -> bool:
        key = (item.get("key") or "").strip()
        url = self._exercise_url(key)
        context = self.app.session.context
        assert context is not None
        page = context.new_page()
        page.set_default_timeout(20000)
        page.on("dialog", self.app.session._on_dialog)
        frame = None
        try:
            self.log.info(f"打开作业页：{url}")
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.bring_to_front()
            frame = self._wait_exercise_frame(page)
            if frame is None:
                self.log.warning("未找到作业 iframe")
                return False
            if not self._wait_question_tabs(frame, page, 30):
                self.log.warning("作业 iframe 里没有找到题目分页（可能被安全核验/网络挡住）")
                return False
            if not self._wait_security(page):
                return False
            ok = self._answer(page, frame, item)
            if not ok:
                return False
            if not self.settings.answer_submit:
                self.log.info("auto_submit=false，已作答但未提交")
                return False
        finally:
            try:
                page.close()
            except Exception:
                pass
            try:
                self.page.bring_to_front()
            except Exception:
                pass

        # 用未完成列表最终确认
        self._ensure_homework_list()
        self.page.wait_for_timeout(1500)
        present = self._item_present(key)
        if present:
            self.log.warning("作业已提交，但「未完成」列表里仍然存在（可能未真正提交/需要老师批阅）")
            return False
        return True

    def _is_submitted(self, frame) -> bool:
        # 只看底部提交按钮：题目内部也可能出现“已提交”字样，不能作为整份作业已提交的依据
        try:
            return bool(
                frame.evaluate(
                    """() => {
                      const btn = [...document.querySelectorAll('button')]
                        .find(b => /提交/.test(b.innerText || ''));
                      return !!(btn && (btn.innerText || '').trim() === '已提交');
                    }"""
                )
            )
        except Exception:
            return False

    def _wait_exercise_frame(self, page):
        for _ in range(60):
            for f in page.frames:
                if "iframe-exercise" in (f.url or ""):
                    return f
            page.wait_for_timeout(500)
        return None

    def _wait_question_tabs(self, frame, page, timeout: float = 30.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if int(frame.evaluate("() => document.querySelectorAll('.subject-item.J_order').length") or 0) > 0:
                    return True
            except Exception:
                pass
            page.wait_for_timeout(500)
        return False

    # ---------------- 安全核验 ----------------
    def _security_visible(self, page, frame=None) -> bool:
        for ctx in (page, frame):
            if ctx is None:
                continue
            try:
                if ctx.get_by_text(SAFE_TEXT).first.is_visible(timeout=300):
                    return True
            except Exception:
                pass
        try:
            el = page.locator("#homework-automation-risk-challenge").first
            if el.count() > 0:
                box = el.bounding_box()
                if box and box.get("width", 0) > 10 and box.get("height", 0) > 10:
                    return True
        except Exception:
            pass
        return False

    def _wait_security(self, page, frame=None, timeout: float = 300.0) -> bool:
        if not self._security_visible(page, frame):
            return True
        if self.settings.headless:
            self.log.error("作业页触发了雨课堂『安全核验』，无头模式无法完成；请用有界面模式运行作业答题")
            return False
        self.log.warning("检测到雨课堂『安全核验』：请在浏览器里完成拖动验证，完成后会自动继续（最多等 5 分钟）")
        page.bring_to_front()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self._security_visible(page, frame):
                self.log.info("安全核验已完成，继续")
                return True
            page.wait_for_timeout(2000)
        self.log.warning("安全核验等待超时，跳过本次作业")
        return False

    # ---------------- 答题 ----------------
    def _answer(self, page, frame, item: dict) -> bool:
        tabs = int(frame.evaluate("() => document.querySelectorAll('.subject-item.J_order').length") or 0)
        if tabs <= 0:
            self.log.warning("未找到题目分页（.subject-item.J_order）")
            return False
        self.log.info(f"共 {tabs} 题，开始 AI 作答")
        ok_count = 0
        problems: list[int] = []

        for i in range(tabs):
            switched = False
            for _attempt in range(3):
                frame.evaluate(
                    "(i) => document.querySelectorAll('.subject-item.J_order')[i]?.click()",
                    i,
                )
                page.wait_for_timeout(700)
                if self._wait_question_number(frame, page, i, 4):
                    switched = True
                    break
            if not switched:
                self.log.warning(f"第 {i + 1} 题：页面没有切换到该题，跳过")
                problems.append(i + 1)
                continue
            if not self._wait_security(page, frame):
                return False
            if self._tab_submitted(frame, i):
                ok_count += 1
                continue

            info = self._question_info(frame)
            if not info or not info.get("ok"):
                self.log.warning(f"第 {i + 1} 题：未找到题目容器，跳过")
                problems.append(i + 1)
                continue

            # 已选答案但还没点“提交”（例如上次只保存未提交）
            if info.get("answered"):
                ok = True
                if self.settings.answer_submit:
                    ok = self._submit_current(page, frame, i)
                if ok:
                    ok_count += 1
                    self.log.info(f"第 {i + 1} 题：已提交")
                else:
                    problems.append(i + 1)
                    self.log.warning(f"第 {i + 1} 题：提交失败")
                continue

            option_count = int(info.get("options") or 0)
            input_count = int(info.get("inputs") or 0)
            if option_count <= 0 and input_count <= 0:
                self.log.warning(f"第 {i + 1} 题：既没有选项也没有输入框，跳过")
                problems.append(i + 1)
                continue

            self._wait_images(frame, page)
            try:
                shot = frame.locator(".container-problem").first.screenshot()
            except Exception as exc:  # noqa: BLE001
                self.log.warning(f"第 {i + 1} 题截图失败：{exc}")
                problems.append(i + 1)
                continue
            b64 = base64.b64encode(shot).decode("ascii")
            text = info.get("text") or ""
            answered_ok = False

            if option_count > 0:
                self.log.info(f"🤖 第 {i + 1} 题：请求视觉模型（选择题，{option_count} 个选项）…")
                indices = self.solver.solve(text, [""] * option_count, screenshot_b64=b64)
                if indices:
                    clicked = frame.evaluate(
                        """(indices) => {
                          const root = document.querySelector('.container-problem .subject-item')
                            || document.querySelector('.container-problem');
                          if (!root) return 0;
                          const lis = [...root.querySelectorAll('.list-inline li, .list-unstyled li')]
                            .filter(li => li.querySelector('input[type=radio], input[type=checkbox]'));
                          let n = 0;
                          for (const idx of indices) {
                            const li = lis[idx];
                            if (!li) continue;
                            const target = li.querySelector('label') || li.querySelector('input') || li;
                            target.click();
                            n++;
                          }
                          return n;
                        }""",
                        indices,
                    )
                    page.wait_for_timeout(400)
                    answered_ok = bool(
                        frame.evaluate(
                            "() => !!document.querySelector('.container-problem input:checked')"
                        )
                    )
                    if clicked:
                        self.log.info(f"第 {i + 1} 题：已选择 {clicked} 个选项")
                if not answered_ok:
                    self.log.warning(f"第 {i + 1} 题：选项未能选中")
            else:
                self.log.info(f"🤖 第 {i + 1} 题：请求视觉模型（填空/主观题，{input_count} 个输入框）…")
                answer = self.solver.solve_fill(text, screenshot_b64=b64)
                if answer:
                    parts = [p.strip() for p in answer.split("|") if p.strip()]
                    if input_count == 1:
                        parts = [answer.replace("|", " ").strip()]
                    if not parts:
                        parts = [answer]
                    filled = frame.evaluate(
                        """(parts) => {
                          const root = document.querySelector('.container-problem .subject-item')
                            || document.querySelector('.container-problem');
                          if (!root) return 0;
                          const inputs = [...root.querySelectorAll(
                            'input:not([type=radio]):not([type=checkbox]), textarea, [contenteditable="true"]'
                          )].filter(el => el.offsetWidth || el.offsetHeight);
                          let n = 0;
                          for (let i = 0; i < inputs.length; i++) {
                            const val = parts[i] !== undefined ? parts[i] : (parts[parts.length - 1] || '');
                            const el = inputs[i];
                            if (el.isContentEditable) {
                              el.innerText = val;
                              el.dispatchEvent(new Event('input', { bubbles: true }));
                            } else {
                              const proto = el.tagName === 'TEXTAREA'
                                ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                              const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                              setter.call(el, val);
                              el.dispatchEvent(new Event('input', { bubbles: true }));
                              el.dispatchEvent(new Event('change', { bubbles: true }));
                            }
                            el.blur();
                            n++;
                          }
                          return n;
                        }""",
                        parts,
                    )
                    page.wait_for_timeout(300)
                    answered_ok = bool(filled) and self._fill_answered(frame)
                    self.log.info(f"第 {i + 1} 题：已填入 {filled} 个输入框")
                else:
                    self.log.warning(f"第 {i + 1} 题：AI 未给出填空答案")

            if not answered_ok:
                problems.append(i + 1)
                continue

            ok = True
            if self.settings.answer_submit:
                ok = self._submit_current(page, frame, i)
            if ok:
                ok_count += 1
                if self.settings.answer_submit:
                    self.log.info(f"第 {i + 1} 题：已提交")
            else:
                problems.append(i + 1)
                self.log.warning(f"第 {i + 1} 题：提交失败")
            page.wait_for_timeout(500)

        self.log.info(f"完成：{ok_count}/{tabs} 题")
        if problems:
            self.log.warning(
                f"有 {len(problems)} 题未完成/未提交（题号 {problems}），未整卷提交。"
            )
            return False
        return True

    def _tab_submitted(self, frame, idx: int) -> bool:
        try:
            return bool(
                frame.evaluate(
                    """(i) => {
                      const t = document.querySelectorAll('.subject-item.J_order')[i];
                      return !!(t && t.querySelector('.status-container .icon-status.success'));
                    }""",
                    idx,
                )
            )
        except Exception:
            return False

    def _submit_current(self, page, frame, idx: int) -> bool:
        """提交当前这道题（雨课堂是每题单独提交，不是整卷一次性提交）。"""
        if self._tab_submitted(frame, idx):
            return True
        if not self._wait_security(page, frame):
            return False
        clicked = frame.evaluate(
            """() => {
              const b = [...document.querySelectorAll('button')]
                .find(x => /提交/.test(x.innerText || '') && !x.disabled);
              if (b) { b.click(); return true; }
              return false;
            }"""
        )
        if not clicked:
            return self._tab_submitted(frame, idx)
        page.wait_for_timeout(800)
        if not self._wait_security(page, frame):
            return False
        # 可能弹出确认框
        for _ in range(8):
            confirmed = False
            for ctx in (page, frame):
                try:
                    if ctx.evaluate(
                        """() => {
                          const bs = [...document.querySelectorAll('button')]
                            .filter(b => /确定|确认/.test(b.innerText || '') && !b.disabled);
                          if (bs.length) { bs[0].click(); return true; }
                          return false;
                        }"""
                    ):
                        confirmed = True
                        break
                except Exception:
                    pass
            if confirmed:
                break
            page.wait_for_timeout(300)
        for _ in range(12):
            if self._tab_submitted(frame, idx):
                return True
            page.wait_for_timeout(400)
        return False

    def _wait_question_number(self, frame, page, idx: int, timeout: float = 6.0) -> bool:
        want = str(idx + 1)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                order = frame.evaluate(
                    "() => document.querySelector('.subject-item.J_order.active')?.getAttribute('data-order') || ''"
                )
            except Exception:
                order = ""
            if str(order).strip() == want:
                return True
            page.wait_for_timeout(300)
        return False

    def _question_info(self, frame) -> dict | None:
        try:
            return frame.evaluate(
                """() => {
                  const root = document.querySelector('.container-problem .subject-item')
                    || document.querySelector('.container-problem');
                  if (!root) return { ok: false };
                  const kind = (root.querySelector('.item-type')?.innerText || '').trim();
                  const answers = [...root.querySelectorAll('.list-inline li, .list-unstyled li')]
                    .filter(li => li.querySelector('input[type=radio], input[type=checkbox]'));
                  const inputs = [...root.querySelectorAll(
                    'input:not([type=radio]):not([type=checkbox]), textarea, [contenteditable="true"]'
                  )].filter(el => el.offsetWidth || el.offsetHeight);
                  const fillValues = inputs.map(el => (el.value ?? el.innerText ?? '').trim());
                  const answered = answers.length
                    ? !!root.querySelector('input:checked')
                    : (inputs.length ? fillValues.every(Boolean) : false);
                  return {
                    ok: true,
                    text: (root.innerText || '').trim(),
                    kind,
                    options: answers.length,
                    inputs: inputs.length,
                    answered
                  };
                }"""
            )
        except Exception:
            return None

    def _fill_answered(self, frame) -> bool:
        try:
            return bool(
                frame.evaluate(
                    """() => {
                      const root = document.querySelector('.container-problem .subject-item')
                        || document.querySelector('.container-problem');
                      if (!root) return false;
                      const inputs = [...root.querySelectorAll(
                        'input:not([type=radio]):not([type=checkbox]), textarea, [contenteditable="true"]'
                      )].filter(el => el.offsetWidth || el.offsetHeight);
                      return inputs.length > 0 &&
                        inputs.every(el => ((el.value ?? el.innerText ?? '').trim().length > 0));
                    }"""
                )
            )
        except Exception:
            return False

    def _wait_images(self, frame, page) -> None:
        for _ in range(10):
            try:
                if frame.evaluate("() => [...document.querySelectorAll('.container-problem img')].every(i => i.complete)"):
                    return
            except Exception:
                pass
            page.wait_for_timeout(300)

