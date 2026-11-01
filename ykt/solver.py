# -*- coding: utf-8 -*-
"""可插拔答题接口。

支持三种 provider：
- openai     OpenAI 兼容 /chat/completions
- anthropic  Anthropic Messages API
- custom     任意 HTTP 接口（便于接入自己的题库/中转）
"""
from __future__ import annotations

import json
import re
import time

import requests

from .config import Settings

LETTERS = "ABCDEFGH"

SYSTEM_PROMPT = "你是一个只输出答案的做题助手。判断题输出“对”或“错”，选择题只输出选项字母。"

PROMPT_TMPL = """请根据题目选出正确答案。
强约束：
1) 本题只有 {count} 个选项，范围 A-{last}
2) 忽略 OCR 噪音，按选项出现顺序映射 A/B/C/D...
3) 输出格式必须包含“正确答案：”前缀，例如 正确答案：A 或 正确答案：ABD 或 正确答案：对
{extra}
题目内容：
{question}
"""

VISION_PROMPT = """下面是一道雨课堂作业题的截图，包含题干和选项（选项可能是图片或树形图）。
请以图片为准理解题目，不要依赖下面可能乱码的文字。"""

FILL_PROMPT = """下面是一道雨课堂填空题/简答题的截图和题目文字（文字可能乱码，以图片为准）。
请只输出答案本身，不要解释，不要写“答案：”。
如果有多个空，用 | 分隔，顺序与题目一致。
题目：
{question}"""

ANSWER_RE = re.compile(
    r"(?:正确|参考|标准)?答案\s*[：:是]?\s*([A-Fa-f]+(?:[\s,，、]*[A-Fa-f]+)*|[对错]|正确|错误)"
)
PLAIN_RE = re.compile(r"^\s*([A-Fa-f]{1,6})\s*$")


class AnswerClient:
    def __init__(self, settings: Settings, log):
        self.settings = settings
        self.log = log

    @property
    def enabled(self) -> bool:
        s = self.settings
        if not s.answer_enabled:
            return False
        if s.answer_provider == "custom":
            return bool(s.answer_url and s.answer_key)
        return bool(s.answer_url and s.answer_key and "xxxx" not in s.answer_key.lower())

    # ---------------- 对外 ----------------
    def solve(
        self,
        question: str,
        options: list[str],
        screenshot_b64: str | None = None,
    ) -> list[int]:
        """返回选项下标列表；无法解析返回空列表。

        screenshot_b64: 题目截图（不含 data: 前缀），用于视觉模型。
        """
        if not self.enabled:
            return []
        question = (question or "").strip()
        has_image = bool(screenshot_b64 and self.settings.answer_vision)
        if len(question) < 3 and not has_image:
            return []
        for attempt in range(1, self.settings.answer_retries + 1):
            try:
                raw = self._request(
                    question,
                    options,
                    screenshot_b64 if self.settings.answer_vision else None,
                )
                indices = parse_answer(raw, len(options))
                if indices:
                    self.log.info(f"接口答案：{describe(indices, len(options))}（原始返回：{raw.strip()[:40]!r}）")
                    return indices
                self.log.warning(f"答案解析失败（{attempt}/{self.settings.answer_retries}）：{raw[:80]!r}")
            except Exception as exc:  # noqa: BLE001
                self.log.warning(f"答题接口请求失败（{attempt}/{self.settings.answer_retries}）：{exc}")
            if attempt < self.settings.answer_retries:
                time.sleep(2)
        return []

    def solve_fill(self, question: str, screenshot_b64: str | None = None) -> str:
        """填空题/简答题：返回纯文本答案（多个空用 | 分隔）。"""
        if not self.enabled:
            return ""
        question = (question or "").strip()
        has_image = bool(screenshot_b64 and self.settings.answer_vision)
        if not question and not has_image:
            return ""
        prompt = FILL_PROMPT.format(question=question)
        if has_image:
            prompt = VISION_PROMPT + "\n\n" + prompt
        for attempt in range(1, self.settings.answer_retries + 1):
            try:
                raw = self._request_with_prompt(
                    prompt,
                    screenshot_b64 if has_image else None,
                )
                answer = clean_fill(raw)
                if answer:
                    self.log.info(f"接口答案（填空）：{answer[:60]!r}")
                    return answer
                self.log.warning(f"填空答案解析失败（{attempt}/{self.settings.answer_retries}）：{raw[:80]!r}")
            except Exception as exc:  # noqa: BLE001
                self.log.warning(f"填空答题接口失败（{attempt}/{self.settings.answer_retries}）：{exc}")
            if attempt < self.settings.answer_retries:
                time.sleep(2)
        return ""

    def _request_with_prompt(self, prompt: str, screenshot_b64: str | None = None) -> str:
        provider = self.settings.answer_provider
        if provider == "custom":
            return self._custom_prompt(prompt, screenshot_b64)
        if provider == "anthropic":
            return self._anthropic(prompt, screenshot_b64)
        return self._openai(prompt, screenshot_b64)

    # ---------------- provider ----------------
    def _request(self, question: str, options: list[str], screenshot_b64: str | None = None) -> str:
        s = self.settings
        provider = s.answer_provider
        if provider == "custom":
            return self._custom(question, options, screenshot_b64)
        prompt = build_prompt(question, options)
        if screenshot_b64:
            prompt = VISION_PROMPT + "\n\n" + prompt
        return self._request_with_prompt(prompt, screenshot_b64)

    def _headers(self) -> dict:
        s = self.settings
        headers = {"Content-Type": "application/json"}
        if s.answer_auth == "x-api-key":
            headers["x-api-key"] = s.answer_key
        else:
            headers["Authorization"] = f"Bearer {s.answer_key}"
        return headers

    def _openai(self, prompt: str, screenshot_b64: str | None = None) -> str:
        s = self.settings
        if screenshot_b64:
            content = [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"},
                },
            ]
        else:
            content = prompt
        resp = requests.post(
            s.answer_url,
            headers=self._headers(),
            json={
                "model": s.answer_model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": content},
                ],
                "temperature": 0.1,
            },
            timeout=s.answer_timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        message = data["choices"][0]["message"]
        return str(message.get("content") or message.get("reasoning_content") or "")

    def _anthropic(self, prompt: str, screenshot_b64: str | None = None) -> str:
        s = self.settings
        headers = self._headers()
        if "api.anthropic.com" in s.answer_url:
            headers["anthropic-version"] = "2023-06-01"
        content = prompt
        if screenshot_b64:
            content = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": screenshot_b64,
                    },
                },
                {"type": "text", "text": prompt},
            ]
        resp = requests.post(
            s.answer_url,
            headers=headers,
            json={
                "model": s.answer_model,
                "max_tokens": 1024,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": content}],
            },
            timeout=s.answer_timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return str(data["content"][0]["text"])

    def _custom(self, question: str, options: list[str], screenshot_b64: str | None = None) -> str:
        payload = {"question": question, "options": options}
        if screenshot_b64:
            payload["image"] = f"data:image/png;base64,{screenshot_b64}"
        return self._custom_post(payload)

    def _custom_prompt(self, prompt: str, screenshot_b64: str | None = None) -> str:
        payload = {"question": prompt, "options": [], "type": "fill"}
        if screenshot_b64:
            payload["image"] = f"data:image/png;base64,{screenshot_b64}"
        return self._custom_post(payload)

    def _custom_post(self, payload: dict) -> str:
        s = self.settings
        resp = requests.post(
            s.answer_url,
            headers=self._headers(),
            json=payload,
            timeout=s.answer_timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, str):
            return data
        for key in ("answer", "result", "data"):
            value = data.get(key) if isinstance(data, dict) else None
            if isinstance(value, str) and value.strip():
                return value
            if isinstance(value, dict):
                for sub in ("answer", "result"):
                    if isinstance(value.get(sub), str) and value[sub].strip():
                        return value[sub]
        return json.dumps(data, ensure_ascii=False)


def clean_fill(raw: str) -> str:
    text = (raw or "").strip()
    text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    text = re.sub(r"^(?:正确|参考|标准)?答案\s*[：:]\s*", "", text).strip()
    text = text.strip('"\'“”')
    text = text.replace("\n", " ").strip()
    return text[:300]


def build_prompt(question: str, options: list[str]) -> str:
    count = len(options) or 4
    last = LETTERS[min(count, len(LETTERS)) - 1]
    if options:
        lines = "\n".join(f"{LETTERS[i]}. {opt}" for i, opt in enumerate(options))
        extra = "\n选项：\n" + lines + "\n"
    else:
        extra = "\n（本题没有选项，若为判断题请输出“对”或“错”）\n"
    return PROMPT_TMPL.format(count=count, last=last, extra=extra, question=question)


def parse_answer(raw: str, option_count: int) -> list[int]:
    if not raw:
        return []
    text = raw.strip()
    match = ANSWER_RE.search(text) or PLAIN_RE.match(text)
    if not match:
        return []
    token = match.group(1).upper().replace(" ", "")
    if token in ("对", "正确"):
        return [0] if option_count <= 2 else []
    if token in ("错", "错误"):
        return [1] if option_count <= 2 else []
    indices: list[int] = []
    for char in re.sub(r"[\s,，、]", "", token):
        if char in LETTERS:
            idx = LETTERS.index(char)
            if idx < option_count and idx not in indices:
                indices.append(idx)
    return indices


def describe(indices: list[int], option_count: int) -> str:
    if not indices:
        return "无"
    return "".join(LETTERS[i] for i in indices if i < len(LETTERS))
