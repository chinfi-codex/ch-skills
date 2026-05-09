#!/usr/bin/env node
/**
 * 健康管家看板构建脚本 v4 — 完全参照下载的 HTML 设计
 * 明亮暖色调主题（Claude 暖橘黄主调）
 *
 * 注意：这是整页模板重建器，会覆盖 dashboard/index.html 的手工 HTML/CSS 改动。
 * 日常数据刷新请使用 scripts/refresh_dashboard_data.py。
 */

const fs = require('fs');
const path = require('path');

const DATA_DIR = path.join(__dirname, 'data', 'daily');
const PROFILE_FILE = path.join(__dirname, 'profile.json');
const OUTPUT_FILE = path.join(__dirname, 'dashboard', 'index.html');

const BMR = 1544;
const DAILY_BASE = 1853;  // BMR × 1.2，纯久坐不含运动
const TARGET_DEFICIT = 500;
const PLAN_INTAKE = 1600;
const DASHBOARD_TIME_ZONE = 'Asia/Shanghai';

function getYMDInTimeZone(date = new Date(), timeZone = DASHBOARD_TIME_ZONE) {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit'
  }).formatToParts(date);
  const values = Object.fromEntries(parts.map(part => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}

function formatDateInTimeZone(date = new Date(), timeZone = DASHBOARD_TIME_ZONE) {
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone,
    year: 'numeric',
    month: 'long',
    day: 'numeric'
  }).format(date);
}

function formatDateTimeInTimeZone(date = new Date(), timeZone = DASHBOARD_TIME_ZONE) {
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone,
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false
  }).format(date);
}

function loadData() {
  const files = fs.readdirSync(DATA_DIR).filter(f => f.endsWith('.json')).sort();
  const profileHeightCm = loadProfileHeightCm();
  const data = {};
  for (const file of files) {
    const raw = JSON.parse(fs.readFileSync(path.join(DATA_DIR, file), 'utf-8'));
    data[raw.date] = processData(raw, profileHeightCm);
  }
  return data;
}

function loadProfileHeightCm() {
  if (!fs.existsSync(PROFILE_FILE)) return null;
  try {
    const profile = JSON.parse(fs.readFileSync(PROFILE_FILE, 'utf-8'));
    return profile?.basic?.height_cm || null;
  } catch {
    return null;
  }
}

function calculateBmi(weightKg, heightCm, fallback = null) {
  if (weightKg && heightCm) {
    const heightM = heightCm / 100;
    if (heightM > 0) return Math.round((weightKg / (heightM * heightM)) * 10) / 10;
  }
  return fallback;
}

function processData(raw, profileHeightCm = null) {
  const intake = raw.nutrition_summary?.total_calories || 0;
  const target = raw.nutrition_summary?.target_calories || PLAN_INTAKE;
  const heightCm = raw.profile_snapshot?.height_cm || profileHeightCm;
  const weightKg = raw.profile_snapshot?.weight_kg;
  const exerciseMinutes = raw.health_markers?.exercise_minutes || (raw.exercise?.duration_min || 0);
  const exerciseType = raw.exercise?.category || raw.exercise?.type || '';

  let exerciseLabel = '';
  if (raw.exercise?.items?.length > 0) {
    exerciseLabel = raw.exercise.items.map(i => i.name).join(' / ');
  } else if (raw.exercise?.type && raw.exercise.type !== '休息') {
    exerciseLabel = raw.exercise.type;
  } else if (raw.exercise?.category) {
    exerciseLabel = raw.exercise.category;
  }

  let exerciseBurn = raw.health_markers?.active_energy_kcal || 0;
  if (!exerciseBurn && exerciseMinutes > 0) {
    const kcalPerMin = exerciseType.includes('有氧') || exerciseType.includes('快走') || exerciseType.includes('跑步') ? 5 : 4;
    exerciseBurn = Math.round(exerciseMinutes * kcalPerMin);
  }

  return {
    date: raw.date,
    weekday: raw.weekday,
    weekdayShort: raw.weekday.replace('周', ''),
    heightCm,
    weight: weightKg,
    bodyFat: raw.profile_snapshot?.body_fat_pct,
    bmi: calculateBmi(weightKg, heightCm, raw.profile_snapshot?.bmi),
    intake,
    targetIntake: target,
    exerciseBurn,
    exerciseMinutes,
    exerciseType: raw.exercise?.category || raw.exercise?.type,
    exerciseLabel,
    exerciseDone: raw.exercise?.status === 'completed',
    water: raw.health_markers?.water_intake_ml || raw.nutrition_summary?.water_ml || 0,
    waterTarget: raw.health_markers?.water_target_ml || 2000,
    waterPct: Math.round((raw.health_markers?.water_intake_ml || raw.nutrition_summary?.water_ml || 0) / 2000 * 100),
    meals: raw.meals,
    steps: raw.health_markers?.steps,
    protein: raw.nutrition_summary?.protein_g,
    carbs: raw.nutrition_summary?.carbs_g,
    fat: raw.nutrition_summary?.fat_g
  };
}

function generateHTML(dataMap) {
  const dates = Object.keys(dataMap).sort();
  const latest = dataMap[dates[dates.length - 1]];
  const previous = dates.length > 1 ? dataMap[dates[dates.length - 2]] : null;
  const allDataJSON = JSON.stringify(dataMap);

  // Scan backward for latest non-null values
  function latestField(field) {
    for (let i = dates.length - 1; i >= 0; i--) {
      const val = dataMap[dates[i]][field];
      if (val !== null && val !== undefined) return val;
    }
    return null;
  }
  const latestBodyFat = latestField('bodyFat');
  const latestBmi = latestField('bmi');

  const weightChange = previous && latest.weight
    ? `${latest.weight < previous.weight ? '↓' : (latest.weight > previous.weight ? '↑' : '→')} ${Math.abs(latest.weight - previous.weight).toFixed(1)}kg`
    : '建档首日';

  const weightChangeClass = previous && latest.weight
    ? (latest.weight < previous.weight ? 'down' : (latest.weight > previous.weight ? 'up' : 'neutral'))
    : 'neutral';

  const todayStr = formatDateInTimeZone();
  const updateTime = formatDateTimeInTimeZone().replace(/\//g, '/');
  const todayYMD = getYMDInTimeZone();

  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>主人健康看板</title>
  <style>
    /* ===== 设计系统：明亮健康主题（Claude 暖橘黄主调） ===== */
    :root {
      --bg-page: #FBF7F1;
      --bg-page-2: #F5EEE2;
      --bg-card: #FFFFFF;
      --bg-card-hover: #FEFCF8;
      --bg-block: #F8F2E6;
      --bg-block-hover: #F1E8D5;
      --bg-empty: #FBF7EE;

      --border: #EFE4D0;
      --border-light: #E2D2B5;
      --border-highlight: #D97757;

      --text-primary: #2B1D10;
      --text-secondary: #6B5640;
      --text-tertiary: #9B8870;

      --accent-amber: #E8A33D;
      --accent-amber-deep: #C8821C;
      --accent-amber-soft: #FBEFD3;

      --accent-clay: #D97757;
      --accent-clay-deep: #B85A3C;
      --accent-clay-soft: #FBE4D9;

      --accent-sand: #C9A961;
      --accent-sand-soft: #F4E9CE;

      --accent-mint: #6FBE9F;
      --accent-mint-deep: #4FA383;
      --accent-mint-soft: #DCEFE5;

      --accent-sky: #6BA8C9;
      --accent-sky-deep: #4889AD;
      --accent-sky-soft: #DCE8F0;

      --accent-coral: #E08566;
      --accent-coral-deep: #C56747;
      --accent-coral-soft: #FAE0D4;

      --accent-lavender: #A893C9;
      --accent-lavender-soft: #ECE4F4;

      --accent-green: #6FBE9F;
      --accent-green-deep: #4FA383;
      --accent-green-soft: #DCEFE5;

      --shadow: 0 1px 2px rgba(120, 80, 30, 0.05);
      --shadow-md: 0 4px 14px rgba(120, 80, 30, 0.08);
      --shadow-lg: 0 12px 32px rgba(120, 80, 30, 0.10);
      --shadow-amber: 0 8px 24px rgba(232, 163, 61, 0.22);
      --shadow-green: 0 8px 24px rgba(111, 190, 159, 0.18);

      --radius-sm: 6px;
      --radius-md: 10px;
      --radius-lg: 14px;
      --radius-xl: 18px;
    }

    * { margin: 0; padding: 0; box-sizing: border-box; }

    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', sans-serif;
      background: var(--bg-page);
      min-height: 100vh;
      color: var(--text-primary);
      -webkit-font-smoothing: antialiased;
    }

    /* ===== 品牌栏 ===== */
    .brand-bar {
      max-width: 1400px;
      margin: 0 auto;
      padding: 22px 24px 4px;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 10px;
      font-weight: 700;
      letter-spacing: 0.2px;
    }
    .brand-logo {
      width: 32px; height: 32px;
      border-radius: 10px;
      background: var(--accent-amber);
      display: grid; place-items: center;
      color: #fff;
      font-size: 16px;
      box-shadow: var(--shadow-amber);
    }
    .brand-title { font-size: 1rem; color: var(--text-primary); }
    .brand-sub { font-size: 0.75rem; color: var(--text-tertiary); font-weight: 500; }
    .brand-meta {
      font-size: 0.75rem;
      color: var(--text-tertiary);
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .pulse-dot {
      width: 8px; height: 8px; border-radius: 50%;
      background: var(--accent-amber);
      box-shadow: 0 0 0 0 rgba(232, 163, 61, 0.6);
      animation: pulse 2s infinite;
    }
    @keyframes pulse {
      0% { box-shadow: 0 0 0 0 rgba(232, 163, 61, 0.5); }
      70% { box-shadow: 0 0 0 8px rgba(232, 163, 61, 0); }
      100% { box-shadow: 0 0 0 0 rgba(232, 163, 61, 0); }
    }

    /* ===== 顶部状态卡 ===== */
    .top-bar { padding: 12px 24px 16px; }

    .stats-row {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 14px;
      max-width: 1400px;
      margin: 0 auto;
    }

    .stat-card {
      position: relative;
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      padding: 18px 20px;
      box-shadow: var(--shadow-md);
      display: flex;
      flex-direction: column;
      justify-content: center;
      min-height: 110px;
      transition: transform 0.25s ease, box-shadow 0.25s ease, border-color 0.25s ease;
      overflow: hidden;
    }
    .stat-card::before {
      content: '';
      position: absolute;
      top: 0; left: 0; right: 0;
      height: 3px;
      background: var(--card-accent, var(--accent-green));
      opacity: 0.9;
    }
    .stat-card:hover {
      transform: translateY(-3px);
      box-shadow: var(--shadow-lg);
      border-color: var(--border-light);
    }
    .stat-card .label {
      font-size: 0.7rem;
      color: var(--text-tertiary);
      text-transform: uppercase;
      letter-spacing: 1.4px;
      margin-bottom: 8px;
      font-weight: 600;
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .stat-card .label .ico {
      width: 18px; height: 18px;
      border-radius: 6px;
      background: var(--card-accent-soft, var(--accent-green-soft));
      color: var(--card-accent, var(--accent-green));
      display: grid; place-items: center;
      font-size: 11px;
    }
    .stat-card .value {
      font-size: 1.8rem;
      font-weight: 800;
      color: var(--text-primary);
      line-height: 1.1;
      letter-spacing: -0.5px;
    }
    .stat-card .unit {
      font-size: 0.75rem;
      color: var(--text-secondary);
      font-weight: 500;
      margin-left: 4px;
    }
    .stat-card .sub {
      font-size: 0.72rem;
      margin-top: 8px;
      font-weight: 600;
      color: var(--text-secondary);
      display: flex;
      align-items: center;
      gap: 4px;
    }

    .stat-card.c-sky   { --card-accent: var(--accent-sky);   --card-accent-soft: var(--accent-sky-soft); }
    .stat-card.c-coral { --card-accent: var(--accent-coral); --card-accent-soft: var(--accent-coral-soft); }
    .stat-card.c-amber { --card-accent: var(--accent-amber); --card-accent-soft: var(--accent-amber-soft); }
    .stat-card.c-lavender { --card-accent: var(--accent-lavender); --card-accent-soft: var(--accent-lavender-soft); }
    .stat-card.c-mint  { --card-accent: var(--accent-mint);  --card-accent-soft: var(--accent-mint-soft); }

    .stat-card.highlight {
      background: var(--accent-amber-soft);
      border: 1px solid rgba(232, 163, 61, 0.4);
      box-shadow: var(--shadow-amber);
    }
    .stat-card.highlight::before { background: var(--accent-amber-deep); }
    .stat-card.highlight .label { color: var(--accent-amber-deep); }
    .stat-card.highlight .label .ico { background: rgba(255,255,255,0.75); color: var(--accent-amber-deep); }
    .stat-card.highlight .value { color: var(--accent-amber-deep); }
    .stat-card.highlight .unit { color: var(--accent-amber-deep); opacity: 0.7; }
    .stat-card.highlight .sub { color: var(--accent-amber-deep); }

    .sub.down { color: var(--accent-green-deep); }
    .sub.up { color: var(--accent-coral-deep); }
    .sub.neutral { color: var(--text-tertiary); }

    /* ===== 日历容器 ===== */
    .calendar-shell {
      max-width: 1400px;
      margin: 12px auto 24px;
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: var(--radius-xl);
      box-shadow: var(--shadow-md);
      overflow: hidden;
      position: relative;
    }
    .calendar-shell::before {
      content: '';
      position: absolute;
      inset: 0 0 auto 0;
      height: 3px;
      background: var(--accent-amber);
      opacity: 0.9;
    }
    .calendar-shell-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 18px 24px 4px;
      gap: 16px;
    }
    .calendar-shell-title {
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .calendar-shell-title .ttl {
      font-size: 1.05rem;
      font-weight: 800;
      color: var(--text-primary);
      letter-spacing: -0.2px;
    }
    .calendar-shell-title .sub {
      font-size: 0.75rem;
      color: var(--text-tertiary);
      font-weight: 500;
    }
    .calendar-shell-title .chip {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      font-size: 0.7rem;
      font-weight: 700;
      color: var(--accent-amber-deep);
      background: var(--accent-amber-soft);
      padding: 4px 10px;
      border-radius: 999px;
    }

    /* ===== 日历控制栏 ===== */
    .calendar-control {
      display: flex;
      align-items: center;
      gap: 12px;
      color: var(--text-secondary);
    }
    .calendar-control button {
      background: var(--bg-card);
      border: 1px solid var(--border);
      color: var(--text-secondary);
      width: 38px;
      height: 38px;
      border-radius: 50%;
      cursor: pointer;
      font-size: 0.9rem;
      transition: all 0.2s;
      display: flex;
      align-items: center;
      justify-content: center;
      box-shadow: var(--shadow);
    }
    .calendar-control button:hover {
      background: var(--accent-amber);
      border-color: var(--accent-amber);
      color: #fff;
      transform: translateY(-1px);
      box-shadow: var(--shadow-amber);
    }
    .calendar-control .week-label {
      font-size: 0.9rem;
      font-weight: 700;
      min-width: 160px;
      text-align: center;
      color: var(--text-primary);
      letter-spacing: 0.3px;
      padding: 7px 16px;
      background: var(--bg-block);
      border: 1px solid var(--border);
      border-radius: 999px;
    }

    /* ===== 日历横向滚动容器 ===== */
    .calendar-wrapper {
      padding: 16px 20px 22px;
      overflow-x: auto;
      overflow-y: hidden;
      scroll-snap-type: x mandatory;
      -webkit-overflow-scrolling: touch;
      scrollbar-width: none;
    }
    .calendar-wrapper::-webkit-scrollbar { display: none; }
    .week-grid {
      display: flex;
      flex-wrap: nowrap;
      gap: 10px;
      min-width: 100%;
    }
    .calendar-wrapper::after {
      content: '⟷';
      display: block;
      text-align: center;
      font-size: 0.75rem;
      color: var(--text-tertiary);
      padding-top: 6px;
      opacity: 0.6;
    }
    .calendar-wrapper.has-scrolled::after {
      opacity: 0;
      transition: opacity 0.3s ease;
    }

    /* 日期列卡片 — 一页3个 */
    .day-column {
      flex: 0 0 calc(33.333% - 7px);
      min-width: 280px;
      scroll-snap-align: start;
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      overflow: hidden;
      min-height: 480px;
      display: flex;
      flex-direction: column;
      transition: transform 0.25s ease, box-shadow 0.25s ease, border-color 0.25s ease;
      box-shadow: var(--shadow);
    }
    .day-column:hover {
      border-color: var(--border-light);
      box-shadow: var(--shadow-md);
      transform: translateY(-2px);
    }
    .day-column.center {
      border-color: var(--accent-clay);
      box-shadow: 0 4px 16px rgba(217, 119, 87, 0.15);
    }
    .day-column.center .day-header {
      background: var(--accent-clay-soft);
    }
    .day-column.today {
      border: 1px solid var(--accent-amber);
      box-shadow: 0 0 0 3px var(--accent-amber-soft), var(--shadow-amber);
    }
    .day-column.empty {
      background: var(--bg-empty);
      border: 1px dashed var(--border-light);
      box-shadow: none;
    }

    /* 日期头部 */
    .day-header {
      padding: 14px 12px 12px;
      text-align: center;
      border-bottom: 1px solid var(--border);
      background: var(--bg-block);
      position: relative;
    }
    .day-column.today .day-header {
      background: var(--accent-amber-soft);
    }
    .day-header .weekday {
      font-size: 0.65rem;
      color: var(--text-tertiary);
      font-weight: 700;
      letter-spacing: 1.5px;
      text-transform: uppercase;
    }
    .day-column.today .day-header .weekday { color: var(--accent-amber-deep); }
    .day-header .date-num {
      font-size: 1.6rem;
      font-weight: 800;
      color: var(--text-primary);
      margin-top: 2px;
      line-height: 1.1;
      letter-spacing: -0.5px;
    }
    .day-column.today .day-header .date-num { color: var(--accent-amber-deep); }
    .day-header .weight-tag {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      font-size: 0.7rem;
      color: var(--accent-coral-deep);
      background: var(--accent-coral-soft);
      padding: 2px 8px;
      border-radius: 999px;
      margin-top: 6px;
      font-weight: 700;
    }

    /* 数据区块 */
    .day-body {
      flex: 1;
      padding: 12px;
      display: flex;
      flex-direction: column;
      gap: 8px;
      overflow-y: auto;
    }
    .data-block {
      background: var(--bg-block);
      border: 1px solid transparent;
      border-radius: var(--radius-md);
      padding: 10px 12px;
      transition: background 0.2s, border-color 0.2s;
    }
    .data-block:hover {
      background: var(--bg-block-hover);
      border-color: var(--border);
    }
    .exercise-block {
      cursor: default;
    }
    .exercise-block:hover {
      border-color: rgba(214, 106, 88, 0.32);
      box-shadow: 0 8px 18px rgba(214, 106, 88, 0.10);
    }
    .data-block-title {
      font-size: 0.7rem;
      color: var(--text-secondary);
      margin-bottom: 6px;
      display: flex;
      align-items: center;
      gap: 6px;
      font-weight: 600;
      letter-spacing: 0.3px;
    }
    .data-block-value {
      font-size: 1rem;
      font-weight: 700;
      color: var(--text-primary);
      letter-spacing: -0.2px;
    }
    .data-block-sub {
      font-size: 0.7rem;
      color: var(--text-tertiary);
      margin-top: 4px;
      line-height: 1.5;
    }

    /* 缺口条 */
    .deficit-bar {
      height: 6px;
      background: #E8F0EB;
      border-radius: 999px;
      overflow: hidden;
      margin-top: 8px;
    }
    .deficit-fill {
      height: 100%;
      border-radius: 999px;
      background: var(--accent-green);
      transition: width 0.8s ease;
    }
    .deficit-fill.under {
      background: var(--accent-amber);
    }
    .deficit-badge {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 4px 10px;
      border-radius: 999px;
      font-size: 0.7rem;
      font-weight: 700;
      margin-top: 8px;
      letter-spacing: 0.3px;
    }
    .deficit-badge.ok {
      background: var(--accent-green-soft);
      color: var(--accent-green-deep);
    }
    .deficit-badge.under {
      background: var(--accent-amber-soft);
      color: #B07810;
    }

    /* 饮水条 */
    .water-bar {
      height: 6px;
      background: #E5F1F8;
      border-radius: 999px;
      overflow: hidden;
      margin-top: 8px;
    }
    .water-fill {
      height: 100%;
      border-radius: 999px;
      background: var(--accent-sky);
      transition: width 0.8s ease;
    }

    /* 餐食项 */
    .meal-items {
      font-size: 0.72rem;
      color: var(--text-secondary);
      line-height: 1.6;
      margin-top: 4px;
    }
    .meal-items .meal-line {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      padding: 4px 0;
      border-bottom: 1px dashed var(--border);
    }
    .meal-items .meal-line:last-child {
      border-bottom: none;
    }
    .meal-items .meal-cal {
      color: var(--accent-coral);
      font-size: 0.68rem;
      font-weight: 700;
      white-space: nowrap;
    }

    /* 运动标签 */
    .exercise-tag {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 4px 10px;
      border-radius: 999px;
      font-size: 0.72rem;
      font-weight: 700;
      margin-top: 4px;
      letter-spacing: 0.2px;
    }
    .exercise-done {
      background: var(--accent-green-soft);
      color: var(--accent-green-deep);
    }
    .exercise-pending {
      background: var(--accent-amber-soft);
      color: #B07810;
    }
    .exercise-plan-popover {
      position: fixed;
      left: 0;
      top: 0;
      width: min(320px, calc(100vw - 32px));
      padding: 14px;
      border: 1px solid rgba(214, 106, 88, 0.22);
      border-radius: var(--radius-lg);
      background: var(--bg-card);
      box-shadow: var(--shadow-lg);
      opacity: 0;
      pointer-events: none;
      transform: translate3d(0, -4px, 0);
      transition: opacity 0.16s ease, transform 0.16s ease;
      z-index: 100;
    }
    .exercise-plan-popover.active {
      opacity: 1;
      transform: translate3d(0, 0, 0);
    }
    .exercise-plan-title {
      margin-bottom: 10px;
      color: var(--text-primary);
      font-size: 0.92rem;
      font-weight: 800;
    }
    .exercise-plan-main {
      padding: 10px 11px;
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      background: var(--bg-block);
      color: var(--text-primary);
      font-size: 0.82rem;
      font-weight: 800;
      line-height: 1.45;
    }
    .exercise-plan-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
      margin-top: 10px;
    }
    .exercise-plan-stat {
      padding: 8px 9px;
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      background: var(--bg-card-hover);
    }
    .exercise-plan-stat span {
      display: block;
      margin-bottom: 3px;
      color: var(--text-tertiary);
      font-size: 0.66rem;
      font-weight: 700;
    }
    .exercise-plan-stat strong {
      color: var(--text-primary);
      font-size: 0.78rem;
      font-weight: 800;
    }
    .exercise-plan-note {
      margin-top: 10px;
      color: var(--text-secondary);
      font-size: 0.76rem;
      font-weight: 600;
      line-height: 1.5;
    }

    /* 状态点 */
    .status-dot {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      display: inline-block;
      box-shadow: 0 0 0 2px rgba(255,255,255,0.6);
    }
    .dot-ok { background: var(--accent-green); }
    .dot-warn { background: var(--accent-amber); }
    .dot-bad { background: var(--accent-coral); }

    /* 页脚 */
    .footer {
      text-align: center;
      color: var(--text-tertiary);
      font-size: 0.72rem;
      padding: 20px;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
    }
    .footer .leaf {
      color: var(--accent-green);
    }

    /* 响应式 */
    @media (max-width: 1200px) {
      .day-column { flex: 0 0 calc(33.333% - 7px); min-width: 280px; }
    }
    @media (max-width: 768px) {
      .stats-row { flex-wrap: wrap; }
      .stat-card { min-width: calc(50% - 6px); flex: none; }
      .calendar-shell-header {
        flex-direction: column;
        align-items: center;
        padding: 16px 16px 6px;
      }
      .calendar-shell-title {
        flex-wrap: wrap;
        justify-content: center;
      }
      .calendar-control { justify-content: center; }
      .day-column { flex: 0 0 calc(50% - 5px); min-width: 260px; }
    }
    @media (max-width: 480px) {
      .day-column { flex: 0 0 85%; min-width: 260px; }
      .stat-card { min-width: 100%; }
    }

    /* 滚动条 */
    .day-body::-webkit-scrollbar { width: 3px; }
    .day-body::-webkit-scrollbar-track { background: transparent; }
    .day-body::-webkit-scrollbar-thumb { background: var(--border-light); border-radius: 3px; }
  </style>
</head>
<body>

  <!-- 品牌栏 -->
  <div class="brand-bar">
    <div class="brand">
      <div class="brand-logo">♥</div>
      <div>
        <div class="brand-title">主人健康看板</div>
        <div class="brand-sub">Daily Wellness · 每日健康追踪</div>
      </div>
    </div>
    <div class="brand-meta">
      <span class="pulse-dot"></span>
      <span>实时同步 · ${todayStr}</span>
    </div>
  </div>

  <!-- 顶部状态卡 -->
  <div class="top-bar">
    <div class="stats-row">
      <div class="stat-card c-sky">
        <div class="label"><span class="ico">⚖</span>当前体重</div>
        <div class="value"><span id="summary-weight">${latest.weight || '--'}</span><span class="unit">kg</span></div>
        <div class="sub ${weightChangeClass}" id="summary-weight-change">${weightChange}</div>
      </div>
      <div class="stat-card c-lavender">
        <div class="label"><span class="ico">◎</span>目标体重</div>
        <div class="value">66.5<span class="unit">kg</span></div>
        <div class="sub neutral" id="summary-target-gap">还需 ${latest.weight ? (latest.weight - 66.5).toFixed(1) : '--'}kg</div>
      </div>
      <div class="stat-card c-lavender">
        <div class="label"><span class="ico">◎</span>体脂率</div>
        <div class="value"><span id="summary-body-fat">${latestBodyFat || '--'}</span><span class="unit">%</span></div>
        <div class="sub neutral">健康范围</div>
      </div>
      <div class="stat-card c-amber">
        <div class="label"><span class="ico">◇</span>BMI</div>
        <div class="value" id="summary-bmi">${latestBmi || '--'}</div>
        <div class="sub ${latestBmi < 24 ? 'neutral' : (latestBmi < 28 ? 'neutral' : 'up')}" id="summary-bmi-status">
          ${latestBmi < 18.5 ? '偏瘦' : (latestBmi < 24 ? '正常' : (latestBmi < 28 ? '超重' : '肥胖'))}
        </div>
      </div>
    </div>
  </div>

  <!-- 日历容器 -->
  <div class="calendar-shell">
    <div class="calendar-shell-header">
      <div class="calendar-shell-title">
        <div class="ttl">每周追踪</div>
        <span class="chip">Weekly</span>
        <div class="sub">饮食 · 运动 · 饮水</div>
      </div>
      <div class="calendar-control">
        <button onclick="changeDay(-1)" title="前一天">◀</button>
        <div class="week-label" id="week-label">加载中...</div>
        <button onclick="changeDay(1)" title="后一天">▶</button>
      </div>
    </div>
    <div class="calendar-wrapper">
      <div class="week-grid" id="week-grid"><!-- JS 填充 --></div>
    </div>
  </div>

  <!-- 底部留白 -->
  <div style="height: 20px;"></div>

  <div class="footer">
    <span class="leaf">❀</span>
    <span>数据更新于 ${updateTime} 上海时间 · 主人在，管家就在</span>
    <span class="leaf">❀</span>
  </div>
  <div class="exercise-plan-popover" id="exercise-plan-popover" aria-hidden="true"></div>

  <script>
    const allData = ${allDataJSON};
    const DAILY_BASE = ${DAILY_BASE};
    const TARGET_DEFICIT = ${TARGET_DEFICIT};
    const DASHBOARD_TIME_ZONE = '${DASHBOARD_TIME_ZONE}';
    const PROFILE_HEIGHT_CM = ${loadProfileHeightCm() || 'null'};
    const TARGET_WEIGHT_KG = 66.5;

    let currentCenterDate = '${todayYMD}';

    function dateFromYMD(dateStr) {
      const parts = dateStr.split('-').map(Number);
      return new Date(Date.UTC(parts[0], parts[1] - 1, parts[2]));
    }
    function getShanghaiToday() {
      const parts = new Intl.DateTimeFormat('en-US', {
        timeZone: DASHBOARD_TIME_ZONE,
        year: 'numeric',
        month: '2-digit',
        day: '2-digit'
      }).formatToParts(new Date());
      const values = Object.fromEntries(parts.map(part => [part.type, part.value]));
      return values.year + '-' + values.month + '-' + values.day;
    }
    function getWeekStart(dateStr) {
      const d = dateFromYMD(dateStr);
      const day = d.getUTCDay();
      const diff = day === 0 ? -6 : 1 - day;
      d.setUTCDate(d.getUTCDate() + diff);
      return d;
    }
    function fmtDate(d) {
      const year = d.getUTCFullYear();
      const month = String(d.getUTCMonth() + 1).padStart(2, '0');
      const day = String(d.getUTCDate()).padStart(2, '0');
      return year + '-' + month + '-' + day;
    }
    function fmtMD(s) { return parseInt(s.slice(5,7)) + '/' + parseInt(s.slice(8,10)); }
    function latestDayWith(field) {
      const dates = Object.keys(allData).sort().reverse();
      for (const date of dates) {
        const day = allData[date];
        if (day && day[field] !== null && day[field] !== undefined && day[field] !== '') return day;
      }
      return null;
    }
    function calculateBmiFromDay(day) {
      if (!day || !day.weight) return null;
      const heightCm = day.heightCm || PROFILE_HEIGHT_CM;
      if (!heightCm) return day.bmi || null;
      const heightM = heightCm / 100;
      return Math.round((day.weight / (heightM * heightM)) * 10) / 10;
    }
    function bmiStatus(bmi) {
      if (!bmi) return {text: '--', className: 'neutral'};
      if (bmi < 18.5) return {text: '偏瘦', className: 'neutral'};
      if (bmi < 24) return {text: '正常', className: 'neutral'};
      if (bmi < 28) return {text: '超重', className: 'neutral'};
      return {text: '肥胖', className: 'up'};
    }
    function setText(id, value) {
      const el = document.getElementById(id);
      if (el) el.textContent = value;
    }
    function setSubClass(id, className) {
      const el = document.getElementById(id);
      if (el) el.className = 'sub ' + className;
    }
    function escapeHtml(value) {
      const chars = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
      return String(value ?? '').replace(/[&<>"']/g, ch => chars[ch]);
    }
    function renderSummaryCards() {
      const latestWeightDay = latestDayWith('weight');
      const latestBodyFatDay = latestDayWith('bodyFat');
      const datesWithWeight = Object.keys(allData).sort().filter(date => {
        const day = allData[date];
        return day && day.weight !== null && day.weight !== undefined;
      });
      const previousWeightDay = datesWithWeight.length > 1 ? allData[datesWithWeight[datesWithWeight.length - 2]] : null;
      const weight = latestWeightDay ? latestWeightDay.weight : null;
      const bmi = calculateBmiFromDay(latestWeightDay);
      const status = bmiStatus(bmi);

      setText('summary-weight', weight !== null ? weight : '--');
      setText('summary-body-fat', latestBodyFatDay ? latestBodyFatDay.bodyFat : '--');
      setText('summary-bmi', bmi !== null ? bmi : '--');
      setText('summary-bmi-status', status.text);
      setSubClass('summary-bmi-status', status.className);

      if (weight !== null) {
        const gap = Math.max(0, weight - TARGET_WEIGHT_KG).toFixed(1);
        setText('summary-target-gap', '还需 ' + gap + 'kg');
      }

      if (latestWeightDay && previousWeightDay && previousWeightDay.weight !== null && previousWeightDay.weight !== undefined) {
        const delta = Math.round((latestWeightDay.weight - previousWeightDay.weight) * 10) / 10;
        const arrow = delta < 0 ? '↓' : (delta > 0 ? '↑' : '→');
        const className = delta < 0 ? 'down' : (delta > 0 ? 'up' : 'neutral');
        setText('summary-weight-change', arrow + ' ' + Math.abs(delta).toFixed(1) + 'kg');
        setSubClass('summary-weight-change', className);
      } else {
        setText('summary-weight-change', '建档首日');
        setSubClass('summary-weight-change', 'neutral');
      }
    }
    function todayExercisePlanHtml() {
      const day = allData[getShanghaiToday()];
      if (!day) {
        return '<div class="exercise-plan-title">今日运动方案</div><div class="exercise-plan-main">今日暂无数据</div>';
      }
      const hasPlan = Boolean(day.exerciseType || day.exerciseLabel || day.exerciseMinutes || day.exerciseBurn);
      const label = day.exerciseLabel || day.exerciseType || '今日暂无运动方案';
      const status = hasPlan ? (day.exerciseDone ? '已完成' : '待完成') : '未安排';
      const minutes = day.exerciseMinutes || 0;
      const burn = day.exerciseBurn || 0;
      const note = hasPlan ? '按当前计划执行，完成后记录运动状态和主观强度。' : '如果今天安排了训练，记录运动类型、时长和完成状态后这里会同步显示。';

      return '<div class="exercise-plan-title">今日运动方案</div>' +
        '<div class="exercise-plan-main">' + escapeHtml(label) + '</div>' +
        '<div class="exercise-plan-grid">' +
          '<div class="exercise-plan-stat"><span>状态</span><strong>' + status + '</strong></div>' +
          '<div class="exercise-plan-stat"><span>时长</span><strong>' + minutes + ' min</strong></div>' +
          '<div class="exercise-plan-stat"><span>消耗</span><strong>' + burn + ' kcal</strong></div>' +
        '</div>' +
        '<div class="exercise-plan-note">' + escapeHtml(note) + '</div>';
    }
    function positionExercisePlan(event) {
      const popover = document.getElementById('exercise-plan-popover');
      if (!popover) return;
      const margin = 12;
      const gap = 14;
      const rect = popover.getBoundingClientRect();
      let left = event.clientX + gap;
      let top = event.clientY + gap;
      if (left + rect.width > window.innerWidth - margin) {
        left = event.clientX - rect.width - gap;
      }
      if (top + rect.height > window.innerHeight - margin) {
        top = event.clientY - rect.height - gap;
      }
      popover.style.left = Math.max(margin, left) + 'px';
      popover.style.top = Math.max(margin, top) + 'px';
    }
    function showExercisePlan(event) {
      const popover = document.getElementById('exercise-plan-popover');
      if (!popover) return;
      popover.innerHTML = todayExercisePlanHtml();
      popover.classList.add('active');
      popover.setAttribute('aria-hidden', 'false');
      positionExercisePlan(event);
    }
    function moveExercisePlan(event) {
      const popover = document.getElementById('exercise-plan-popover');
      if (popover && popover.classList.contains('active')) positionExercisePlan(event);
    }
    function hideExercisePlan() {
      const popover = document.getElementById('exercise-plan-popover');
      if (!popover) return;
      popover.classList.remove('active');
      popover.setAttribute('aria-hidden', 'true');
    }
    function renderCalendar() {
      const grid = document.getElementById('week-grid');
      const label = document.getElementById('week-label');
      
      // 以 currentCenterDate 为中心，前后各3天共7天
      const center = dateFromYMD(currentCenterDate);
      const visibleDays = [];
      for (let i = -3; i <= 3; i++) {
        const d = new Date(center);
        d.setUTCDate(d.getUTCDate() + i);
        visibleDays.push(fmtDate(d));
      }
      label.textContent = currentCenterDate + ' 前后三天';

      const weekdays = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
      const todayStr = getShanghaiToday();

      grid.innerHTML = visibleDays.map((dateStr, idx) => {
        const day = allData[dateStr];
        const isToday = dateStr === todayStr;
        const isCenter = dateStr === currentCenterDate;
        if (!day) {
          return '<div class="day-column empty' + (isCenter ? ' center' : '') + '"><div class="day-header"><div class="weekday">' + weekdays[(dateFromYMD(dateStr).getUTCDay() + 6) % 7] + '</div><div class="date-num">' + parseInt(dateStr.slice(8,10)) + '</div></div><div class="day-body" style="justify-content:center;align-items:center;color:var(--text-tertiary);font-size:0.8rem">暂无数据</div></div>';
        }

        const intake = day.intake || 0;
        const dailyBurn = DAILY_BASE + (day.exerciseBurn || 0);
        const dailyLimit = dailyBurn - TARGET_DEFICIT;
        const intakePct = Math.min(100, Math.round((intake / dailyLimit) * 100));
        const isUnderLimit = intake <= dailyLimit;
        const remaining = Math.round(dailyLimit - intake);
        const overAmount = Math.round(intake - dailyLimit);

        let mealHtml = '';
        const mealNames = {breakfast: '早', lunch: '午', snack: '加', dinner: '晚'};
        for (const [type, meal] of Object.entries(day.meals || {})) {
          if (!meal || !meal.total_calories) continue;
          const items = meal.items ? meal.items.slice(0, 2).map(i => i.name).join('、') : (meal.notes || '').slice(0, 8);
          mealHtml += '<div class="meal-line"><span>' + (mealNames[type] || type) + ' ' + items + '</span><span class="meal-cal">' + meal.total_calories + 'kcal</span></div>';
        }

        let html = '<div class="day-column' + (isToday ? ' today' : '') + (isCenter ? ' center' : '') + '">';
        html += '<div class="day-header"><div class="weekday">' + weekdays[(dateFromYMD(dateStr).getUTCDay() + 6) % 7] + '</div><div class="date-num">' + parseInt(dateStr.slice(8,10)) + '</div>' + (day.weight ? '<div class="weight-tag">⚖️ ' + day.weight + 'kg</div>' : '') + '</div>';
        html += '<div class="day-body">';

        // 餐食明细
        html += '<div class="data-block">';
        html += '<div class="data-block-title"><span style="color:var(--accent-coral)">🍽</span> 餐食明细</div>';
        html += '<div class="meal-items">' + mealHtml + '</div>';
        html += '</div>';

        // 饮水
        html += '<div class="data-block">';
        html += '<div class="data-block-title"><span class="status-dot ' + (day.waterPct >= 100 ? 'dot-ok' : (day.waterPct >= 60 ? 'dot-warn' : 'dot-bad')) + '"></span>💧 饮水 ' + day.water + 'ml / ' + day.waterTarget + 'ml</div>';
        html += '<div class="water-bar"><div class="water-fill" style="width:' + Math.min(day.waterPct, 100) + '%"></div></div>';
        html += '<div class="data-block-sub">' + day.waterPct + '%</div>';
        html += '</div>';

        // 摄入限额
        html += '<div class="data-block">';
        html += '<div class="data-block-title"><span style="color:var(--accent-amber)">◐</span> 热量</div>';
        html += '<div class="data-block-value" style="color:' + (isUnderLimit ? 'var(--accent-green-deep)' : 'var(--accent-coral)') + '">' + intake + ' / ' + Math.round(dailyLimit) + ' kcal</div>';
        const fillColor = isUnderLimit ? 'var(--accent-amber)' : 'var(--accent-coral)';
        html += '<div class="deficit-bar"><div class="deficit-fill" style="width:' + Math.min(intakePct, 100) + '%;background:' + fillColor + '"></div></div>';
        html += '<span class="deficit-badge ' + (isUnderLimit ? 'ok' : 'under') + '">今日还可摄入 ' + remaining + '，今日热量缺口 ' + (remaining + TARGET_DEFICIT) + '</span>';
        html += '</div>';

        // 运动
        html += '<div class="data-block exercise-block" onmouseenter="showExercisePlan(event)" onmousemove="moveExercisePlan(event)" onmouseleave="hideExercisePlan()">';
        html += '<div class="data-block-title"><span style="color:var(--accent-coral)">◆</span> 运动</div>';
        if (day.exerciseType) {
          html += '<span class="exercise-tag ' + (day.exerciseDone ? 'exercise-done' : 'exercise-pending') + '">' + (day.exerciseDone ? '✓' : '⏳') + ' ' + day.exerciseLabel + '</span>';
          html += '<div class="data-block-sub">' + day.exerciseMinutes + ' min · 烧耗 ' + (day.exerciseBurn || 0) + ' kcal' + (day.steps ? ' · ' + day.steps.toLocaleString() + ' 步' : '') + '</div>';
        } else {
          html += '<div class="data-block-sub">无记录</div>';
        }
        html += '</div>';

        html += '</div></div>';
        return html;
      }).join('');
    }

    function changeDay(delta) {
      const d = dateFromYMD(currentCenterDate);
      d.setUTCDate(d.getUTCDate() + delta);
      currentCenterDate = fmtDate(d);
      renderCalendar();
      setTimeout(() => {
        const grid = document.querySelector('.week-grid');
        const centerCard = grid.querySelector('.day-column.center') || grid.children[3];
        if (centerCard && centerCard.scrollIntoView) {
          centerCard.scrollIntoView({ behavior: 'smooth', inline: 'center', block: 'nearest' });
        }
      }, 50);
    }

    renderSummaryCards();
    renderCalendar();
    setTimeout(() => {
      const grid = document.querySelector('.week-grid');
      const centerCard = grid.querySelector('.day-column.center') || grid.children[3];
      if (centerCard && centerCard.scrollIntoView) {
        centerCard.scrollIntoView({ behavior: 'auto', inline: 'center', block: 'nearest' });
      }
      const wrapper = document.querySelector('.calendar-wrapper');
      if (wrapper) {
        wrapper.addEventListener('scroll', () => {
          wrapper.classList.add('has-scrolled');
        }, { once: true });
      }
    }, 100);
  </script>
</body>
</html>`;
}

function main() {
  console.log('🏗️  构建暖色调日历看板...');
  const dashboardDir = path.dirname(OUTPUT_FILE);
  if (!fs.existsSync(dashboardDir)) fs.mkdirSync(dashboardDir, { recursive: true });

  const dataMap = loadData();
  console.log(`📊 加载了 ${Object.keys(dataMap).length} 天的数据`);
  if (Object.keys(dataMap).length === 0) { console.error('❌ 无数据'); process.exit(1); }

  const html = generateHTML(dataMap);
  fs.writeFileSync(OUTPUT_FILE, html, 'utf-8');
  console.log(`✅ 看板已生成: ${OUTPUT_FILE}`);
  console.log(`🌐 file://${OUTPUT_FILE}`);
}

main();
