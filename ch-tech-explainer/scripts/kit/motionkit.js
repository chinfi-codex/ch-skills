/*
 * motionkit (MK) —— 把 motion_spec.md 的六类动效语法落成可执行的时间轴构造器。
 *
 * 脑 / 手边界：这里只做"把 beat 翻译成 GSAP 补间"的确定性插值。什么时候、对谁、
 * 为什么动(教学编排)由模型写在 scene.beats 里；本文件不含任何讲解内容与领域判断。
 *
 * 用法：MK.buildTimeline(gsap, rootSvgEl, beats) -> 一条有限、可 seek 的 GSAP timeline。
 *   beat = { at, target, action, ... }
 *     target : 相对 root 的 CSS 选择器（如 ".die" / ".bar-fill"）
 *     action : introduce | emphasize | contrast | transform | flow | reveal
 */
(function (global) {
  function prefersReduce() {
    return !!(global.matchMedia && global.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }
  function sel(root, s) {
    return Array.prototype.slice.call(root.querySelectorAll(s));
  }

  // 把一个 beat 映射成 timeline 上的补间。每个 case 对应 motion_spec 的一类语法。
  function applyBeat(gsap, tl, root, b) {
    var t = sel(root, b.target);
    if (!t.length) return;
    var at = b.at || 0;
    var dur = b.dur || 0.5;
    var ease = b.ease || 'power2.out';
    var stagger = b.stagger || 0;
    var from = b.from || {};

    switch (b.action) {
      case 'introduce': // 引入新对象：淡入 + 轻微位移
        tl.from(t, {
          opacity: 0,
          y: from.y != null ? from.y : -20,
          x: from.x != null ? from.x : 0,
          duration: dur, ease: ease, stagger: stagger
        }, at);
        break;

      case 'reveal': // 渐进披露：能描边的元素走线，其它淡入
        t.forEach(function (el) {
          var drew = false;
          if (el.getTotalLength) {
            try { var L = el.getTotalLength(); if (L > 0) { gsap.set(el, { strokeDasharray: L, strokeDashoffset: L }); drew = true; } } catch (e) {}
          }
          if (!drew) gsap.set(el, { opacity: 0 });
        });
        tl.to(t, { strokeDashoffset: 0, opacity: 1, duration: dur, ease: ease, stagger: stagger }, at);
        break;

      case 'emphasize': // 强调："现在看这里"，克制的脉冲
        tl.to(t, { scale: b.scale || 1.06, duration: dur * 0.5, ease: 'sine.inOut', transformOrigin: b.origin || '50% 50%', yoyo: true, repeat: 1 }, at);
        break;

      case 'contrast': // 对比 / 量化：横向生长（条形）
        gsap.set(t, { scaleX: 0, transformOrigin: '0% 50%' });
        tl.to(t, { scaleX: 1, duration: dur, ease: ease, stagger: stagger }, at);
        break;

      case 'transform': // 形变：位移 / 缩放到目标态
        tl.to(t, Object.assign({ duration: dur, ease: ease, stagger: stagger }, b.to || {}), at);
        break;

      case 'flow': // 流动：虚线沿路径平移（有限位移，循环时无缝）
        gsap.set(t, { opacity: 0, strokeDasharray: b.dash || '7 7' });
        tl.to(t, { opacity: 1, duration: 0.3 }, at);
        tl.to(t, { strokeDashoffset: '-=' + (b.distance || 140), duration: b.dur || 2.4, ease: 'none' }, at);
        break;

      default:
        tl.from(t, { opacity: 0, duration: dur, ease: ease, stagger: stagger }, at);
    }
  }

  function buildTimeline(gsap, root, beats, opts) {
    opts = opts || {};
    var tl = gsap.timeline({ paused: true, defaults: { ease: 'power2.out' } });
    (beats || []).forEach(function (b) { applyBeat(gsap, tl, root, b); });
    // 可达性：reduced-motion 直接落静态末帧（关键信息必须在末帧可见）。
    if (prefersReduce()) tl.progress(1).pause();
    return tl;
  }

  global.MK = { buildTimeline: buildTimeline, applyBeat: applyBeat, prefersReduce: prefersReduce, sel: sel };
})(typeof window !== 'undefined' ? window : this);
