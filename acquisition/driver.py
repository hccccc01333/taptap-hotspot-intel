"""页面驱动：冻结回放驱动（默认）与历史 browse 结果适配器。

本模块负责把「页面样本」转换成 :class:`acquisition.model.PageView`，并按
:class:`acquisition.model.Observation` 返回一次动作的结果。它不读写数据库、
不决定预算或路由，也**不读取 fixture 中的评测真值**（``expected_answer`` /
``expected_region_role``）；策略只能看到页面内容与区域角色。

两种实现：

- :class:`FixtureDriver`：读取 ``acquisition/fixtures/pages/*.json`` 的冻结回放样本，
  代表 Playwright 后端的页面行为（懒加载、视觉文本、阻塞页、固定三屏视口）。
- :class:`LegacyBrowseAdapter`：把 ``agent_v3.browser_tools.browse()`` 返回的
  固定三屏 capture 字典离线转换为 :class:`~acquisition.model.PageView`，
  仅供离线对照测试；尚不支持按需抓取。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from . import model
from .model import (
    DEFAULT_UNIT_WEIGHTS,
    DOM_BODY_REGION,
    EXTRACTIONS,
    Cost,
    Observation,
    PageView,
    Region,
    ResearchGoal,
    VIEWPORT_REGION_PREFIX,
    candidate_text,
    fixed_frame_offsets,
    stable_id,
)

FIXTURE_SCHEMA = 'acquisition-fixture-v0.1'
_VIEWPORT_PREFIX = VIEWPORT_REGION_PREFIX
_VISUAL_METHODS = ('ocr', 'vlm')
_TEXT_METHODS = ('dom', 'skill', 'http')
_INTERACT_COST = Cost(units=DEFAULT_UNIT_WEIGHTS['interact'])
_INSPECT_COST = Cost(units=DEFAULT_UNIT_WEIGHTS['inspect'])
_LOCATE_COST = Cost(units=DEFAULT_UNIT_WEIGHTS['locate'])


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else ''


def _as_int(value: Any, name: str) -> int:
    _require(isinstance(value, int) and not isinstance(value, bool), f'{name} 必须为整数')
    return value


def _as_viewport(value: Any) -> tuple[int, int]:
    _require(isinstance(value, (list, tuple)) and len(value) == 2, 'viewport 必须为两个正整数')
    width, height = (_as_int(item, 'viewport 分量') for item in value)
    _require(width > 0 and height > 0, 'viewport 必须为两个正整数')
    return (width, height)


def _as_bbox(value: Any) -> tuple[int, int, int, int]:
    _require(isinstance(value, (list, tuple)) and len(value) == 4, 'bbox 必须为四元组')
    x, y, width, height = (_as_int(item, 'bbox 分量') for item in value)
    _require(x >= 0 and y >= 0 and width > 0 and height > 0, 'bbox 必须为非负整数且宽高为正')
    return (x, y, width, height)


def _dedupe_join(parts: list[str]) -> str:
    seen: set[str] = set()
    result: list[str] = []
    for part in parts:
        text = part.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return '\n'.join(result)


def _requires_login(region: Mapping[str, Any]) -> bool:
    """区域是否要求登录才能读取；``requires`` 形如 ``login`` / ``click:展开规则``。"""
    requirement = region.get('requires')
    return isinstance(requirement, str) and 'login' in requirement.lower()


class FixtureDriver:
    """冻结回放驱动：用合成页面样本代表 Playwright 后端的页面行为。

    关键语义（见 ``acquisition/fixtures/README.md``）：

    - 懒加载区域在 ``interact`` 触发前 ``text`` / ``visual_text`` 都不可读；
      触发后页面内容变化，``page_version`` 随之变化。
    - ``visual_text`` 只能通过 ``ocr`` / ``vlm`` 提取读到；``text`` 可通过
      ``dom`` / ``skill`` / ``ocr`` 读到。
    - ``blocked_reason`` 非空的页面，任何提取都返回 ``blocked=True`` 且不带文字。
    - ``dom:body`` 是虚拟区域，读整页 DOM 正文；``viewport:<i>`` 按
      :func:`acquisition.model.fixed_frame_offsets` 合成固定三屏。
    - 驱动的任何输出都不包含 fixture 的 ``expected_*`` 评测真值。
    """

    backend = 'playwright'

    def __init__(self, page: Mapping[str, Any]) -> None:
        _require(isinstance(page, Mapping), '页面样本必须是 JSON 对象')
        schema = page.get('schema')
        _require(schema in (None, FIXTURE_SCHEMA), f'未知样本规范: {schema!r}')
        _require(bool(page.get('url')), '页面样本缺少 url')
        blocked_reason = page.get('blocked_reason')
        _require(blocked_reason in (None, 'captcha', 'login_wall', 'access_denied'),
                 f'未知阻塞类型: {blocked_reason!r}')
        self.page_id = str(page.get('page_id') or 'page')
        self.url = str(page['url'])
        self.title = _as_text(page.get('title'))
        self.viewport = _as_viewport(page.get('viewport', [1440, 1000]))
        self.scroll_height = _as_int(page.get('scroll_height', 0), 'scroll_height')
        _require(self.scroll_height >= 0, 'scroll_height 必须为非负整数')
        self.blocked_reason = blocked_reason
        self._raw_regions: list[dict[str, Any]] = [dict(item) for item in page.get('regions') or []]
        self._base_dom_text = _as_text(page.get('dom_text'))
        self._expanded: set[str] = set()
        self._opened = False
        self._closed = False
        self._view: PageView | None = None
        self.call_counts: dict[str, int] = {}
        self.capture_count = 0

    # ------------------------------------------------------------------ 构造
    @classmethod
    def from_file(cls, path) -> 'FixtureDriver':
        """从 UTF-8 JSON 文件读取页面样本。"""
        text = Path(path).read_text(encoding='utf-8-sig')
        payload = json.loads(text)
        _require(isinstance(payload, dict), '页面样本必须是 JSON 对象')
        return cls(payload)

    # ------------------------------------------------------------------ 内部
    def _hit(self, name: str) -> None:
        self.call_counts[name] = self.call_counts.get(name, 0) + 1

    def _ensure_not_closed(self) -> None:
        if self._closed:
            raise RuntimeError('驱动已关闭')

    def _ensure_open(self) -> PageView:
        self._ensure_not_closed()
        if not self._opened or self._view is None:
            raise RuntimeError('驱动尚未 open(goal)，不能执行页面动作')
        return self._view

    def _checked_view(self, view: PageView) -> PageView:
        self._ensure_open()
        if not isinstance(view, PageView):
            raise TypeError('view 必须是 PageView')
        return view

    def _raw_region(self, region_id: str) -> dict[str, Any]:
        for item in self._raw_regions:
            if str(item.get('region_id')) == region_id:
                return item
        raise KeyError(region_id)

    def _is_readable(self, region: Mapping[str, Any]) -> bool:
        """区域当前是否对驱动可读：非阻塞、非登录墙、懒加载已触发。"""
        if bool(region.get('blocked')):
            return False
        if _requires_login(region):
            return False
        if bool(region.get('lazy')) and str(region.get('region_id')) not in self._expanded:
            return False
        return True

    def _dom_text(self) -> str:
        """整页 DOM 正文；交互后包含已展开的懒加载文本（不含 visual_text）。"""
        parts = [self._base_dom_text]
        for raw in self._raw_regions:
            if bool(raw.get('lazy')) and str(raw.get('region_id')) in self._expanded:
                parts.append(_as_text(raw.get('text')))
        return _dedupe_join(parts)

    def _snapshot(self) -> dict[str, Any]:
        """生成页面内容快照；page_version 只随内容变化，不随抓取时间变化。"""
        regions = []
        for index, raw in enumerate(self._raw_regions):
            readable = self._is_readable(raw)
            regions.append({
                'index': index,
                'region_id': str(raw.get('region_id')),
                'role': str(raw.get('role')),
                'bbox': _as_bbox(raw.get('bbox')),
                'text': _as_text(raw.get('text')) if readable else '',
                'visual_text': _as_text(raw.get('visual_text')) if readable else '',
                'scroll_y': _as_int(raw.get('scroll_y', 0), 'scroll_y'),
            })
        return {
            'page_id': self.page_id,
            'url': self.url,
            'title': self.title,
            'viewport': self.viewport,
            'scroll_height': self.scroll_height,
            'blocked_reason': self.blocked_reason,
            'dom_text': self._dom_text(),
            'regions': regions,
        }

    def _project(self) -> PageView:
        page_version = model.derive_page_version(self._snapshot())
        regions: list[Region] = []
        for raw in self._raw_regions:
            readable = self._is_readable(raw)
            regions.append(Region(
                region_id=str(raw.get('region_id')),
                role=str(raw.get('role')),
                bbox=_as_bbox(raw.get('bbox')),
                text=_as_text(raw.get('text')) if readable else '',
                visual_text=_as_text(raw.get('visual_text')) if readable else '',
                lazy=bool(raw.get('lazy')),
                requires=raw.get('requires') if isinstance(raw.get('requires'), str) else None,
                blocked=bool(raw.get('blocked')),
                scroll_y=_as_int(raw.get('scroll_y', 0), 'scroll_y'),
            ))
        return PageView(page_version=page_version, url=self.url, title=self.title,
                        viewport=self.viewport, scroll_height=self.scroll_height,
                        regions=tuple(regions), dom_text=self._dom_text(),
                        blocked_reason=self.blocked_reason)

    def _frame_offset(self, view: PageView, index: int) -> int:
        offsets = fixed_frame_offsets(view.scroll_height, view.viewport[1])
        _require(isinstance(index, int) and not isinstance(index, bool), 'viewport 编号必须为整数')
        _require(0 <= index < len(offsets), f'视口编号超出固定帧范围: {index}')
        return offsets[index]

    def _extract_cost(self, method: str) -> Cost:
        return model.action_cost('extract', method)

    def _capture_id(self, view: PageView, region_id: str, method: str, text: str) -> str:
        """稳定截图标识：同一页面版本、区域、通道、文本必然得到同一 id。"""
        return stable_id('capture_', {
            'page_version': view.page_version,
            'region': region_id,
            'method': method,
            'text': text,
        })

    def _blocked_observation(self, view: PageView, region_id: str, role: str,
                             method: str, bbox) -> Observation:
        """阻塞页面/阻塞区域的提取结果：不返回文字，但成本照记（截图不能当证据）。"""
        cost = self._extract_cost(method)
        capture_id = None
        if method in _VISUAL_METHODS:
            capture_id = self._capture_id(view, region_id, method, '')
            self.capture_count += 1
        if self.blocked_reason:
            note = f'blocked_page:{self.blocked_reason}'
        else:
            raw = next((item for item in self._raw_regions
                        if str(item.get('region_id')) == region_id), {})
            note = 'login_required' if _requires_login(raw) else 'region_blocked'
        return Observation(action='extract', page_version=view.page_version,
                           region_id=region_id, role=role, extraction=method,
                           text='', capture_id=capture_id, bbox=bbox, cost=cost,
                           blocked=True, note=note)

    # ------------------------------------------------------------- PageDriver
    def open(self, goal: ResearchGoal) -> PageView:
        """加载页面并返回初始视图；每次 open 等价于一次全新页面加载。"""
        self._ensure_not_closed()
        if not isinstance(goal, ResearchGoal):
            raise TypeError('goal 必须是 ResearchGoal')
        self._hit('open')
        self._expanded.clear()
        self._opened = True
        self._view = self._project()
        return self._view

    def inspect(self, view: PageView) -> Observation:
        """观察页面候选区域；不产生文字证据。"""
        view = self._checked_view(view)
        self._hit('inspect')
        candidates = tuple(region.region_id for region in view.regions)
        note = f'blocked_page:{view.blocked_reason}' if view.blocked_reason else ''
        return Observation(action='inspect', page_version=view.page_version,
                           cost=_INSPECT_COST, candidates=candidates, note=note)

    def locate(self, view: PageView, goal: ResearchGoal) -> Observation:
        """按目标先验给出候选区域顺序；只更新候选，不返回文字证据。"""
        view = self._checked_view(view)
        _require(isinstance(goal, ResearchGoal), 'goal 必须是 ResearchGoal')
        self._hit('locate')
        ordered: list[str] = []
        seen: set[str] = set()

        def add(region: Region) -> None:
            if region.region_id not in seen:
                seen.add(region.region_id)
                ordered.append(region.region_id)

        # 1) 目标 region_hints 命中的语义角色；2) 可读文本像目标候选；
        # 3) 只有视觉文本；4) 其他区域。
        for region in view.regions:
            if any(region.role in objective.region_hints for objective in goal.objectives):
                add(region)
        for region in view.regions:
            if any(candidate_text(objective, region.candidate_text()) for objective in goal.objectives):
                add(region)
        for region in view.regions:
            if region.visual_text.strip():
                add(region)
        for region in view.regions:
            add(region)
        note = f'候选区域 {len(ordered)} 个'
        return Observation(action='locate', page_version=view.page_version,
                           cost=_LOCATE_COST, candidates=tuple(ordered), note=note)

    def extract(self, view: PageView, region: Region, method: str) -> Observation:
        """按通道提取区域文本。

        ``dom`` 只读 ``region.text``；``ocr`` / ``vlm`` 读 ``text + visual_text``
        并消耗一次截图；懒加载未触发时提取成功但文本为空；阻塞页面任何提取
        都不返回文字。
        """
        view = self._checked_view(view)
        if not isinstance(region, Region):
            raise TypeError('region 必须是 Region')
        _require(method in EXTRACTIONS, f'未知提取通道: {method!r}')
        self._hit('extract')
        self._hit(f'extract:{method}')

        region_id = region.region_id
        role = region.role
        bbox = region.bbox
        is_virtual = region_id == DOM_BODY_REGION or region_id.startswith(_VIEWPORT_PREFIX)

        if region_id == DOM_BODY_REGION:
            role = 'section'
            bbox = (0, 0, view.viewport[0], max(view.viewport[1], view.scroll_height))
        elif region_id.startswith(_VIEWPORT_PREFIX):
            index_text = region_id[len(_VIEWPORT_PREFIX):]
            _require(index_text.isdigit(), f'视口编号不合法: {region_id!r}')
            offset = self._frame_offset(view, int(index_text))
            role = 'section'
            bbox = (0, offset, view.viewport[0], view.viewport[1])

        if self.blocked_reason:
            return self._blocked_observation(view, region_id, role, method, bbox)

        # 真实语义区域的阻塞判定需要看原始样本字段（登录墙 / 区域阻塞）。
        raw: dict[str, Any] = {}
        if not is_virtual:
            raw = self._raw_region(region_id)
            if bool(raw.get('blocked')):
                return self._blocked_observation(view, region_id, role, method, bbox)
            if _requires_login(raw):
                return self._blocked_observation(view, region_id, role, method, bbox)

        cost = self._extract_cost(method)
        note = ''
        if region_id == DOM_BODY_REGION:
            text = view.dom_text.strip()
        elif region_id.startswith(_VIEWPORT_PREFIX):
            offset = bbox[1]
            band_top, band_bottom = offset, offset + view.viewport[1]
            parts: list[str] = []
            for item in view.regions:
                x, y, width, height = item.bbox
                if y >= band_bottom or y + height <= band_top:
                    continue
                if item.text.strip():
                    parts.append(item.text)
                if method in _VISUAL_METHODS and item.visual_text.strip():
                    parts.append(item.visual_text)
            text = _dedupe_join(parts)
        else:
            if region.lazy and region_id not in self._expanded:
                text = ''
                note = 'lazy_not_loaded'
            else:
                pieces = [region.text]
                if method in _VISUAL_METHODS:
                    pieces.append(region.visual_text)
                text = _dedupe_join(pieces)

        capture_id = None
        if method in _VISUAL_METHODS:
            capture_id = self._capture_id(view, region_id, method, text)
            self.capture_count += 1
        return Observation(action='extract', page_version=view.page_version,
                           region_id=region_id, role=role, extraction=method,
                           text=text, capture_id=capture_id, bbox=bbox, cost=cost,
                           blocked=False, note=note)

    def interact(self, view: PageView, region: Region) -> Observation:
        """交互：展开懒加载区域后返回**新 PageView**；非懒加载交互不改变版本。"""
        view = self._checked_view(view)
        if not isinstance(region, Region):
            raise TypeError('region 必须是 Region')
        self._hit('interact')
        region_id = region.region_id
        _require(not (region_id == DOM_BODY_REGION or region_id.startswith(_VIEWPORT_PREFIX)),
                 f'虚拟区域不支持交互: {region_id!r}')
        raw = self._raw_region(region_id)

        if self.blocked_reason:
            note = f'blocked_page:{self.blocked_reason}'
            return Observation(action='interact', page_version=view.page_version,
                               region_id=region_id, role=region.role, cost=_INTERACT_COST,
                               blocked=True, note=note)
        if bool(raw.get('blocked')):
            return Observation(action='interact', page_version=view.page_version,
                               region_id=region_id, role=region.role, cost=_INTERACT_COST,
                               blocked=True, note='region_blocked')
        if _requires_login(raw):
            return Observation(action='interact', page_version=view.page_version,
                               region_id=region_id, role=region.role, cost=_INTERACT_COST,
                               blocked=True, note='login_required')

        if bool(raw.get('lazy')):
            if region_id in self._expanded:
                return Observation(action='interact', page_version=view.page_version,
                                   region_id=region_id, role=region.role, cost=_INTERACT_COST,
                                   note='already_expanded')
            self._expanded.add(region_id)
            new_view = self._project()
            self._view = new_view
            return Observation(action='interact', page_version=new_view.page_version,
                               region_id=region_id, role=region.role, cost=_INTERACT_COST,
                               view=new_view, note='lazy_expanded')
        return Observation(action='interact', page_version=view.page_version,
                           region_id=region_id, role=region.role, cost=_INTERACT_COST,
                           note='no_state_change')

    def close(self) -> None:
        """关闭驱动；关闭后任何页面动作都抛 RuntimeError。"""
        if self._closed:
            return
        self._hit('close')
        self._closed = True
        self._opened = False
        self._view = None


class LegacyBrowseAdapter:
    """把 V3 ``browser_tools.browse()`` 的固定三屏结果离线映射为 PageView。

    输入形如::

        {'status': 'ok', 'resolved_url': 'https://...', 'title': '...',
         'body': '整页 DOM 正文', 'captures': [{'scroll_y': 0, 'width': 1440,
         'height': 1000, 'ocr': {'text': '...'}}, ...],
         'comment_cards': [{'text': '...', 'frame_index': 0}, ...]}

    映射结果：``dom:body``（``dom_text``）+ 每屏 ``viewport:<i>``（截图 OCR 文本
    只作为 ``visual_text``，只有 ``ocr`` / ``vlm`` 通道能读到）+ ``comments`` 区域
    （``comment_cards`` 的离线转录文本）。

    **尚不支持按需抓取：真实动态 Playwright（区域级交互、懒加载触发、按需截图）
    属下一阶段；本适配器只转换一次已经发生的固定三屏 browse 结果，供离线对照
    测试使用。**
    """

    backend = 'playwright'

    def __init__(self, capture: Mapping[str, Any]) -> None:
        _require(isinstance(capture, Mapping), 'browse 结果必须是 JSON 对象')
        self.capture = dict(capture)
        self.status = str(capture.get('status') or 'failed')
        self._blocked = self.status == 'blocked'
        self.call_counts: dict[str, int] = {}
        self.capture_count = len(self.capture.get('captures') or [])
        self._view = self._to_page_view()

    # ------------------------------------------------------------------ 内部
    def _hit(self, name: str) -> None:
        self.call_counts[name] = self.call_counts.get(name, 0) + 1

    @staticmethod
    def _frame_text(frame: Mapping[str, Any]) -> str:
        recognized = frame.get('ocr') if isinstance(frame.get('ocr'), Mapping) else {}
        text = recognized.get('text')
        if isinstance(text, str) and text.strip():
            return text
        transcript = frame.get('comment_transcript')
        if isinstance(transcript, str):
            return transcript
        return ''

    def _to_page_view(self) -> PageView:
        frames = [dict(item) for item in (self.capture.get('captures') or [])][:3]
        regions: list[Region] = []
        scroll_height = 0
        width = 1440
        height = 1000
        for index, frame in enumerate(frames):
            scroll_y = _as_int(frame.get('scroll_y', index * 850), 'scroll_y')
            width = _as_int(frame.get('width', width), 'width')
            height = _as_int(frame.get('height', height), 'height')
            _require(width > 0 and height > 0, '截图宽高必须为正')
            scroll_height = max(scroll_height, scroll_y + height)
            frame_text = self._frame_text(frame)
            # 固定三屏的截图文本是 OCR 转录：只能作为视觉文本，由 ocr/vlm 通道读取。
            regions.append(Region(region_id=f'viewport:{index}', role='section',
                                  bbox=(0, scroll_y, width, height),
                                  text='', visual_text=frame_text,
                                  scroll_y=scroll_y))
        cards = [dict(item) for item in (self.capture.get('comment_cards') or [])]
        card_texts = [str(card.get('text') or '').strip() for card in cards]
        card_text = _dedupe_join(card_texts)
        if card_text:
            anchor = 0
            if cards and isinstance(cards[0].get('frame_index'), int):
                frame_index = min(max(cards[0]['frame_index'], 0), max(len(frames) - 1, 0))
                if frames:
                    anchor = _as_int(frames[frame_index].get('scroll_y', 0), 'scroll_y')
            regions.append(Region(region_id='comments', role='comments',
                                  bbox=(0, anchor, width, height),
                                  text=card_text, scroll_y=anchor))
        body = self.capture.get('body')
        dom_text = body if isinstance(body, str) else ''
        url = str(self.capture.get('resolved_url') or self.capture.get('requested_url') or '')
        title = str(self.capture.get('title') or '')
        snapshot = {'url': url, 'title': title, 'dom_text': dom_text,
                    'frames': [{'scroll_y': region.scroll_y, 'visual': region.visual_text,
                                'region': region.region_id} for region in regions],
                    'blocked': self._blocked}
        return PageView(page_version=model.derive_page_version(snapshot), url=url,
                        title=title, viewport=(width, height),
                        scroll_height=scroll_height or height,
                        regions=tuple(regions), dom_text=dom_text,
                        blocked_reason='access_denied' if self._blocked else None)

    # ------------------------------------------------------------------ 公共
    def to_page_view(self) -> PageView:
        """返回离线转换的 PageView（适配器构造时已生成，重复调用返回同一视图）。"""
        return self._view

    def open(self, goal: ResearchGoal | None = None) -> PageView:
        self._hit('open')
        return self._view

    def inspect(self, view: PageView) -> Observation:
        _require(isinstance(view, PageView), 'view 必须是 PageView')
        self._hit('inspect')
        candidates = tuple(region.region_id for region in view.regions)
        note = 'blocked_page:access_denied' if self._blocked else ''
        return Observation(action='inspect', page_version=view.page_version,
                           cost=_INSPECT_COST, candidates=candidates, note=note)

    def locate(self, view: PageView, goal: ResearchGoal | None = None) -> Observation:
        _require(isinstance(view, PageView), 'view 必须是 PageView')
        self._hit('locate')
        return Observation(action='locate', page_version=view.page_version,
                           cost=_LOCATE_COST,
                           candidates=tuple(region.region_id for region in view.regions),
                           note='离线固定三屏结果，无按需定位')

    def extract(self, view: PageView, region: Region, method: str) -> Observation:
        _require(isinstance(view, PageView), 'view 必须是 PageView')
        _require(isinstance(region, Region), 'region 必须是 Region')
        _require(method in EXTRACTIONS, f'未知提取通道: {method!r}')
        self._hit('extract')
        self._hit(f'extract:{method}')
        cost = model.action_cost('extract', method)
        capture_id = None
        if method in _VISUAL_METHODS:
            self.capture_count += 1
            capture_id = stable_id('capture_', {'page_version': view.page_version,
                                                'region': region.region_id,
                                                'method': method})
        if self._blocked:
            return Observation(action='extract', page_version=view.page_version,
                               region_id=region.region_id, role=region.role,
                               extraction=method, text='', capture_id=capture_id,
                               bbox=region.bbox, cost=cost, blocked=True,
                               note='blocked_page:access_denied')
        if region.region_id == DOM_BODY_REGION:
            text = view.dom_text.strip()
        elif method in _VISUAL_METHODS:
            text = _dedupe_join([region.text, region.visual_text])
        else:
            text = region.text.strip()
        return Observation(action='extract', page_version=view.page_version,
                           region_id=region.region_id, role=region.role,
                           extraction=method, text=text, capture_id=capture_id,
                           bbox=region.bbox, cost=cost, blocked=False)

    def interact(self, view: PageView, region: Region) -> Observation:
        raise NotImplementedError(
            'LegacyBrowseAdapter 尚不支持按需抓取：真实动态 Playwright 属下一阶段')

    def close(self) -> None:
        self._hit('close')

    # -------------------------------------------------------------- 测试辅助
    @classmethod
    def from_file(cls, path) -> 'LegacyBrowseAdapter':
        payload = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        _require(isinstance(payload, dict), 'browse 结果必须是 JSON 对象')
        return cls(payload)
