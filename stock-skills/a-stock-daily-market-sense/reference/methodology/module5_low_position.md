# 模块 5：低位放量异动

使用 `module5_low_position.json`。

低位规则：

- A 轨：`close_position_120d <= 0.20`。
- B 轨：`drawdown_120_high <= -35%` 且 `close_cv_10d <= 0.03`。

触发规则：

- `amount_ratio_15d >= 3.0`
- `pct_chg >= 7%`
- 触发日在今日或最近 5 个交易日内
- 如果触发日不是今日，今日必须站在 MA5 上方

质量池：

- A：深回撤强启动。
- B：宽低位强动量质量。
- C：仅观察池。

情形解释：

- starter：今日触发，后续延续性尚未验证。
- sustain：触发后成交额维持高位且价格守住，资金仍在换手。
- quiet：触发后成交额收缩且价格守住，可能是放量后的缩量蓄势。
- undetermined：已触发但既非持续换手也非缩量企稳，通常代表量价分歧。

写作要求：

- 先写各情形和质量层级计数。
- 先展示 A/B 高质量池，再展示 starter/sustain/quiet/undetermined。
- 按成交额行为解释，不只看价格。
