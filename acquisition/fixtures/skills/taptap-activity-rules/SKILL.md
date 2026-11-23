# Skill：TapTap 活动规则取证（`taptap-activity-rules`）

> 本文件是 skillproof 风格 Skill 的说明文档，**不执行**。运行时只读取
> `manifest.json`（门禁与区域先验）与 `verify.json`（流程完成条件），
> `acquisition.skills.SkillRegistry` 不会解析或执行本文档中的任何指令。

## 1. 用途

在 TapTap 活动公告页定位「活动规则」语义区域，优先展开懒加载的折叠规则，
再按内容形态选择 DOM 文本或截图 OCR 读取规则字段（如报名截止日期）。

**边界（不可协商）**：

- 只提供观察顺序（区域先验）与流程完成验证，不提供事实结论。
- `verify.json` 命中的是「流程完成条件」，**不是**事实核验；
  `SkillRegistry.verify()` 不会、也不能把 `Evidence.verified` 置真。
  事实核验必须由独立的 `model.EvidenceVerifier` 完成（本切片只验证边界：不置真）。
- 不绕过登录墙 / 验证码 / 访问拒绝；不读取评测样本中的 `expected_*` 真值。
- 不访问 `allowed_hosts` 之外的站点。

## 2. 适用页面（门禁）

同时满足以下条件才允许产出区域先验（`SkillRegistry.eligible()`）：

1. 页面主机 ∈ `allowed_hosts`（`www.taptap.cn`）；
2. `permissions.site_rules_reviewed = true`（站点规则已人工复核）；
3. 页面结构签名 ∈ `structural_signatures`（只比较区域角色序列，
   布局位置、区域 id、承载方式变化不影响签名）；
4. `permissions.login_required` 与页面登录场景一致：本 Skill 不要求登录，
   因此出现在登录墙页面时不得使用；
5. 页面未被验证码 / 登录墙 / 访问拒绝整体阻塞。

## 3. 结构先验

- 目标角色：`activity_rules`（主目标）、`comments`（辅助定位活动讨论区）。
- `structural_signatures` 由 `model.structural_signature()` 对
  `layout_changed_v1` / `layout_changed_v2` 两个冻结样本的区域角色序列计算
  （两者序列相同，去重后合并为一项）。

## 4. 建议工作流（由策略执行，Skill 本身不执行动作）

1. `inspect` 观察页面，确认主机、结构签名与阻塞状态；
2. 若 `activity_rules` 区域 `lazy = true`，先 `interact` 展开，再读取新 `PageView`；
3. 区域有可读 DOM 文本时用 `extract(dom)`，仅视觉内容（如长图）时才用 `extract(ocr)`；
4. 提取到规则文本后交给 harness 记录证据（`verified` 仍为 `False`），
   由独立 verifier 决定是否核验；
5. 目标区域缺失、页面阻塞或需要登录时如实记录缺口，不伪造证据。

## 5. 完成条件（`verify.json`）

| 检查 | 含义 |
| --- | --- |
| `region_present` | 页面仍存在 `activity_rules` 语义区域 |
| `extraction_non_empty` | 轨迹中至少一条非空提取证据 |
| `text_pattern` | 证据文本命中规则字段模式（`报名截止`） |
| `page_not_blocked` | 页面未被验证码 / 登录墙 / 访问拒绝整体阻塞 |
| `page_version_current` | 证据绑定的 `page_version` 等于当前页面版本（旧版本证据不算完成） |

以上全部通过只说明「按本 Skill 的流程走完了」，不说明任何事实成立。
