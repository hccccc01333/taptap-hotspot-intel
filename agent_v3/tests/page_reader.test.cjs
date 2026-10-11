const assert=require('node:assert/strict');
const read=require('../page_reader.cjs');
const text=s=>({nodeType:3,textContent:s});
const element=(tag,children=[],attrs={})=>({nodeType:1,tagName:tag,childNodes:children,
 hidden:attrs.hidden||false,display:attrs.display||(['ARTICLE','P','DIV'].includes(tag)?'block':'inline'),
 visibility:attrs.visibility||'visible',getAttribute:k=>attrs[k]||null});
const article=element('ARTICLE',[
 element('SPAN',[text('YT17')]),element('TIME',[text('9 小时前')]),
 element('P',[text('12月计划上线'),element('STRONG',[text('丽奈')])]),
 element('P',[text('仍需确认获取方式')]),
 element('SCRIPT',[text('PRIVATE_SCRIPT')]),element('NAV',[text('PRIVATE_NAV')]),
 element('SPAN',[text('PRIVATE_HIDDEN')],{display:'none'}),
 element('SPAN',[text('PRIVATE_INVISIBLE')],{visibility:'hidden'}),
 element('DIV',[text('PRIVATE_ROLE_NAV')],{role:'navigation'})]);
global.location={hostname:'www.gcores.com'};
global.getComputedStyle=n=>({display:n.display,visibility:n.visibility});
global.document={title:'fixture',body:article,documentElement:{scrollHeight:1000},
 querySelector:s=>s.startsWith('article')?article:null,querySelectorAll:()=>[]};
const original=article.childNodes.slice();const data=read();
assert.deepEqual(data.body.split(/\n+/),['YT17 9 小时前','12月计划上线 丽奈','仍需确认获取方式']);
assert.equal(data.text_extraction_version,'visible-dom-v3.24');
assert.equal(data.published_at,null);
assert.equal(data.reading_region,'article_or_main');
assert.ok(!data.body.includes('PRIVATE'));assert.ok(!data.body.includes('179'));
assert.deepEqual(article.childNodes,original);
// Site-specific content excludes navigation siblings without altering the DOM.
global.document.body=element('DIV',[element('DIV',[text('PRIVATE_SITE_NAV')]),article]);
global.document.querySelector=s=>s.startsWith('.newsPage_content')?article:null;
assert.equal(read().body,data.body);
assert.equal(read().reading_region,'article_or_main');
global.location.hostname='other.example';
assert.ok(read().body.includes('PRIVATE_SITE_NAV'));
global.location.hostname='www.gcores.com';
article.childNodes=Array.from({length:20001},()=>text('large'));
assert.throws(()=>read(),/dom_text_budget/);
console.log('author/time boundary, hidden content, paragraph order, unchanged DOM and node budget passed');
