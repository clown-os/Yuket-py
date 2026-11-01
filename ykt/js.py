# -*- coding: utf-8 -*-
"""注入到页面执行的 JavaScript 片段。

全部逻辑移植自 yuketang.js（油猴脚本 3.0.6），并针对 Python 驱动做了收敛：
- 页面内只保留“媒体保活/自动播放”控制器，其余判断在 Python 侧轮询，便于日志与中断。
"""

# 页面内媒体控制器：自动播放、静音、倍速、防暂停、从头重播
MEDIA_CONTROLLER = r"""
(() => {
  if (window.__ykt) return;
  const pick = () => {
    const all = [...document.querySelectorAll('video, audio')];
    const visible = all.filter(m => {
      const r = m.getBoundingClientRect();
      return (r.width > 0 && r.height > 0) || m.tagName.toLowerCase() === 'audio';
    });
    const list = visible.length ? visible : all;
    if (!list.length) return null;
    const score = m => {
      const r = m.getBoundingClientRect();
      return (!m.paused && !m.ended ? 1e9 : 0) + r.width * r.height + (Number(m.currentTime) || 0);
    };
    return [...list].sort((a, b) => score(b) - score(a))[0];
  };
  const state = { timer: null, rate: 2, muted: true, media: null, last: 0, forceZeroUntil: 0, lastZeroCt: 0 };
  const apply = m => {
    if (!m) return;
    state.media = m;
    state.last = Date.now();
    if (state.muted) { m.muted = true; m.volume = 0; m.defaultMuted = true; }
    if (Math.abs((m.playbackRate || 1) - state.rate) > 0.01) {
      try { m.playbackRate = state.rate; } catch (e) {}
    }
    // 学习空间播放器会在加载后恢复上次位置。只处理“突然跳回断点”，
    // 允许视频从 0 正常前进，避免把整段重播卡在 00:00。
    if (state.forceZeroUntil) {
      if (Date.now() > state.forceZeroUntil) {
        state.forceZeroUntil = 0;
      } else {
        const ct = Number(m.currentTime) || 0;
        if (ct > state.lastZeroCt + 20) {
          try { m.currentTime = 0; } catch (e) {}
          state.lastZeroCt = 0;
        } else {
          state.lastZeroCt = ct;
        }
      }
    }
    if (m.paused && !m.ended) {
      const p = m.play();
      if (p && p.catch) p.catch(() => {});
    }
  };
  const tick = () => { if (state.timer) apply(pick()); };

  window.__ykt = {
    start(opts) {
      // 只重置定时器，不清空 forceZero：重播时 seekStart 设置的“压回 0”要跨多次 start 保持
      if (state.timer) clearInterval(state.timer);
      document.removeEventListener('visibilitychange', tick);
      window.removeEventListener('focus', tick);
      state.rate = Number(opts?.rate ?? 2);
      state.muted = opts?.mute !== false;
      state.timer = setInterval(tick, 500);
      document.addEventListener('visibilitychange', tick);
      window.addEventListener('focus', tick);
      tick();
    },
    stop() {
      if (state.timer) clearInterval(state.timer);
      state.timer = null;
      document.removeEventListener('visibilitychange', tick);
      window.removeEventListener('focus', tick);
      if (state.media) { try { state.media.pause(); } catch (e) {} }
      state.media = null;
      state.forceZeroUntil = 0;
      state.lastZeroCt = 0;
    },
    media() { return pick(); },
    status() {
      const m = pick();
      if (!m) return { found: false };
      return {
        found: true,
        tag: m.tagName.toLowerCase(),
        ended: !!m.ended,
        paused: !!m.paused,
        current: Number(m.currentTime) || 0,
        duration: Number(m.duration) || 0,
        ready: Number(m.readyState) || 0,
        rate: Number(m.playbackRate) || 1,
        muted: !!m.muted,
        networkState: Number(m.networkState) || 0,
        error: m.error ? { code: m.error.code, message: m.error.message } : null,
        src: String(m.currentSrc || m.src || '').slice(0, 160),
      };
    },
    seekStart() {
      const m = pick();
      if (!m) return false;
      state.forceZeroUntil = Date.now() + 30000;
      state.lastZeroCt = 0;
      try { m.currentTime = 0; } catch (e) { return false; }
      m.play().catch(() => {});
      return true;
    },
    // 雨课堂播放器自带倍速按钮时，模拟点击一次，避免播放器内部状态覆盖 video.playbackRate
    clickSpeed(rate) {
      try {
        const btn = document.querySelector('xt-speedlist xt-button')
          || document.getElementsByTagName('xt-speedlist')[0]?.firstElementChild?.firstElementChild;
        const wrap = document.getElementsByTagName('xt-speedbutton')[0];
        if (btn && wrap) {
          btn.setAttribute('data-speed', rate);
          btn.setAttribute('keyt', rate + '.00');
          btn.innerText = rate + '.00X';
          const ev = document.createEvent('MouseEvent');
          ev.initMouseEvent('mousemove', true, true, window, 0, 10, 10, 10, 10, 0, 0, 0, 0, 0, null);
          wrap.dispatchEvent(ev);
          btn.click();
          return true;
        }
      } catch (e) {}
      return false;
    }
  };
})();
"""

MEDIA_START = "(opts) => { window.__ykt.start(opts); return true; }"
MEDIA_STOP = "() => { window.__ykt && window.__ykt.stop(); return true; }"
MEDIA_STATUS = "() => window.__ykt ? window.__ykt.status() : {found: false}"
MEDIA_SEEK_START = "() => window.__ykt ? window.__ykt.seekStart() : false"
MEDIA_CLICK_SPEED = "(rate) => window.__ykt ? window.__ykt.clickSpeed(rate) : false"

# 判断学习进度文本是否完成
IS_PROGRESS_DONE = r"""
(text) => {
  if (!text) return false;
  if (/100%|99%|98%|已完成/.test(text)) return true;
  const m = String(text).match(/(\d+)\s*\/\s*(\d+)/);
  if (m) {
    const cur = parseInt(m[1], 10), total = parseInt(m[2], 10);
    return total > 0 && cur === total;
  }
  return false;
}
"""

# v2/web 课程目录：读取第 i 集信息
V2_ITEM_INFO = r"""
(i) => {
  const wrap = document.querySelector('.logs-list')?.children?.[i];
  if (!wrap) return null;
  const sec = wrap.querySelector('.content-box section') || wrap.querySelector('section');
  if (!sec) return null;
  const tag = sec.querySelector('.tag use');
  const href = tag?.getAttribute('xlink:href') || tag?.getAttribute('href') || '';
  const title = (sec.querySelector('h2')?.innerText || '').trim();
  const status = (sec.querySelector('.statistics-box .aside')?.innerText || '').trim();
  return {
    href, title, status,
    hasTag: !!sec.querySelector('.tag'),
    subInfo: !!sec.querySelector('.sub-info'),
    expandable: !!sec.querySelector('.sub-info .gray span')
  };
}
"""

# v2/web 批量区：读取第 batchIndex 个目录卡片里的第 idx 个活动
# 注意必须按卡片作用域查询：页面上所有卡片都有一个隐藏的 .leaf_list__wrap
V2_ACTIVITY_INFO = r"""
([batchIndex, idx]) => {
  const box = document.querySelector('.logs-list')?.children?.[batchIndex];
  const wrap = box?.querySelectorAll('.leaf_list__wrap .activity__wrap')?.[idx];
  if (!wrap) return null;
  const tag = wrap.querySelector('.tag use');
  const href = tag?.getAttribute('xlink:href') || tag?.getAttribute('href') || '';
  const tagText = (wrap.querySelector('.tag')?.innerText || '').trim();
  const title = (wrap.querySelector('h2')?.innerText || '').trim();
  const status = (wrap.querySelector('.statistics-box .aside')?.innerText || '').trim();
  return { href, tagText, title, status };
}
"""

V2_COUNT = "() => document.querySelector('.logs-list')?.children?.length || 0"
V2_ACTIVITY_COUNT = "(batchIndex) => document.querySelector('.logs-list')?.children?.[batchIndex]?.querySelectorAll('.leaf_list__wrap .activity__wrap')?.length || 0"

# v2/web 视频/音频页进度
PROGRESS_STATE = r"""
(sel) => {
  const el = document.querySelector(sel || '.progress-wrap .text');
  if (!el) return { has: false, done: false, text: '' };
  const text = (el.textContent || '').replace(/\s+/g, ' ').trim();
  return {
    has: true,
    done: /100%|99%|98%|已完成/.test(text),
    text
  };
}
"""

# 学习空间顶部“完成度：xx%”（当前叶子）
TOP_PROGRESS = r"""
() => {
  const nodes = [...document.querySelectorAll('.el-tooltip.text')];
  const el = nodes.find(n => /完成度/.test(n.innerText || '')) || nodes[0];
  let t = (el?.innerText || '').replace(/\s+/g, ' ').trim();
  if (!t) {
    t = (document.querySelector('.leaf-item.is-active .leaf-item-status')?.innerText || '').trim();
  }
  return t;
}
"""

# 是否已过考核截止时间（视频页 .box 文案）
DEADLINE_PASSED = r"""
() => {
  const box = document.querySelector('.box')?.innerText || '';
  return box.includes('已过考核截止时间');
}
"""

# 关闭“好好学习/继续观看/报告老师”等弹窗
DISMISS_POPUPS = r"""
() => {
  let closed = 0;
  const wrappers = document.querySelectorAll('.el-dialog__wrapper, .el-message-box__wrapper');
  for (const w of wrappers) {
    const style = getComputedStyle(w);
    const rect = w.getBoundingClientRect();
    if (style.display === 'none' || style.visibility === 'hidden' || rect.width === 0) continue;
    const text = w.innerText || '';
    const buttons = [...w.querySelectorAll('button')];
    const click = label => {
      const btn = buttons.find(b => (b.innerText || '').trim().includes(label));
      if (btn) { btn.click(); closed++; }
    };
    if (text.includes('好好学习') || text.includes('继续观看')) click('继续观看');
    else if (text.includes('报告老师')) click('取消');
  }
  return closed;
}
"""

# 防切屏（pro/lms 路线专用）
PREVENT_SCREEN_CHECK = r"""
() => {
  const win = window;
  if (win.__yktScreenGuard) return true;
  win.__yktScreenGuard = true;
  const black = new Set(['visibilitychange', 'blur', 'pagehide']);
  const wrapAdd = target => {
    const origin = target.addEventListener;
    target.addEventListener = function (...args) {
      if (black.has(args[0])) return undefined;
      return origin.apply(this, args);
    };
  };
  wrapAdd(win);
  wrapAdd(document);
  try {
    Object.defineProperties(document, {
      hidden: { value: false },
      visibilityState: { value: 'visible' },
      hasFocus: { value: () => true },
      onvisibilitychange: { get: () => undefined, set: () => {} },
      onblur: { get: () => undefined, set: () => {} }
    });
    Object.defineProperties(win, {
      onblur: { get: () => undefined, set: () => {} },
      onpagehide: { get: () => undefined, set: () => {} }
    });
  } catch (e) {}
  return true;
}
"""

# pro/lms 新版：头部课程信息 + 状态
PRO_HEADER = r"""
() => {
  const bar = document.querySelector('.header-bar');
  const name = bar?.firstElementChild?.innerText?.trim() || '';
  const type = bar?.firstElementChild?.firstElementChild?.getAttribute('class') || '';
  const statusEl = document.querySelector('#app > div.app_index-wrapper > div.wrap > div.viewContainer.heightAbsolutely > div > div > div > div > section.title');
  const status = statusEl?.lastElementChild?.innerText?.trim() || '';
  return { name, type, status };
}
"""

# ai-workspace 导航叶子
AI_LEAVES = r"""
() => {
  const nodes = [...document.querySelectorAll('.nav-item-leaf-box')];
  return nodes.map((n, i) => ({
    index: i,
    active: !!(n.firstElementChild && n.firstElementChild.className && String(n.firstElementChild.className).includes('is-active')),
    title: (n.innerText || '').replace(/\s+/g, ' ').trim(),
    cls: String(n.className || ''),
    sub: String(n.firstElementChild?.className || '')
  }));
}
"""

AI_ACTIVE_LEAF_TITLE = r"""
() => (document.querySelector('.leaf-item.is-active')?.innerText || '').replace(/\s+/g, ' ').trim()
"""

AI_LEAF_DONE = r"""
(titleHint) => {
  const leaves = [...document.querySelectorAll('.leaf-item')];
  const active = document.querySelector('.leaf-item.is-active')
    || leaves.find(el => String(el.className || '').includes('is-active'));
  const byTitle = titleHint
    ? leaves.find(el => (el.innerText || '').includes(titleHint))
    : null;
  const leaf = byTitle || active;
  if (!leaf) return { has: false, done: false, text: '' };
  const status = leaf.querySelector('.leaf-item-status');
  const check = status?.querySelector('.icon-yuanquangou-mianzhuang');
  const pie = status?.querySelector('.status-pie');
  let p = null;
  if (pie) {
    const m = String(pie.getAttribute('style') || '').match(/--p:\s*(\d+)/);
    p = m ? parseInt(m[1], 10) : null;
  }
  const text = (document.querySelector('.el-tooltip.text')?.innerText || leaf.innerText || '')
    .replace(/\s+/g, ' ').trim();
  const done = !!check || (p !== null && p >= 100) || /已完成|已学完|100%/.test(text);
  return { has: !!status, done, text: text.slice(0, 100), p };
}
"""

# 作业/练习：提取当前题目（题干 + 选项）
EXERCISE_EXTRACT = r"""
() => {
  const visible = el => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
  };
  const container = document.querySelector('.container-problem, .container-body .container-problem');
  const roots = [...document.querySelectorAll(
    '.item-type, .item-body, .problem-content, .question-content, .problem-main, .problem-body, .question-body'
  )].filter(visible);
  let root = null;
  const itemType = document.querySelector('.item-type');
  if (itemType && visible(itemType.parentElement)) root = itemType.parentElement;
  if (!root) root = roots.find(el => (el.innerText || '').trim().length > 5) || container;
  if (!root) return { ok: false };
  const text = (root.innerText || '').replace(/\n{3,}/g, '\n\n').trim();
  const list = root.querySelector(
    '.list-inline.list-unstyled-radio, .list-unstyled.list-unstyled-radio, .list-unstyled, ul.list, ' +
    '[class*="option-list"], [class*="answer-list"], ul, [role="radiogroup"]'
  );
  let options = [];
  if (list) {
    options = [...list.querySelectorAll('li, .option-item, .answer-item, [class*="option-item"], [class*="answer-item"]')]
      .filter(visible)
      .map(el => (el.innerText || '').replace(/\s+/g, ' ').trim())
      .filter(Boolean);
  }
  const answered = !!root.querySelector('button[disabled], .el-button.is-disabled, .el-button--info.is-disabled');
  const disabled = !!document.querySelector('.el-button.el-button--info.is-disabled.is-plain');
  return { ok: true, text, options, answered, disabled };
}
"""

# 作业/练习：按选项下标点击
EXERCISE_CLICK = r"""
(indices) => {
  const visible = el => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
  };
  const itemType = document.querySelector('.item-type');
  let root = itemType && visible(itemType.parentElement) ? itemType.parentElement : null;
  if (!root) {
    root = [...document.querySelectorAll('.item-body, .problem-content, .question-content, .problem-body, .question-body')]
      .find(visible) || document.querySelector('.container-problem');
  }
  if (!root) return 0;
  const list = root.querySelector(
    '.list-inline.list-unstyled-radio, .list-unstyled.list-unstyled-radio, .list-unstyled, ul.list, ' +
    '[class*="option-list"], [class*="answer-list"], ul, [role="radiogroup"]'
  );
  if (!list) return 0;
  const options = [...list.querySelectorAll('li, .option-item, .answer-item, [class*="option-item"], [class*="answer-item"]')]
    .filter(visible);
  let clicked = 0;
  for (const idx of indices) {
    const opt = options[idx];
    if (!opt) continue;
    const target = opt.querySelector('label.el-radio, label.el-checkbox, .el-radio__label, .el-checkbox__label, [role="radio"], [role="checkbox"], input') || opt;
    target.click();
    clicked++;
  }
  return clicked;
}
"""

# 作业/练习：点击提交/保存
EXERCISE_SUBMIT = r"""
() => {
  const visible = el => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
  };
  const btns = [...document.querySelectorAll('button, .el-button, [role="button"]')].filter(visible);
  const btn = btns.find(b => /提交|保存|确认|确定|交卷/.test((b.innerText || '').trim()) && !b.disabled);
  if (!btn) return false;
  btn.click();
  return true;
}
"""

# 作业/练习：题目索引（多题标签）
EXERCISE_TABS = r"""
() => {
  const visible = el => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
  };
  const nodes = [...document.querySelectorAll(
    '.subject-item.J_order, .subject-item, .problem-index-item, .question-index-item, [class*="subject-item"], [class*="problem-index"], [class*="question-index"]'
  )].filter(el => visible(el) && (el.innerText || '').trim().length <= 20 && (el.innerText || '').trim());
  return nodes.length;
}
"""

# 图文/讨论：取第一条已有评论 + 输入框发送
COMMENT_FIRST = r"""
() => {
  const sels = ['#new_discuss .new_discuss_list .cont_detail', '.new_discuss_list dd .cont_detail', '.cont_detail.word-break'];
  for (const sel of sels) {
    for (const node of document.querySelectorAll(sel)) {
      const text = (node.innerText || '').trim();
      if (text) return text;
    }
  }
  return '';
}
"""

COMMENT_SEND = r"""
(text) => {
  const input = document.querySelector('.el-textarea__inner');
  if (!input) return false;
  input.value = text;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.dispatchEvent(new Event('change', { bubbles: true }));
  const btn = document.querySelector('.el-button.submitComment')
    || document.querySelector('.publish_discuss .postBtn button')
    || document.querySelector('.el-button--primary');
  if (btn && !btn.disabled && !btn.classList.contains('is-disabled')) {
    btn.click();
    return true;
  }
  return false;
}
"""

# 课件：PPT 页码
PPT_SLIDES = "() => document.querySelector('.swiper-wrapper')?.children?.length || 0"
PPT_CLICK = "(i) => { const s = document.querySelector('.swiper-wrapper')?.children?.[i]; if (s) { s.click(); return true; } return false; }"
VIDEO_BOXES = r"""
() => [...document.querySelectorAll('.video-box')].map((b, i) => ({
  i, text: (b.innerText || '').trim()
}))
"""
VIDEO_BOX_CLICK = "(i) => { const b = document.querySelectorAll('.video-box')[i]; if (b) { b.click(); return true; } return false; }"

# 课件视频：播放器时间显示
XT_TIME = r"""
() => {
  const t = document.querySelector('.xt_video_player_current_time_display')?.innerText || '';
  const parts = t.split('/').map(s => s.trim());
  return { text: t, current: parts[0] || '', total: parts[1] || '' };
}
"""

CHECK_COURSEWARE_BTN = r"""
() => {
  const btn = document.querySelector('.ppt_img_box .check') || document.querySelector('p.check');
  if (btn && (btn.innerText || '').trim() === '查看课件') { btn.click(); return true; }
  return false;
}
"""

