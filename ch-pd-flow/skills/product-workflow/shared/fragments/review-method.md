## `/pd-review` 评审方法

从以下 6 个维度评审 PRD，并给出 0-10 分评分：

1. `Goal Completeness`
   - 目标是否明确、可判断、与背景一致
2. `Scope Completeness`
   - 功能范围、非范围、场景边界是否完整
3. `Rule Completeness`
   - 业务规则、判断条件、约束是否充分
4. `State / Exception Completeness`
   - 状态流转、异常、失败、空状态、边界状态是否完整
5. `Handoff Readiness`
   - 是否足以交给技术、设计、测试协作，不依赖口头补充
6. `Acceptance Readiness`
   - 验收标准、指标、事件定义是否可执行

评审规则：

- 每个维度低于 8 分，必须直接补文档，而不是只提出建议。
- review 输出必须包含：
  - 改好的 PRD
  - 一份 `pd-review-report`
- 只有在存在真实 tradeoff、且无法从现有上下文合理裁决时，才允许提问。
- 若可以基于项目已有方向做出合理产品裁决，应先修订文档，再记录剩余 concern。
