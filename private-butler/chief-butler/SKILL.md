---
name: chief-butler
description: >
  总管家 Skill。负责聚合和协调下层技能的状态、摘要和 dashboard 刷新。
  当前管理 Health Butler 与 Simple Todo。它不替代下层技能的事实记录，
  只负责总览、调度和跨技能聚合。
---

# Chief Butler

## 目标

提供私人管家的总管理层：一个上层 dashboard 入口，一套下层技能 summary 协议，以及跨技能状态聚合。

当前下层技能：

- `health-butler`：身体健康、饮食、饮水、运动、热量预算、计划调整。
- `simple-todo`：本地待办事项。

## 职责边界

Chief Butler 负责：

- 持有总管 dashboard：`dashboard/index.html`
- 读取下层技能的 `data/summary.json`
- 生成总管聚合态：`data/dashboard_state.json`
- 触发下层刷新脚本
- 维护跨技能入口和后续扩展协议

Chief Butler 不负责：

- 直接修改健康事实数据
- 直接修改 todo 事实数据
- 替代下层技能的安全规则和记录规则

## Dashboard

当前总管 dashboard 从 health-butler 上提而来，HTML 样式和页面内容保持不变。

权威路径：

```text
skills/chief-butler/dashboard/index.html
```

`health-butler` 继续提供健康数据，`simple-todo` 继续提供待办数据。后续新增技能必须先提供 summary，再由 Chief Butler 聚合。

## Summary 协议

详见 `references/skill-summary-protocol.md`。

每个下层技能应提供：

```text
skills/<skill-name>/data/summary.json
skills/<skill-name>/scripts/export_summary.py
```

## 脚本

| 脚本 | 职责 |
|------|------|
| `scripts/collect_status.py` | 读取并聚合下层技能 summary，写 `data/dashboard_state.json` |
| `scripts/refresh_dashboard.py` | 刷新健康 dashboard 数据、收集 summary，保持 HTML 样式不变 |

## 常用命令

```bash
python scripts/collect_status.py
python scripts/refresh_dashboard.py
```

## 扩展规则

新增技能接入 Chief Butler 时，先实现：

1. `data/summary.json`
2. `scripts/export_summary.py`
3. 在 `collect_status.py` 注册 child skill
4. 如需可视化，再由 Chief Butler dashboard 读取聚合态展示
