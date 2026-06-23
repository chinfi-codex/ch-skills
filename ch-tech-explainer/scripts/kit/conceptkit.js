/*
 * conceptkit (CKIT) —— 参数化的"知识图元"组件库。每个组件：
 *     svg(spec)     -> 一段 SVG 内容字符串（不含外层 <svg>）
 *     viewBox(spec) -> "0 0 680 H"（可为字符串或函数）
 *     beats(spec)   -> 默认动效编排（motion_spec 六类语法），模型可在 scene.beats 覆盖
 *
 * 脑 / 手边界：组件只负责把 spec 摆成确定性的图形布局 + 给一套默认动效；
 * spec 写什么、归哪个组件、动效怎么调，全部由模型决定。组件不含领域判断。
 *
 * 配色走 assets/explainer.css 里的 .ck 作用域 + --ck-* 主题变量（暗色自适应）。
 * 伪 3D 深度向量统一 dx=16, dy=9（见 motion_spec.md）。
 */
(function (global) {
  var DX = 16, DY = 9;

  function esc(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  // 一个等距立体盒：cls 为正面类名，cls-top / cls-side 为顶 / 侧面。
  function isoBox(x, y, w, h, cls) {
    return '<polygon class="' + cls + '-side" points="' + (x + w) + ',' + y + ' ' + (x + w + DX) + ',' + (y - DY) + ' ' + (x + w + DX) + ',' + (y + h - DY) + ' ' + (x + w) + ',' + (y + h) + '"/>'
      + '<polygon class="' + cls + '-top" points="' + x + ',' + y + ' ' + (x + w) + ',' + y + ' ' + (x + w + DX) + ',' + (y - DY) + ' ' + (x + DX) + ',' + (y - DY) + '"/>'
      + '<rect class="' + cls + '" x="' + x + '" y="' + y + '" width="' + w + '" height="' + h + '" rx="3"/>';
  }
  // 朴素折行（中英混排按宽度估算每行字数）。
  function wrapText(text, x, y, maxW, cls) {
    text = String(text || '');
    var cpl = Math.max(4, Math.floor(maxW / 14));
    var lines = [];
    for (var i = 0; i < text.length; i += cpl) lines.push(text.slice(i, i + cpl));
    if (!lines.length) lines = [''];
    var spans = lines.map(function (ln, i) {
      return '<tspan x="' + x + '" dy="' + (i === 0 ? 0 : 18) + '">' + esc(ln) + '</tspan>';
    }).join('');
    return '<text class="' + cls + '" x="' + x + '" y="' + y + '">' + spans + '</text>';
  }

  var CKIT = {};

  // —— 概念卡：标题 + 类比 + 一个关键 chip —————————————————————————
  CKIT.concept_card = {
    viewBox: '0 0 680 190',
    svg: function (s) {
      s = s || {};
      var out = '<rect class="card" x="40" y="26" width="600" height="140" rx="12"/>';
      out += '<text class="t" x="64" y="60" style="font-size:16px">' + esc(s.title || '') + '</text>';
      out += wrapText(s.analogy || '', 64, 88, 552, 'ts2');
      if (s.chip) {
        var cw = 24 + String(s.chip).length * 13;
        out += '<g class="cchip"><rect class="chip-hl" x="64" y="128" width="' + cw + '" height="26" rx="6"/><text class="ti" x="76" y="145">' + esc(s.chip) + '</text></g>';
      }
      return out;
    },
    beats: function () {
      return [
        { at: 0.2, target: '.card', action: 'introduce', from: { y: 14 }, dur: 0.5 },
        { at: 0.7, target: '.cchip', action: 'introduce', from: { y: 8 }, dur: 0.4 }
      ];
    }
  };

  // —— 破墙：痛点 → 墙 → 解 ————————————————————————————————
  CKIT.breakwall = {
    viewBox: '0 0 680 150',
    svg: function (s) {
      s = s || {};
      var items = [
        { cls: 'pill', label: s.driver || '需求 ↑' },
        { cls: 'pill', label: s.wall || '撞上墙' },
        { cls: 'pill-hl', label: s.solution || '技术破墙' }
      ];
      var x = 54, y = 56, out = '';
      items.forEach(function (it, i) {
        var cw = 28 + it.label.length * 15;
        out += '<g class="bw"><rect class="' + it.cls + '" x="' + x + '" y="' + y + '" width="' + cw + '" height="40" rx="8"/>'
          + '<text class="' + (it.cls === 'pill-hl' ? 'ti' : 't') + '" x="' + (x + cw / 2) + '" y="' + (y + 25) + '" text-anchor="middle">' + esc(it.label) + '</text></g>';
        x += cw;
        if (i < items.length - 1) { out += '<text class="t" x="' + (x + 18) + '" y="' + (y + 26) + '" text-anchor="middle">→</text>'; x += 36; }
      });
      if (s.note) out += '<text class="ts" x="54" y="124">' + esc(s.note) + '</text>';
      return out;
    },
    beats: function () {
      return [{ at: 0.2, target: '.bw', action: 'introduce', from: { x: -14 }, dur: 0.45, stagger: 0.4 }];
    }
  };

  // —— 堆叠层：等距叠片 + TSV + 邻接主芯片 + 数据通道 + 指标条 ——————————
  CKIT.stack_layers = {
    viewBox: '0 0 680 300',
    svg: function (s) {
      s = s || {};
      var layers = s.layers || ['DRAM die', 'DRAM die', 'DRAM die', 'DRAM die'];
      var topLabel = s.topLabel || layers[layers.length - 1];
      var baseLabel = s.baseLabel || 'base die';
      var peer = s.peer || { label: 'GPU', sub: '' };
      var metric = s.metric || { label: '指标', note: '' };
      var x = 110, w = 150, h = 22, step = 26, baseY = 198, n = layers.length;
      var out = '';
      out += '<g class="die">' + isoBox(x, baseY, w, h, 'bc') + '<text class="ts" x="' + (x + w / 2) + '" y="' + (baseY + 14) + '" text-anchor="middle">' + esc(baseLabel) + '</text></g>';
      var topY = baseY;
      for (var i = 0; i < n; i++) {
        var yy = baseY - step * (i + 1); topY = yy;
        var lbl = (i === n - 1) ? topLabel : '';
        out += '<g class="die">' + isoBox(x, yy, w, h, 'fc') + (lbl ? '<text class="ti" x="' + (x + w / 2) + '" y="' + (yy + 14) + '" text-anchor="middle">' + esc(lbl) + '</text>' : '') + '</g>';
      }
      var c1 = x + 40, c2 = x + w - 45;
      out += '<line class="tsv" x1="' + c1 + '" y1="' + topY + '" x2="' + c1 + '" y2="' + (baseY + h) + '"/>';
      out += '<line class="tsv" x1="' + c2 + '" y1="' + topY + '" x2="' + c2 + '" y2="' + (baseY + h) + '"/>';
      out += '<text class="ts" x="' + (x - 6) + '" y="' + ((topY + baseY) / 2) + '" text-anchor="end">' + esc(s.connectorLabel || 'TSV') + '</text>';
      var px = 430, py = 96, pw = 120, ph = 120;
      out += '<g>' + isoBox(px, py, pw, ph, 'gpu') + '<text class="t" x="' + (px + pw / 2) + '" y="' + (py + 54) + '" text-anchor="middle">' + esc(peer.label) + '</text>'
        + (peer.sub ? '<text class="ts" x="' + (px + pw / 2) + '" y="' + (py + 74) + '" text-anchor="middle">' + esc(peer.sub) + '</text>' : '') + '</g>';
      if (s.channel !== false) {
        out += '<rect class="chan" x="282" y="142" width="146" height="32" rx="4"/>';
        out += '<text class="ts" x="355" y="136" text-anchor="middle">' + esc(s.channelLabel || '数据流 → 带宽') + '</text>';
        out += '<line class="flow" x1="286" y1="150" x2="424" y2="150"/><line class="flow" x1="286" y1="158" x2="424" y2="158"/><line class="flow" x1="286" y1="166" x2="424" y2="166"/>';
      }
      out += '<text class="ts" x="110" y="246">' + esc(metric.label) + '</text>';
      out += '<rect class="track" x="110" y="252" width="260" height="12" rx="6"/>';
      out += '<rect class="bar-fill" x="110" y="252" width="260" height="12" rx="6"/>';
      if (metric.note) out += '<text class="ti" x="378" y="262">' + esc(metric.note) + '</text>';
      return out;
    },
    beats: function (s) {
      var b = [
        { at: 0.2, target: '.die', action: 'introduce', from: { y: -30 }, dur: 0.55, stagger: 0.4 },
        { at: 2.4, target: '.tsv', action: 'reveal', dur: 0.6 }
      ];
      if (!s || s.channel !== false) {
        b.push({ at: 2.9, target: '.chan', action: 'introduce', from: { y: 0 }, dur: 0.5 });
        b.push({ at: 3.1, target: '.flow', action: 'flow', distance: 140, dur: 2.6 });
        b.push({ at: 3.1, target: '.bar-fill', action: 'contrast', dur: 0.9 });
      }
      return b;
    }
  };

  // —— 新旧对比条 ————————————————————————————————————————
  CKIT.compare_bars = {
    viewBox: function (s) {
      var n = ((s || {}).bars || []).length;
      return '0 0 680 ' + (((s && s.title) ? 48 : 24) + n * 54 + 16);
    },
    svg: function (s) {
      s = s || {};
      var bars = s.bars || [], x = 170, w = 380, rowH = 54;
      var out = '';
      if (s.title) out += '<text class="t" x="40" y="28">' + esc(s.title) + '</text>';
      bars.forEach(function (b, i) {
        var y = (s.title ? 52 : 28) + i * rowH;
        out += '<text class="ts" x="40" y="' + (y + 4) + '">' + esc(b.label) + '</text>';
        out += '<rect class="track" x="' + x + '" y="' + (y - 13) + '" width="' + w + '" height="24" rx="6"/>';
        var fw = Math.max(0.02, Math.min(1, b.value || 0));
        out += '<rect class="cbar ' + (b.kind === 'old' ? 'bar-old' : 'bar-new') + '" x="' + x + '" y="' + (y - 13) + '" width="' + (w * fw) + '" height="24" rx="6"/>';
        out += '<text class="' + (b.kind === 'old' ? 'ts' : 'ti') + '" x="' + (x + w + 8) + '" y="' + (y + 4) + '">' + esc(b.valueText || '') + '</text>';
      });
      return out;
    },
    beats: function () {
      return [{ at: 0.2, target: '.cbar', action: 'contrast', dur: 0.7, stagger: 0.25 }];
    }
  };

  // —— 产业链流：上中下游 + 箭头 + 高亮环节 ————————————————————
  CKIT.supply_chain = {
    viewBox: '0 0 680 210',
    svg: function (s) {
      s = s || {};
      var nodes = s.nodes || [], n = nodes.length;
      var gap = 12, aw = 24, x0 = 40, total = 600;
      var colW = n ? (total - (n - 1) * (gap + aw)) / n : total;
      var y = 70, h = 100, out = '';
      if (s.title) out += '<text class="t" x="40" y="30">' + esc(s.title) + '</text>';
      var cx = x0;
      nodes.forEach(function (nd, i) {
        var hl = nd.highlight;
        out += '<g class="node"><rect class="' + (hl ? 'col-hl' : 'col') + '" x="' + cx + '" y="' + y + '" width="' + colW + '" height="' + h + '" rx="8"/>';
        out += '<text class="ts" x="' + (cx + 12) + '" y="' + (y + 22) + '">' + esc(nd.tier || '') + '</text>';
        out += wrapText(nd.label, cx + 12, y + 44, colW - 24, hl ? 'ti' : 't');
        if (nd.note) out += '<text class="ts" x="' + (cx + 12) + '" y="' + (y + h - 12) + '">' + esc(nd.note) + '</text>';
        out += '</g>';
        if (i < n - 1) out += '<text class="t" x="' + (cx + colW + (gap + aw) / 2) + '" y="' + (y + h / 2 + 5) + '" text-anchor="middle">→</text>';
        cx += colW + gap + aw;
      });
      return out;
    },
    beats: function () {
      return [
        { at: 0.2, target: '.node', action: 'introduce', from: { x: -16 }, dur: 0.5, stagger: 0.3 },
        { at: 1.6, target: '.col-hl', action: 'emphasize', dur: 0.7 }
      ];
    }
  };

  // —— 时间线 / 路线图 + 信号 chip ————————————————————————
  CKIT.timeline = {
    viewBox: '0 0 680 200',
    svg: function (s) {
      s = s || {};
      var ms = s.milestones || [], signals = s.signals || [];
      var out = '';
      if (s.title) out += '<text class="t" x="40" y="28">' + esc(s.title) + '</text>';
      var x0 = 70, x1 = 610, y = 76, n = ms.length;
      out += '<line class="axis" x1="' + x0 + '" y1="' + y + '" x2="' + x1 + '" y2="' + y + '"/>';
      ms.forEach(function (m, i) {
        var mx = x0 + (n <= 1 ? (x1 - x0) / 2 : (x1 - x0) * i / (n - 1));
        out += '<g class="ms"><circle class="' + (m.current ? 'dot-cur' : 'dot') + '" cx="' + mx + '" cy="' + y + '" r="6"/>';
        out += '<text class="' + (m.current ? 'ti' : 'ts') + '" x="' + mx + '" y="' + (y - 14) + '" text-anchor="middle">' + esc(m.label) + '</text>';
        if (m.current) out += '<text class="ts" x="' + mx + '" y="' + (y + 22) + '" text-anchor="middle">当下</text>';
        out += '</g>';
      });
      var sx = 70, sy = 116;
      signals.forEach(function (sg) {
        var cw = 20 + String(sg).length * 13;
        if (sx + cw > 640) { sx = 70; sy += 34; }
        out += '<g class="sig"><rect class="chip" x="' + sx + '" y="' + sy + '" width="' + cw + '" height="26" rx="6"/><text class="ts" x="' + (sx + 10) + '" y="' + (sy + 17) + '">' + esc(sg) + '</text></g>';
        sx += cw + 10;
      });
      return out;
    },
    beats: function () {
      return [
        { at: 0.2, target: '.ms', action: 'reveal', dur: 0.5, stagger: 0.3 },
        { at: 1.4, target: '.sig', action: 'introduce', from: { y: 10 }, dur: 0.4, stagger: 0.15 }
      ];
    }
  };

  CKIT._helpers = { esc: esc, isoBox: isoBox, wrapText: wrapText };
  global.CKIT = CKIT;
})(typeof window !== 'undefined' ? window : this);
