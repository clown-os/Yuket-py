# -*- coding: utf-8 -*-
"""雨课堂本地刷课工具（Python + Playwright）。

用法：
    python main.py                       # 列出课程，选择后开刷
    python main.py --url "<课程页URL>"    # 直接刷指定课程
    python main.py --course-id 123456    # 指定教室 ID
    python main.py --login-only          # 只扫码登录
"""
from __future__ import annotations

import argparse
import sys

from ykt.browser import BrowserSession, is_login_url
from ykt.config import answer_configured, build_settings, load_config, start_url
from ykt.courses import choose_courses, index_url, list_courses, open_course_by_name
from ykt.logger import setup_logger
from ykt.player import MediaUnavailable
from ykt.runner import App, MaxItemsReached, StopRequested, run
from ykt.runner_course import UnfinishedCourseRunner, has_unfinished_tab
from ykt.solver import AnswerClient
from ykt.state import StateStore, normalize_url

VERSION = "1.1.0"


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="雨课堂本地刷课工具（微信扫码登录 / 选择课程 / 未完成列表刷课 / 播完未完成整段重播）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python main.py\n"
            "  python main.py --url \"https://www.yuketang.cn/v2/web/studentLog/123456\"\n"
            "  python main.py --course-id 123456\n"
            "  python main.py --max-items 2          # 只刷 2 个未完成视频（验收用）\n"
        ),
    )
    parser.add_argument("--url", help="课程页 URL（指定后不再列出课程）")
    parser.add_argument("--course-id", help="雨课堂教室 ID（classroom_id）")
    parser.add_argument("--config", default="config.ini", help="配置文件路径（默认 config.ini）")
    parser.add_argument("--headless", action="store_true", help="无头模式（首次登录不要用）")
    parser.add_argument("--speed", type=float, help="播放倍速，覆盖配置")
    parser.add_argument("--replay-attempts", type=int, help="每个视频最多播放次数（含第一次，默认 2）")
    parser.add_argument("--no-replay", action="store_true", help="禁用“播完未完成则整段重播”")
    parser.add_argument("--stop-on-fail", action="store_true", help="重播后仍未完成时停止")
    parser.add_argument("--no-answer", action="store_true", help="禁用自动答题")
    parser.add_argument("--answer", action="store_true", help="启用自动答题（需在配置里填写 API）")
    parser.add_argument("--homework", action="store_true", help="处理「未完成 → 作业」")
    parser.add_argument("--no-homework", action="store_true", help="只刷视频，不处理作业")
    parser.add_argument("--homework-only", action="store_true", help="只做作业，不刷视频")
    parser.add_argument("--homework-index", type=int, help="只处理第 N 个未完成作业（1 开始，测试用）")
    parser.add_argument("--no-submit", action="store_true", help="答完不提交（测试/人工复核用）")
    parser.add_argument("--max-items", type=int, help="每门课最多处理多少个视频（0 = 不限制）")
    parser.add_argument("--workers", type=int, help="并行播放标签页数量（1~8，默认 4）")
    parser.add_argument("--reset-state", action="store_true", help="清除所选课程的断点/失败记录")
    parser.add_argument("--login-only", action="store_true", help="只完成扫码登录并保存登录态")
    parser.add_argument("--list-courses", action="store_true", help="只列出课程中心里的课程并退出")
    parser.add_argument("--log-level", default="INFO", help="日志级别（DEBUG/INFO/WARNING）")
    parser.add_argument("--version", action="version", version=f"yuketang-python {VERSION}")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    cp = load_config(args.config)
    overrides = {
        "headless": True if args.headless else None,
        "speed": args.speed,
        "replay_attempts": args.replay_attempts,
        "replay_enabled": False if args.no_replay else None,
        "stop_on_fail": True if args.stop_on_fail else None,
        "answer_enabled": False if args.no_answer else (True if args.answer else None),
        "answer_homework": False if args.no_homework else (True if args.homework else None),
        "homework_only": True if args.homework_only else None,
        "homework_index": args.homework_index,
        "answer_submit": False if args.no_submit else None,
        "max_items": args.max_items,
        "workers": args.workers,
    }
    settings = build_settings(cp, overrides)
    log = setup_logger(settings.log_dir, args.log_level)

    log.info(f"雨课堂本地刷课工具 v{VERSION}")
    if settings.answer_enabled and not answer_configured(settings):
        log.warning("已启用自动答题，但 api_url / api_key 未填写完整，答题将被跳过")

    state = StateStore(settings.state_file)
    solver = AnswerClient(settings, log)
    target = _resolve_target(settings, args)

    session = BrowserSession(settings, log)
    try:
        session.start()
        session.ensure_login(target)
        if args.login_only:
            log.info("登录态已保存，退出")
            return 0
        if args.list_courses:
            courses = list_courses(session.page, settings, log)
            for i, course in enumerate(courses, 1):
                print(f"[{i:>2}] {course['text']}")
            return 0
        if is_login_url(session.page.url):
            log.error("登录未完成，请重新运行并扫码")
            return 2

        if target:
            _open_target(session.page, target, session, log)
            _run_course(session, settings, state, solver, log, session.page, args, target)
        else:
            _run_selected_courses(session, settings, state, solver, log, args)

        log.info("全部完成")
        return 0
    except MaxItemsReached as exc:
        log.info(f"已停止：{exc}")
        return 0
    except MediaUnavailable as exc:
        log.error(f"已停止：{exc}")
        return 1
    except StopRequested as exc:
        log.error(f"已停止：{exc}")
        return 1
    except KeyboardInterrupt:
        log.warning("收到 Ctrl+C，已保存断点，下次运行会继续")
        return 130
    except Exception as exc:  # noqa: BLE001
        log.exception(f"运行失败：{exc}")
        return 1
    finally:
        try:
            state.save()
        except Exception:
            pass
        session.close()


# ---------------- 选课流程 ----------------
def _run_selected_courses(session, settings, state, solver, log, args) -> None:
    index_page = session.page
    courses = list_courses(index_page, settings, log)
    if not courses:
        log.error("课程中心没有读取到课程")
        return
    picked = choose_courses(courses, log)
    if not picked:
        log.warning("没有选择课程")
        return

    for n, course in enumerate(picked, 1):
        log.info("=" * 72)
        log.info(f"课程 {n}/{len(picked)}：{course['name']}  {course['text']}")
        log.info("=" * 72)
        course_page, course_url = open_course_by_name(index_page, settings, course, log)
        if not course_page:
            continue
        session.page = course_page
        try:
            _run_course(session, settings, state, solver, log, course_page, args, course_url)
        except (MediaUnavailable, StopRequested, MaxItemsReached):
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning(f"课程「{course['name']}」处理失败：{exc}")
        finally:
            if course_page is not index_page:
                try:
                    course_page.close()
                except Exception:
                    pass
                session.page = index_page
                try:
                    index_page.bring_to_front()
                except Exception:
                    pass
            try:
                index_page.goto(index_url(settings), wait_until="domcontentloaded")
                index_page.wait_for_timeout(1500)
            except Exception:
                pass


def _run_course(session, settings, state, solver, log, page, args, course_url: str | None) -> None:
    session.page = page
    if args.reset_state:
        state.reset_course(page.url)
        log.info("已清除该课程的断点记录")

    # 等页面渲染出课程标签
    for _ in range(10):
        if has_unfinished_tab(page):
            break
        page.wait_for_timeout(500)

    app = App(session=session, settings=settings, log=log, state=state, solver=solver)
    if has_unfinished_tab(page):
        log.info("使用「未完成」列表刷课")
        UnfinishedCourseRunner(app, course_url or page.url).run()
    else:
        log.info("当前课程没有「未完成」列表，回退到目录/路线模式")
        run(app)


def _resolve_target(settings, args: argparse.Namespace) -> str | None:
    if args.url:
        return args.url
    if args.course_id:
        return f"{settings.base_url}/v2/web/studentLog/{args.course_id}"
    if args.login_only:
        return start_url(settings)
    return None


def _open_target(page, target: str, session, log) -> None:
    if normalize_url(page.url) == normalize_url(target):
        return
    log.info(f"打开课程页：{target}")
    try:
        page.goto(target, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"打开课程页失败：{exc}")
    if is_login_url(page.url):
        log.warning("登录态失效，重新等待扫码")
        session.ensure_login(target)


if __name__ == "__main__":
    sys.exit(main())
