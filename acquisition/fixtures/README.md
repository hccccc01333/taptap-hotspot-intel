# Acquisition Harness 冻结回放样本（fixture）规范

这些 JSON 是**合成页面样本**，用现有 `agent_v3/browser_worker.cjs` 与 `page_reader.cjs`
的输出结构构造，用于离线、确定性的机制对照。它们**不是真实站点录制**，也不代表
TapTap 页面分布；样本中的活动日期、评论与站点名称均为虚构。评测结论只能写
「该机制在冻结样本上的表现」，不能写成真实网页质量或事实核验能力。

规范版本：`acquisition-fixture-v0.1`（随 `model.SCHEMA_VERSION` 冻结）。

## 目录

- `pages/*.json`：页面样本（本文件定义格式）。
- `skills/<skill_id>/`：skillproof 风格的 Skill 样例（`SKILL.md`、`manifest.json`、
  `verify.json`）。由 `acquisition.skills.SkillRegistry` 读取，详见该模块。

## 页面 JSON 字段

```json
{
  "schema": "acquisition-fixture-v0.1",
  "page_id": "activity_rules_lazy",
  "label": "活动规则在懒加载区域（合成样本）",
  "source_note": "合成样本：按 browser_worker 输出结构构造，不是真实站点录制",
  "url": "https://www.taptap.cn/example/activity-001",
  "title": "活动公告（合成）",
  "viewport": [1440, 1000],
  "scroll_height": 5200,
  "blocked_reason": null,
  "dom_text": "整页 DOM 中当前已加载的可见正文；懒加载/仅图片文字不在此处",
  "regions": [
    {
      "region_id": "rules",
      "role": "activity_rules",
      "bbox": [120, 1500, 1200, 700],
      "text": "DOM 可见文字（懒加载未触发时为空）",
      "visual_text": "只存在于截图里的文字（OCR/VLM 才可读）",
      "lazy": true,
      "requires": "click:展开规则",
      "blocked": false,
      "scroll_y": 1500
    }
  ],
  "objectives": [
    {
      "objective_id": "deadline",
      "question": "确认活动截止日期",
      "answer_kind": "date",
      "region_hints": ["activity_rules"],
      "required": true,
      "expected_answer": "2026年11月20日",
      "expected_region_role": "activity_rules"
    }
  ],
  "skill_ref": "taptap-activity-rules"
}
```

### 约束

1. `role` 使用 `model.REGION_ROLES` 中的值；`bbox` 为 `[x, y, width, height]`，
   非负整数且宽高为正。
2. `lazy=true` 的区域在交互触发前 `text` 与 `visual_text` 都不可读；触发后
   两者可读，并由驱动层生成新的 `page_version`（页面内容版本变化）。
3. `visual_text` 只能通过 `ocr`/`vlm` 提取读到；`text` 可通过 `dom`、`skill`
   或 `ocr` 读到（截图 OCR 能转录已渲染文字）。
4. `blocked_reason` 取值 `null | "captcha" | "login_wall" | "access_denied"`。
   阻塞页面的截图只能用于核查，**不能进入证据**；由 harness 统一拦截。
5. `objectives[].expected_answer` / `expected_region_role` 是评测真值，只允许
   `acquisition.evaluate` 读取；驱动层不得把它们暴露给页面视图或策略。
6. `dom_text` 不包含 `visual_text`，`lazy` 区域触发前也不包含其 `text`。
7. URL 使用 `https://` 与 `example` 路径，不指向真实可访问资源。

## 虚拟区域约定（驱动层合成）

- `dom:body`：整页 DOM 正文，`extraction="dom"`，对应 V3 现状的 body 读取。
- `viewport:<i>`：按 `model.fixed_frame_offsets()` 计算的一屏，`extraction="ocr"`。
  固定三屏偏移与 `browser_worker.cjs` 现有实现一致，避免基线被削弱。

## 必需的对照组样本

| page_id | 预期机制结论（冻结样本内） |
| --- | --- |
| `static_article` | 现状 V3 基线与自适应都能命中；自适应动作/成本更少，且证据可定位到语义区域。 |
| `activity_rules_lazy` | 固定三屏与现状基线都读不到规则；自适应先交互再提取，能命中。 |
| `poster_image_only` | 期限只在海报图中且位于第三屏之后；固定三屏漏读，自适应定点 OCR 命中。 |
| `layout_changed_v1` / `layout_changed_v2` | 结构签名相同、区域位置或承载方式变化：Skill 仍可定位；固定回放漏读。 |
| `login_comments` | 评论区需要登录：自适应记录 `login_required` 缺口并停止，不伪造评论证据。 |
| `captcha_blocked` | 全策略证据泄漏为 0；自适应在 inspect 后立即停止，固定截图为浪费但不得入库。 |

评测脚本按上表生成检查项；实现可调整样本细节，但不得把预期答案写进策略或驱动，
也不得删除会让自适应策略失败的条件。
