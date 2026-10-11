/* Serialized by Playwright: read the rendered DOM without modifying the page. */
module.exports = function readPage() {
 const ignored = new Set(['SCRIPT','STYLE','NAV','FOOTER','HEADER','FORM','ASIDE','TEMPLATE','IFRAME']);
 function visibleText(root) {
  const parts=[]; let visited=0;
  function walk(node) {
   if (++visited>20000) throw Error('dom_text_budget');
   if (node.nodeType===3) { parts.push((node.textContent||'').replace(/\s+/g,' ')); return; }
   if (node.nodeType!==1 || ignored.has(node.tagName) || node.hidden || node.getAttribute('aria-hidden')==='true' || node.getAttribute('role')==='navigation') return;
   const style=getComputedStyle(node);
   if (style.display==='none' || ['hidden','collapse'].includes(style.visibility)) return;
   const separator=/^(block|flex|grid|table|list-item|flow-root)/.test(style.display)?'\n':' ';
   parts.push(separator);
   for (const child of node.childNodes) walk(child);
   parts.push(separator);
  }
  walk(root);
  return parts.join('').replace(/[ \t]+/g,' ').replace(/ *\n */g,'\n').replace(/\n{3,}/g,'\n\n').trim();
 }
 const videoRegions=location.hostname.endsWith('bilibili.com')?Array.from(document.querySelectorAll('.video-info-container,.video-desc-container')):[];
 const siteRegion=['gcores.com','www.gcores.com'].includes(location.hostname)?document.querySelector('.newsPage_content,.articlePage_content'):null;
 const region=siteRegion||document.querySelector('article,#artibody,#articleContent,.article-content,.content-detail,main');
 const selected=videoRegions.length?videoRegions:[region||document.body];
 const meta=document.querySelector('meta[property="article:published_time"],meta[name="pubdate"],meta[itemprop="datePublished"]');
 return {title:document.title,body:selected.map(visibleText).join('\n'),
  reading_region:videoRegions.length?'video_title_and_description':region?'article_or_main':'visible_page_with_navigation',
  text_extraction_version:'visible-dom-v3.24',published_at:meta?.content||null,
  links:Array.from(document.querySelectorAll('#b_results .b_algo h2 a,.result h3 a')).slice(0,8).map(a=>({title:a.innerText,url:a.href})),
  height:document.documentElement.scrollHeight};
};
