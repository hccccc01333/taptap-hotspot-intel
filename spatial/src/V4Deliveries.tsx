import { useEffect, useState } from 'react';
import { v4Api, type V4DeliveryIndex, type V4DeliverySummary } from './lib/api';
import './v4-deliveries.css';

type Package = any;
type Tab = 'intelligence' | 'material' | 'creative' | 'checks';

const labels: Record<Tab, string> = {
  intelligence: '情报', material: '素材', creative: '增长创意', checks: '质量检查',
};
const reviewLabels: Record<string, string> = {
  draft: '草稿', needs_review: '待人工审核', approved: '已人工通过',
};
const riskLabels: Record<string, string> = {
  independent_fact_check_pending: '独立事实核验待完成',
  human_approval_pending: '人工审核待完成',
};
function reviewLabel(value?: string) { return reviewLabels[value || ''] || value || '草稿'; }
function riskLabel(value: string) { return riskLabels[value] || value; }

function FactList({ facts }: { facts: any[] }) {
  if (!facts?.length) return <p className="v4-muted">没有事实记录。</p>;
  return <ul className="v4-facts">{facts.map((fact, index) => (
    <li key={index}><b>{fact.text}</b><span>引文：{fact.quote}</span></li>
  ))}</ul>;
}

function Creative({ value }: { value: any }) {
  if (!value) return <p className="v4-muted">没有增长创意。</p>;
  return <div className="v4-stack">
    <div className="v4-hero"><small>结构参考 · 不是官方规范</small><h3>{value.title}</h3><p>{value.hook}</p></div>
    <p><b>建议动作</b> {value.proposed_action}</p>
    <pre className="v4-copy">{value.copy_draft}</pre>
    <p><b>使用模式</b> {(value.used_patterns || []).join(' / ') || '—'}</p>
    <p><b>前置条件</b></p>
    <ul>{(value.preconditions || []).map((item: string, index: number) => <li key={index}>{item}</li>)}</ul>
    {value.reference_samples?.length > 0 && <>
      <p><b>参考来源（结构，不复制原文）</b></p>
      <ul className="v4-refs">{value.reference_samples.map((item: any) => (
        <li key={item.ref_id}>{item.title || item.ref_id}
          {item.url && <a href={item.url} target="_blank" rel="noreferrer">打开公开来源 ↗</a>}
          <small>{item.rights_status}</small>
        </li>
      ))}</ul>
    </>}
    <p className="v4-muted">状态：{value.status}</p>
  </div>;
}

export default function V4Deliveries() {
  const [index, setIndex] = useState<V4DeliveryIndex | null>(null);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState<V4DeliverySummary | null>(null);
  const [pkg, setPkg] = useState<Package | null>(null);
  const [loading, setLoading] = useState(false);
  const [tab, setTab] = useState<Tab>('intelligence');

  useEffect(() => {
    let live = true;
    void v4Api.deliveries().then((data) => { if (live) setIndex(data); })
      .catch((reason) => { if (live) setError(reason instanceof Error ? reason.message : '读取 V4 交付失败'); });
    return () => { live = false; };
  }, []);

  async function open(item: V4DeliverySummary) {
    setSelected(item); setPkg(null); setError(''); setLoading(true); setTab('intelligence');
    try { setPkg(await v4Api.delivery(item.event_id)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : '读取交付包失败'); }
    finally { setLoading(false); }
  }

  const checks = pkg?.checks?.checks || {};
  return <section className="v4-deliveries">
    <div className="v4-head">
      <div>
        <p className="v2-eyebrow">V4 DELIVERY / SHARED FRONTEND</p>
        <h2>V4 交付包</h2>
        <p className="v2-muted">三类型成果：情报、素材、增长创意。自动检查不等于人工审核；已通过的审核记录可追溯。</p>
      </div>
      <span className="v4-badge">{index?.deliveries?.length ?? 0} 个交付</span>
    </div>
    {error && <p className="v2-error" role="alert">{error}</p>}
    <div className="v4-grid">
      <div className="v4-list">
        {!index && !error && <p className="v4-muted">正在读取交付索引…</p>}
        {index?.deliveries?.length === 0 && <p className="v4-muted">{index.note || '暂无 V4 交付。'}</p>}
        {index?.deliveries?.map((item) => <button key={item.event_id}
          className={'v4-item' + (selected?.event_id === item.event_id ? ' active' : '')}
          onClick={() => void open(item)}>
          <small>{item.event_id}</small>
          <b>{item.title || '未命名事件'}</b>
          <span className={'v4-review v4-review-' + (item.review_status || item.status)}>
            {reviewLabel(item.review_status || item.status)} · {item.checks_passed ? '自动检查通过' : '自动检查待补'}
          </span>
          {(item.review_risks?.length || 0) > 0 && <small>风险：{item.review_risks!.map(riskLabel).join('、')}</small>}
        </button>)}
      </div>
      <article className="v4-detail">
        {!selected && <p className="v4-muted">选择左侧交付包查看三类型成果。</p>}
        {selected && loading && <p className="v4-muted">正在读取交付包…</p>}
        {pkg && <>
          <header className="v4-detail-head">
            <small>{pkg.event?.url}</small>
            <h3>{pkg.event?.title}</h3>
            <p className="v4-muted">状态：{reviewLabel(pkg.review?.status || pkg.status)} · 来源：{pkg.event?.source || '公开来源'}</p>
            <div className="v4-review-box" role="status">
              <b>审核：{reviewLabel(pkg.review?.status || pkg.status)}</b>
              <span>{pkg.review?.reviewer ? `审核人：${pkg.review.reviewer}` : '尚未人工审核'}</span>
              {pkg.review?.note && <span>{pkg.review.note}</span>}
              {(pkg.review?.risks?.length || 0) > 0 && <span>待处理：{pkg.review.risks.map(riskLabel).join('、')}</span>}
            </div>
          </header>
          <nav className="v4-tabs" aria-label="交付类型">
            {(Object.keys(labels) as Tab[]).map((key) =>
              <button key={key} className={tab === key ? 'active' : ''} onClick={() => setTab(key)}>{labels[key]}</button>)}
          </nav>
          {tab === 'intelligence' && <div className="v4-stack">
            <p className="v4-lede">{pkg.intelligence?.one_line}</p>
            <p>{pkg.intelligence?.summary}</p>
            <p><b>为什么与 TapTap 有关（判断，不是事实）</b> {pkg.intelligence?.why_taptap}</p>
            <FactList facts={pkg.intelligence?.facts || []}/>
            <p><b>未知</b></p>
            <ul>{(pkg.intelligence?.unknowns || []).map((item: string, index: number) => <li key={index}>{item}</li>)}</ul>
          </div>}
          {tab === 'material' && <div className="v4-stack">
            <p className="v4-muted">{pkg.material?.channel}</p>
            <h3>{pkg.material?.title}</h3>
            <pre className="v4-copy">{pkg.material?.draft}</pre>
            <p><b>使用边界</b> {pkg.material?.usage_boundary}</p>
          </div>}
          {tab === 'creative' && <Creative value={pkg.growth_creative}/>}
          {tab === 'checks' && <div className="v4-stack">
            <ul className="v4-checks">{Object.entries(checks).map(([name, passed]) =>
              <li key={name} className={passed ? 'ok' : 'bad'}>{name}：{passed ? '通过' : '待补'}</li>)}</ul>
            <p className="v4-muted">逐字引文支持不等于语义支持；未做独立事实核验。</p>
          </div>}
        </>}
      </article>
    </div>
  </section>;
}
