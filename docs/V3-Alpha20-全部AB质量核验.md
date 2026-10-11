# Alpha20 全部 A/B 质量核验（Alpha23 补录）

开发者自评（Codex辅助逐项核查；尚未由开发者本人或独立评审复核）

核查日期：2026-10-11。阅读原始输出、初始输入、冻结必要事实及原网页；未调用评分模型。全部20份A/B均列出，C组原失败未改写。

原实验冻结哈希：`7cf4fa67fe0d4caa658d1b8fc590176ee7747daa82785883562079ee8b35989a`。完整逐条数据见[公开核验JSON](evaluations/alpha20-ab-self-review-alpha23-v2.json)。

## 结果与边界

A：33条显式断言，23条有来源支持，4条有误，6条无法确认；必要事实12/20。B：34条显式断言，25条有支持，2条有误，7条无法确认；必要事实12/20。B没有增加必要事实覆盖。

引文逐字及支持检查：A仅2/10份全部通过，8份有问题；B为1份通过、8份有问题、1份无法确认。风险处理A9/10、B10/10符合本次规则；这不是商业增长效果评价。

Alpha20完整系统C没有有效业务情报：两例执行失败，两例入口过滤，六例留观/归档；覆盖0/20。所有C质量状态保留未核实。不能把无创意、合法JSON或留观当作情报交付。

复合断言中包含未证实年份时，整条断言标为无法确认；日期未知不会因主体正确而算合格。v2据此纠正四条计数，v1与原始输出保留。必要事实按原冻结的月份/日、主体和边界判断，年份未知另外列出。

## 全部事件与错误类型

### Steam挂机钓鱼游戏《喵！一起摸鱼吧~》正式定档11月5日

**A**：支持3、错误0、未知1；必要事实2/2。原报道支持11月5日和桌面形态；明确发售年份未直接给出。第3条引文改了断句，第4条产品文档引文为改写。

facts：无法确认；dates：无法确认；citations：有误；unsupported_assertions：成立；risk_refusal：成立。

**B**：支持4、错误0、未知0；必要事实2/2。一行摘要补入2026年，unknowns又说年份未确认；第4条引文把逗号后内容替换成句号。

facts：成立；dates：无法确认；citations：有误；unsupported_assertions：有误；risk_refusal：成立。

核查来源：[来源1](https://www.gcores.com/articles/220685)、[来源2](https://developer.taptap.cn/docs/pc-store/)

### 新手上路就遇见“手法”角色了

**A**：支持0、错误2、未知1；必要事实1/2。把疾风骑士论坛的角色仿形者写成游戏名，并在后续情报/素材重复；当前网页仅显示08/19，历史互动量未复核。

facts：有误；dates：无法确认；citations：有误；unsupported_assertions：有误；risk_refusal：成立。

**B**：支持1、错误0、未知2；必要事实1/2。保留个人体验和有限样本边界；仍漏掉必要事实里的疾风骑士身份。互动字段不能充当逐字引文，年份/历史计数保留未知。

facts：无法确认；dates：无法确认；citations：有误；unsupported_assertions：成立；risk_refusal：成立。

核查来源：[来源1](https://www.taptap.cn/moment/839260315739753644)

### 更流畅：微软宣布全新Xbox PC客户端

**A**：支持4、错误0、未知0；必要事实2/2。计划/测试未误写已发布；全部事实由原报道支持。

facts：成立；dates：成立；citations：成立；unsupported_assertions：成立；risk_refusal：成立。

**B**：支持4、错误0、未知0；必要事实2/2。第4条引文在“流式传”处截断，未完整支持该条结论；事实可从原网页补验，但不能修补原输出。

facts：成立；dates：成立；citations：有误；unsupported_assertions：成立；risk_refusal：成立。

核查来源：[来源1](https://www.gcores.com/articles/220709)

### 养了五年的猫突然开线还能修吗

**A**：支持1、错误0、未知1；必要事实0/2。不能回溯热搜数值；body字段为空不是网页引文。标题先把话题定为宠物护理，后文又承认可能是玩具，存在无依据定性。未解释两种公开说法。

facts：无法确认；dates：无法确认；citations：有误；unsupported_assertions：有误；risk_refusal：成立。

**B**：支持0、错误0、未知2；必要事实0/2。承认主体不明并拒绝创意，但没有识别毛发乌龙/毛绒玩偶两种说法；历史排行无法确认，热度字段不是网页原文。

facts：无法确认；dates：无法确认；citations：有误；unsupported_assertions：成立；risk_refusal：成立。

核查来源：[来源1](https://weibo.com/2/detail/5352340651508246)、[来源2](https://www.sina.cn/news/detail/5352265640576220.html)、[来源3](https://s.weibo.com/weibo?q=%E5%85%BB%E4%BA%86%E4%BA%94%E5%B9%B4%E7%9A%84%E7%8C%AB%E7%AA%81%E7%84%B6%E5%BC%80%E7%BA%BF%E8%BF%98%E8%83%BD%E4%BF%AE%E5%90%97)

### 湖南永州官方回应商务局干部相关网络举报

**A**：支持4、错误0、未知0；必要事实2/2。主体、10月9日通报、调查而非查实、后续处理均有原报道支持；正确拒绝借势。

facts：成立；dates：成立；citations：成立；unsupported_assertions：成立；risk_refusal：成立。

**B**：支持3、错误0、未知0；必要事实2/2。政务判断克制；第3条TapTap定位虽合理，但用业务上下文改写冒充下载页逐字引文。

facts：成立；dates：成立；citations：有误；unsupported_assertions：成立；risk_refusal：成立。

核查来源：[来源1](https://www.chinanews.com.cn/sh/2026/10-09/10709883.shtml)、[来源2](https://www.taptap.cn/download)

### 育碧宣布将结束《彩虹六号手游》运营 距全球上线尚不足一年

**A**：支持2、错误0、未知1；必要事实0/2。遗漏2027年1月15日；把观测字段当原文日期引文；素材中的社区互动引导不符合负面事件不得用于推广的边界。

facts：无法确认；dates：无法确认；citations：有误；unsupported_assertions：有误；risk_refusal：有误。

**B**：支持2、错误1、未知1；必要事实0/2。第2条错误断言原新闻没给停服日期；页面明确给出。published_at:null为输入字段，不是网页引文；通用下载页也不支持单款页面核验。无推广创意或引流文案。

facts：有误；dates：无法确认；citations：有误；unsupported_assertions：有误；risk_refusal：成立。

核查来源：[来源1](https://www.3dmgame.com/news/202610/3955904.html)、[来源2](https://www.taptap.cn/download)

### 没钱买房就露营

**A**：支持2、错误0、未知2；必要事实2/2。月份日期和个体标签可核查；页面未显示年份，旧文没有被说成当日首发。历史互动量无法从当前页回溯；日期引用无日期，metrics不是网页原文。

facts：无法确认；dates：无法确认；citations：有误；unsupported_assertions：成立；risk_refusal：成立。

**B**：支持2、错误0、未知2；必要事实2/2。引文用分号拼接不同原句，互动字段冒充原文。标题和素材声称把想象搬进游戏，超出文字/标签证据；未知列表又承认玩法未核验。

facts：无法确认；dates：无法确认；citations：有误；unsupported_assertions：有误；risk_refusal：成立。

核查来源：[来源1](https://www.taptap.cn/moment/852915573468695978)

### 《潜水员戴夫》全新DLC将与《深海迷航2：异星水域》展开联动

**A**：支持3、错误1、未知0；必要事实2/2。第4条把输入截断误当原报道未披露；网页已写探索海域、料理、伙伴等内容，素材也重复这个错误。

facts：有误；dates：成立；citations：有误；unsupported_assertions：有误；risk_refusal：成立。

**B**：支持4、错误0、未知0；必要事实2/2。第4条明确限定已获取摘要，可接受；第2条引用改写断句，语义基本支持但不是逐字原文。

facts：成立；dates：成立；citations：有误；unsupported_assertions：成立；risk_refusal：成立。

核查来源：[来源1](https://www.gcores.com/articles/220678)

### 第35届飞天奖揭晓 宋佳同一角色集齐三大奖

**A**：支持3、错误0、未知0；必要事实1/2。日期/烟台/三大奖有依据；漏掉必要事实中的作品和角色。引文添加“标题：”，产品引文也为拼写摘要。

facts：成立；dates：成立；citations：有误；unsupported_assertions：成立；risk_refusal：成立。

**B**：支持2、错误0、未知0；必要事实1/2。正文和标题引文支持已答结论；漏答山花烂漫时/张桂梅，不强行做游戏关联。

facts：成立；dates：成立；citations：成立；unsupported_assertions：成立；risk_refusal：成立。

核查来源：[来源1](https://www.chinanews.com.cn/cul/2026/10-09/10709958.shtml)、[来源2](https://www.taptap.cn/download)

### 《GTA6》杰森淋浴演示未见正面全裸 记者澄清网传说法

**A**：支持1、错误1、未知0；必要事实0/2。第2条把输入缺发布日期说成原报道无发布日期；网页有10月10日06:08:54。漏掉演示不等于最终版的关键事实。

facts：有误；dates：有误；citations：有误；unsupported_assertions：有误；risk_refusal：成立。

**B**：支持3、错误1、未知0；必要事实0/2。官方Rockstar同时列PS5和Xbox Series X|S，“仅支持PS5”缺少限定，误扩展PlayStation语境；原引用URL拼写重复，无法访问。维基引文是2025年旧参考标题，不是本次演示证据。

facts：有误；dates：成立；citations：无法确认；unsupported_assertions：有误；risk_refusal：成立。

核查来源：[来源1](https://www.3dmgame.com/news/202610/3955905.html)、[来源2](https://www.rockstargames.com/VI)、[来源3](https://www.playstation.com/en-us/games/grand-theft-autoft-auto-vi/)、[来源4](https://en.wikipedia.org/wiki/Grand_Theft_Auto_VI)

## 复核方法与限制

- 不是人类独立盲评，开发者可能知道分组；67条显式claim之外，对标题/情报/素材作定性检查，未把所有句子扩充成新分母。
- 事实pass仅表示所核查网页支持，不代表已独立证实媒体所述官方行为；原始公告未全部交叉核实。
- 当前页面不能还原实验当时动态热度；页面月/日不足以独立确认年份。
- 必要事实严格以冻结的2项/事件计数，漏答不当作错误断言；复合必要事实需完整覆盖。
- B检索引入了2025年的旧GTA背景参考，不能作为本次热点证据。
- v2纠正四条混合断言的计数：即使主体或月份正确，只要断言中的年份无法独立证实，整个断言标unverified。保留v1文件，不改变原始输出。

引用有字面匹配不代表支持整个结论；“没有在输入中看到”也不能写成“原报道没有公布”。网页HTTP200如果只有验证页面，不认定为读到正文。当前播放量不能反证历史浏览量，不能据此替换原始结果。

本报告是Codex辅助开发核验；独立人工复核尚未完成。未证明完整系统优于基线，也未证明TapTap商业增长。
