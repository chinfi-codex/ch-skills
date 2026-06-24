// conceptkit_ssr —— conceptkit 组件的"服务端渲染"(Node 侧)。
// 让默认图文网页能把每镜 SVG 直接写进静态 HTML(不靠浏览器 JS 生成)，
// 于是无 JS / GSAP 失效时图仍可见，JS 只负责动画(渐进增强)。
//
// 读 stdin: {"scenes":[{component, spec, beats?}, ...]}
// 写 stdout: [{viewBox, svg, beats}|{error}, ...]，与输入同序。
//
// 脑 / 手边界：只做确定性渲染——执行 conceptkit 的 svg(spec)/viewBox/beats，
// 不产生讲解内容、不下结论。conceptkit.js 与浏览器端是同一份源。
const fs = require("fs");
const path = require("path");

globalThis.window = globalThis; // conceptkit 用 (typeof window!=='undefined'?window:this) 挂 CKIT
const src = fs.readFileSync(path.join(__dirname, "conceptkit.js"), "utf8");
(0, eval)(src); // eslint-disable-line no-eval — 信任的本仓库组件源
const CKIT = globalThis.CKIT || {};

let raw = "";
try { raw = fs.readFileSync(0, "utf8"); } catch (e) { raw = "{}"; }
let scenes = [];
try { scenes = (JSON.parse(raw) || {}).scenes || []; } catch (e) { scenes = []; }

const out = scenes.map(function (sc) {
  const comp = CKIT[sc && sc.component];
  if (!comp) return { error: "unknown conceptkit component: " + (sc && sc.component) };
  const spec = sc.spec || {};
  try {
    const vb = typeof comp.viewBox === "function" ? comp.viewBox(spec) : comp.viewBox;
    const beats = (sc.beats && sc.beats.length) ? sc.beats : comp.beats(spec);
    return { viewBox: String(vb), svg: comp.svg(spec), beats: beats || [] };
  } catch (e) {
    return { error: "render failed for " + sc.component + ": " + (e && e.message) };
  }
});

process.stdout.write(JSON.stringify(out));
