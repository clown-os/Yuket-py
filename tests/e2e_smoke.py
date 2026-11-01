# -*- coding: utf-8 -*-
"""端到端冒烟测试：跑本地 mock 雨课堂，验证

1. 未登录 -> 等待扫码 -> 登录态生效；
2. v2 目录扫描、批量区展开；
3. 视频播放结束后“未完成 -> 从头重播 -> 完成”；
4. 作业调用自定义答题接口并提交。

运行：
    python tests/e2e_smoke.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.mock_site import server as mock  # noqa: E402


def build_config(path: Path, base_url: str) -> None:
    path.write_text(
        f"""[site]
base_url = {base_url}
start_page = /v2/web/index
login_timeout = 30

[browser]
headless = true
channel = chrome
profile_dir = {(path.parent / 'profile').as_posix()}

[playback]
speed = 4
mute = true
ppt_interval = 0.2
completion_timeout = 3
media_timeout = 0

[replay]
enabled = true
max_attempts = 2
reload_on_retry = true
stop_on_fail = false

[answer]
enabled = true
provider = openai
api_url = {base_url}/ai/v1/chat/completions
api_key = test-key
model = mock-model
auth = bearer
timeout = 20
max_retries = 2
auto_submit = true

[comment]
enabled = false

[state]
file = {(path.parent / 'state.json').as_posix()}

[log]
dir = {(path.parent / 'logs').as_posix()}
""",
        encoding="utf-8",
    )


def main() -> int:
    httpd = mock.create_server(0)
    host, port = httpd.server_address
    base_url = f"http://{host}:{port}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    tmp = Path(tempfile.mkdtemp(prefix="ykt-e2e-"))
    cfg = tmp / "config.ini"
    build_config(cfg, base_url)
    os.chdir(tmp)

    # 重置 mock 状态
    for st in mock.STATE["items"].values():
        st["uploads"] = 0
        st["done"] = False
        st["answer"] = None
    mock.STATE["logins"] = 0

    import main as cli

    started = time.time()
    rc = cli.main(
        [
            "--url",
            f"{base_url}/v2/web/studentLog/1",
            "--config",
            str(cfg),
            "--headless",
        ]
    )
    elapsed = time.time() - started

    print(f"\n--- mock state ({elapsed:.1f}s, rc={rc}) ---")
    print(json.dumps(mock.STATE, ensure_ascii=False, indent=2))

    log_text = ""
    for log_file in sorted((tmp / "logs").glob("*.log")):
        log_text += log_file.read_text(encoding="utf-8", errors="replace")

    failures = []
    if rc != 0:
        failures.append(f"main() 返回 {rc}")
    v1 = mock.STATE["items"]["v1"]
    v2 = mock.STATE["items"]["v2"]
    hw = mock.STATE["items"]["hw1"]

    if not v1["done"]:
        failures.append("v1 未完成")
    if v1["uploads"] < 2:
        failures.append(f"v1 未触发重播（uploads={v1['uploads']}）")
    if not v2["done"]:
        failures.append("v2 未完成")
    if v2["uploads"] < 2:
        failures.append(f"v2 未触发目录级重播（uploads={v2['uploads']}）")
    if hw["answer"] != "B":
        failures.append(f"作业答案错误（answer={hw['answer']}）")
    if "从头重播" not in log_text:
        failures.append("日志中没有出现“从头重播”")
    if "目录仍未标记完成" not in log_text:
        failures.append("日志中没有出现目录级复核重播")
    if "登录成功" not in log_text:
        failures.append("日志中没有出现“登录成功”")

    httpd.shutdown()
    if failures:
        print("\n[FAIL] " + "; ".join(failures))
        print("日志：", tmp / "logs")
        return 1
    print("\n[PASS] 登录 / 播放 / 未完成重播 / 自动答题 全部通过")
    print("日志：", tmp / "logs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
