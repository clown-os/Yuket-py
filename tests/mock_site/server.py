# -*- coding: utf-8 -*-
"""本地模拟雨课堂 v2/web 页面，用于端到端验证（不访问真实站点）。

覆盖：
- 未登录跳转 /login，模拟扫码后写 cookie 并回跳
- 课程目录 .logs-list + 批量区
- 视频页：播放结束后上报进度；v1 第一次上报故意失败 -> 触发“从头重播”
- 作业页：调用本地 mock OpenAI 接口答题并提交
"""
from __future__ import annotations

import io
import json
import math
import struct
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

STATE = {
    "items": {
        "v1": {"uploads": 0, "progress": 50, "done": False, "fail_uploads": 1, "ui_done": False},
        "v2": {"uploads": 0, "progress": 50, "done": False, "fail_uploads": 1, "ui_done": True},
        "hw1": {"uploads": 0, "progress": 0, "done": False, "fail_uploads": 0, "answer": None},
    },
    "logins": 0,
}


def _status(item: str) -> str:
    st = STATE["items"][item]
    if st["done"]:
        return "100% 已完成"
    if item == "hw1":
        return "未开始"
    if item == "v2":
        return "进行中"
    return f"{st['progress']}%"


def _batch_status() -> str:
    done = STATE["items"]["v2"]["done"] and STATE["items"]["hw1"]["done"]
    return "已完成" if done else "进行中"


def _wav_bytes(seconds: float = 4.0, rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        frames = bytearray()
        total = int(seconds * rate)
        for i in range(total):
            value = int(1200 * math.sin(2 * math.pi * 440 * i / rate))
            frames += struct.pack("<h", value)
        wav.writeframes(bytes(frames))
    return buf.getvalue()


WAV = _wav_bytes()

LOGIN_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>登录</title></head>
<body>
<h1>微信扫码登录</h1>
<p>模拟页面：2.5 秒后自动视为扫码成功</p>
<img id="qr" width="180" height="180" alt="二维码"
     src="data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='180' height='180'><rect width='180' height='180' fill='%23eee'/><text x='20' y='100' font-size='18'>MOCK QR</text></svg>"/>
<script>
  const next = "__NEXT__" || '/v2/web/studentLog/1';
  setTimeout(() => {
    document.cookie = 'session=ok; path=/';
    location.href = next;
  }, 2500);
</script>
</body></html>"""

DIRECTORY_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>课程目录</title>
<style>
  body{font-family:sans-serif;margin:20px}
  .logs-list>div{border:1px solid #ddd;margin:8px 0;padding:8px}
  section{cursor:pointer}
  .statistics-box .aside{color:#888}
  .leaf_list__wrap{border-left:2px solid #eee;margin-left:12px;padding-left:12px}
  .activity__wrap{padding:6px;cursor:pointer}
  .activity__wrap:hover{background:#f6f6f6}
</style></head>
<body>
<h2>模拟课程目录</h2>
<div class="logs-list">
  <div>
    <div class="content-box">
      <section onclick="location.href='/v2/web/studentCards/v1'">
        <span class="tag"><svg><use xlink:href="#icon-shipin"></use></svg></span>
        <h2>第一章 视频</h2>
        <div class="statistics-box"><span class="aside" data-item="v1">50%</span></div>
      </section>
    </div>
  </div>
  <div>
    <div class="content-box">
      <section>
        <span class="tag"><svg><use xlink:href="#icon-piliang"></use></svg></span>
        <h2>第二章 批量</h2>
        <div class="statistics-box"><span class="aside" data-item="batch">进行中</span></div>
        <div class="sub-info"><span class="gray"><span id="expand">展开</span></span></div>
      </section>
      <div class="leaf_list__wrap" id="leaf-wrap" hidden>
        <div class="activity__wrap" onclick="location.href='/v2/web/studentCards/v2'">
          <span class="tag"><svg><use xlink:href="#icon-shipin"></use></svg>视频</span>
          <h2>批量视频</h2>
          <div class="statistics-box"><span class="aside" data-item="v2">进行中</span></div>
        </div>
        <div class="activity__wrap" onclick="location.href='/v2/web/homework/1'">
          <span class="tag"><svg><use xlink:href="#icon-zuoye"></use></svg>作业</span>
          <h2>批量作业</h2>
          <div class="statistics-box"><span class="aside" data-item="hw1">未开始</span></div>
        </div>
      </div>
    </div>
  </div>
</div>
<script>
  document.getElementById('expand').onclick = (e) => {
    e.stopPropagation();
    const wrap = document.getElementById('leaf-wrap');
    wrap.hidden = !wrap.hidden;
    e.target.textContent = wrap.hidden ? '展开' : '收起';
  };
  async function refresh() {
    try {
      const s = await (await fetch('/api/statuses')).json();
      document.querySelectorAll('.aside[data-item]').forEach(el => {
        const key = el.dataset.item;
        if (s[key] !== undefined) el.textContent = s[key];
      });
    } catch (e) {}
  }
  refresh();
  setInterval(refresh, 700);
</script>
</body></html>"""

CARD_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>%(title)s</title>
<style>body{font-family:sans-serif;margin:20px}.progress-wrap{color:#333}</style></head>
<body>
<div class="title">%(title)s</div>
<div class="progress-wrap"><span class="text">%(initial)s</span></div>
<div class="box">模拟播放器</div>
<audio id="media" src="/media/tone.wav" preload="auto"></audio>
<script>
  const item = '%(item)s';
  const uiDone = %(ui_done)s;
  const media = document.getElementById('media');
  const text = document.querySelector('.progress-wrap .text');
  let reported = false;
  let uiDoneLocked = false;

  async function refresh() {
    if (uiDoneLocked) return;
    try {
      const s = await (await fetch('/api/status?item=' + item)).json();
      if (!uiDoneLocked) text.textContent = s.text;
    } catch (e) {}
  }
  async function report() {
    if (reported) return;
    reported = true;
    try {
      const res = await fetch('/api/upload', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({item})
      });
      if (!res.ok) {
        if (!uiDoneLocked) text.textContent = '75%%';
        reported = false;  // 允许下一次重播再次上报（模拟网络失败）
      }
    } catch (e) { reported = false; }
    refresh();
  }
  media.addEventListener('ended', () => {
    if (uiDone) { uiDoneLocked = true; text.textContent = '100%% 已完成'; }
    report();
  });
  media.addEventListener('timeupdate', () => {
    if (!media.duration) return;
    const pct = Math.min(99, Math.floor(media.currentTime * 100 / media.duration));
    if (!%(initial_done)s) text.textContent = pct + '%%';
  });
  refresh();
  setInterval(refresh, 800);
</script>
</body></html>"""

HOMEWORK_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>作业</title>
<style>body{font-family:sans-serif;margin:20px}.status{color:#2a2}</style></head>
<body>
<div class="container-problem">
  <div class="item-type">单选题</div>
  <div class="item-body">
    <p>1 + 1 = ?</p>
    <ul class="list-inline list-unstyled-radio">
      <li><label class="el-radio"><input type="radio" name="q" value="A"> A. 1</label></li>
      <li><label class="el-radio"><input type="radio" name="q" value="B"> B. 2</label></li>
    </ul>
  </div>
  <button class="el-button" id="submit">提交</button>
</div>
<div class="status" id="status"></div>
<script>
  document.getElementById('submit').onclick = async () => {
    const checked = document.querySelector('input[name=q]:checked');
    if (!checked) { document.getElementById('status').textContent = '请选择答案'; return; }
    const res = await fetch('/api/answer', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({item: 'hw1', answer: checked.value})
    });
    if (res.ok) {
      document.getElementById('status').textContent = '已完成';
      document.getElementById('submit').disabled = true;
    }
  };
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "MockYuketang/1.0"

    def log_message(self, fmt, *args):  # noqa: A003
        pass

    # ---------------- helpers ----------------
    def _send(self, status: int, body: bytes, ctype: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, status: int, data) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _authed(self) -> bool:
        cookie = self.headers.get("Cookie") or ""
        return "session=ok" in cookie

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    # ---------------- GET ----------------
    def do_GET(self):  # noqa: N802
        parts = urlsplit(self.path)
        path = parts.path
        query = parse_qs(parts.query)

        if path == "/login":
            STATE["logins"] += 1
            nxt = unquote(query.get("next", ["/v2/web/studentLog/1"])[0])
            html = LOGIN_HTML.replace('"__NEXT__"', json.dumps(nxt))
            self._send(200, html.encode("utf-8"))
            return

        if path == "/media/tone.wav":
            self._send(200, WAV, "audio/wav")
            return

        if not self._authed():
            self._redirect("/login?next=" + quote(self.path, safe="/?=&"))
            return

        if path == "/api/statuses":
            data = {key: _status(key) for key in STATE["items"]}
            data["batch"] = _batch_status()
            self._json(200, data)
            return

        if path == "/api/status":
            item = query.get("item", ["v1"])[0]
            if item not in STATE["items"]:
                self._json(404, {"error": "unknown item"})
                return
            self._json(200, {"text": _status(item)})
            return

        if path == "/api/state":
            self._json(200, STATE)
            return

        if path == "/v2/web/index":
            self._send(200, b"<html><body><a href='/v2/web/studentLog/1'>course</a></body></html>")
            return

        if path == "/v2/web/studentLog/1":
            self._send(200, DIRECTORY_HTML.encode("utf-8"))
            return

        if path in ("/v2/web/studentCards/v1", "/v2/web/studentCards/v2"):
            item = "v1" if path.endswith("v1") else "v2"
            st = STATE["items"][item]
            html = CARD_HTML % {
                "title": "视频 " + item,
                "item": item,
                "initial": _status(item),
                "initial_done": "true" if st["done"] else "false",
                "ui_done": "true" if st.get("ui_done") else "false",
            }
            self._send(200, html.encode("utf-8"))
            return

        if path == "/v2/web/homework/1":
            self._send(200, HOMEWORK_HTML.encode("utf-8"))
            return

        self._send(404, b"not found")

    # ---------------- POST ----------------
    def do_POST(self):  # noqa: N802
        parts = urlsplit(self.path)
        path = parts.path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            body = {}

        if path == "/ai/v1/chat/completions":
            prompt = ""
            try:
                prompt = body["messages"][-1]["content"]
            except Exception:
                pass
            answer = "正确答案：B" if "1 + 1" in prompt else "正确答案：A"
            self._json(
                200,
                {
                    "id": "mock",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}}],
                },
            )
            return

        if not self._authed():
            self._json(401, {"error": "not logged in"})
            return

        if path == "/api/upload":
            item = body.get("item", "")
            st = STATE["items"].get(item)
            if not st:
                self._json(404, {"error": "unknown item"})
                return
            st["uploads"] += 1
            if st["uploads"] <= st.get("fail_uploads", 0):
                self._json(500, {"error": "simulated network failure"})
                return
            st["progress"] = 100
            st["done"] = True
            self._json(200, {"ok": True, "uploads": st["uploads"]})
            return

        if path == "/api/answer":
            st = STATE["items"]["hw1"]
            st["answer"] = body.get("answer")
            st["done"] = True
            self._json(200, {"ok": True})
            return

        self._json(404, {"error": "not found"})


def create_server(port: int = 0):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return server


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = create_server(args.port)
    host, port = server.server_address
    print(f"mock yuketang on http://{host}:{port}/v2/web/studentLog/1")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
