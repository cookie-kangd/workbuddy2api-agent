/* 剩余用量估算区块（数据看板 → /usage/remaining）。
 *
 * 这个区块把「账号 × 模型」在上游 24 小时窗口里的剩余量画出来：预算来自撞线
 * 账号的样本，冷却中的组合剩余记 0 并给出恢复时刻，没有撞线记录的组合标
 * 「按 24h 估算」。套件钉住三件事：
 *
 *   1. 区块与表头真的在页面里（id 与列名，区块由侧栏导航现场生成，名字不能漂）；
 *   2. renderRemainingUsage() 对每种行都给出正确的数字与徽章：正常行、冷却行
 *      （剩余 0 + 恢复时刻）、估算行、没有预算的行（剩余显示 —）；
 *   3. loadRemainingUsage() 打的是 /usage/remaining，并把载荷交给渲染函数；
 *   4. batch 2：已用只含本窗口（旧段不并入、也不在估算里单独展示）、按模型总
 *      剩余徽章（可用/冷却组数）、未参与标记的预算样本、冷却行的撞线截断悬停。
 *
 * Run with Node: node tests/_test_remaining_usage.js
 */
'use strict';

const assert = require('assert');
const {dashboardHtml, dashboardScript} = require('./_dashboard_source.js');

// One shared fake DOM for every dashboard suite: tests/_dom_stub.js.
const dom = require('./_dom_stub.js');

let checks = 0;
const check = (label, cond, extra) => {
  checks += 1;
  assert.ok(cond, label + (extra ? '  [' + extra + ']' : ''));
};

// ---- 1. 页面里真的有这个区块与表头 -----------------------------------------
const html = dashboardHtml();
check('区块容器在页面里', html.includes('id="remainingUsageSection"'));
check('两个区域的表体都在页面里',
      html.includes('id="remainingTbodyIntl"') && html.includes('id="remainingTbodyCn"'));
check('两个区域的预算条容器都在页面里',
      html.includes('id="remainingBudgetsIntl"') && html.includes('id="remainingBudgetsCn"'));
check('两个区域的标签在页面里',
      html.includes('<b>国际版</b>') && html.includes('<b>国内版</b>'));
for (const th of ['窗口起点', '窗口内已用', '估算剩余', '用量占比']) {
  check('表头「' + th + '」在页面里', html.includes('>' + th + '</th>'));
}
check('说明文字在页面里', html.includes('预算 = 撞线账号在窗口内用量的平均值'));

// ---- 2. 渲染每种行 ---------------------------------------------------------
const requested = [];
dom.installDom({
  fetch: (url) => {
    requested.push(url);
    const payload = {window_seconds: 86400, generated_at: 1, budgets: [], rows: []};
    return Promise.resolve({
      status: 200, ok: true,
      json: () => Promise.resolve(payload),
      text: () => Promise.resolve(JSON.stringify(payload)),
    });
  },
});

const script = dashboardScript();
const api = new Function(script + `
  return { renderRemainingUsage, loadRemainingUsage, toggleRemainingModelFilter,
           remainingSamplesText };`)();

// 渲染按区域拆成两块：断言用「两块拼起来」的视图，与拆分前逐条对应。
const tbodyIntl = document.getElementById('remainingTbodyIntl');
const tbodyCn = document.getElementById('remainingTbodyCn');
const tbody = {get innerHTML(){ return tbodyIntl.innerHTML + tbodyCn.innerHTML; }};
const budgetsIntl = document.getElementById('remainingBudgetsIntl');
const budgetsCn = document.getElementById('remainingBudgetsCn');
const budgetsEl = {get innerHTML(){ return budgetsIntl.innerHTML + budgetsCn.innerHTML; }};
const summaryEl = document.getElementById('remainingUsageSummary');

const row = (over) => Object.assign({
  uid: 'uid-1', nickname: 'meyadi', realm: 'intl', model: 'deepseek-v4.1-flash',
  used: 50000000, budget: 200000000, budget_min: 190000000, budget_max: 200000000,
  samples: 4, remaining: 150000000, used_pct: 25.0,
  window_start: 1, window_iso: '2026-10-09 23:05:35',
  reset_at: null, reset_iso: null, cooling: false, estimated: false,
}, over);

api.renderRemainingUsage({
  window_seconds: 86400,
  budgets: [{realm: 'intl', model: 'deepseek-v4.1-flash', avg: 200000000,
             min: 190000000, max: 200000000, n: 4}],
  rows: [
    row({}),
    row({uid: 'uid-2', nickname: 'm1328640', used: 200000000, remaining: 0,
         used_pct: 100.0, cooling: true, reset_iso: '2026-10-10 15:18:17'}),
    row({uid: 'uid-3', nickname: 'zh990859', used: 80000000, remaining: 120000000,
         used_pct: 40.0, estimated: true}),
    row({uid: 'uid-4', nickname: '183020947', model: 'glm-5.3', budget: null,
         remaining: null, used_pct: null, used: 42, estimated: true}),
  ],
});

const out = tbody.innerHTML;
check('正常行画出昵称与模型', out.includes('meyadi') && out.includes('deepseek-v4.1-flash'), out);
check('正常行剩余 150M', out.includes('150.0M'), out);
check('正常行已用 50M', out.includes('50.0M'), out);
check('正常行占比 25.0%', out.includes('25.0%'), out);
check('正常行窗口起点显示重置时刻', out.includes('10-09 23:05'), out);
check('冷却行剩余记 0', /冷却至[\s\S]{0,40}10-10 15:18/.test(out), out);
check('冷却行的百分比是 100.0%', out.includes('100.0%'), out);
check('估算行带「按 24h 估算」徽章', out.includes('按 24h 估算'), out);
check('估算行有下界说明的悬停提示', out.includes('是剩余量的下界'), out);
check('没有预算的行剩余显示 —', /<td style="text-align:right">—<\/td>/.test(out), out);
check('没有预算的行不画徽章冲突', out.includes('glm-5.3'), out);

// 预算条：区域 · 模型 · 预算 ≈ 均值 (n=样本数)，悬停给出样本区间
check('预算条列出模型与预算', budgetsEl.innerHTML.includes('deepseek-v4.1-flash')
      && budgetsEl.innerHTML.includes('200.0M'), budgetsEl.innerHTML);
check('预算条标注样本数', budgetsEl.innerHTML.includes('(n=4)'), budgetsEl.innerHTML);
check('预算条悬停给出样本区间',
      budgetsEl.innerHTML.includes('190.0M') && budgetsEl.innerHTML.includes('200.0M'),
      budgetsEl.innerHTML);
check('汇总行统计冷却与 ≥80%', summaryEl.textContent.includes('冷却中 1')
      && summaryEl.textContent.includes('4 项'), summaryEl.textContent);

// 空载荷：不是白板，给一句能看懂的空状态
api.renderRemainingUsage({window_seconds: 86400, budgets: [], rows: []});
check('空载荷给出空状态', tbody.innerHTML.includes('暂无数据'), tbody.innerHTML);
check('空载荷时预算条说明来源',
      budgetsEl.innerHTML.includes('暂无预算样本'), budgetsEl.innerHTML);

// 载荷没变就不重画：TTL 内同一份载荷会连着命中几轮轮询，每轮重写三十行
// innerHTML 是白付的解析与布局。用哨兵验证——同一载荷再来一次时 DOM 不动，
// 换了载荷才重画。
const same = {window_seconds: 86400,
              budgets: [{realm: 'intl', model: 'deepseek-v4.1-flash', avg: 200000000,
                         min: 190000000, max: 200000000, n: 4}],
              rows: [row({})]};
api.renderRemainingUsage(same);
// 指纹守卫看的是第一块（国际版）表体：哨兵打在它上面——同一载荷再来一次
// DOM 不动，换了载荷才重画。
tbodyIntl.innerHTML = 'SENTINEL';
api.renderRemainingUsage(same);
check('同一载荷不重画（DOM 保持原样）', tbodyIntl.innerHTML === 'SENTINEL',
      tbodyIntl.innerHTML);
api.renderRemainingUsage({window_seconds: 86400, budgets: same.budgets,
                          rows: [row({used: 60000000, remaining: 140000000,
                                      used_pct: 30.0})]});
check('载荷变了就重画', tbody.innerHTML.includes('140.0M'), tbody.innerHTML);

// ---- 2b. batch 2：分段口径、按模型总剩余、未参与 ---------------------------
check('预算与总剩余共用一个容器（不再有独立的模型总剩余行）',
      !html.includes('id="remainingModels"'));
check('分段说明文字在页面里', html.includes('恢复时刻起的新窗口重新记账'));

api.renderRemainingUsage({
  window_seconds: 86400,
  activity_floor: 1, activity_floor_iso: '2026-10-09 00:00:00',
  budgets: [
    {realm: 'intl', model: 'deepseek-v4.1-flash', avg: 200000000,
     min: 190000000, max: 200000000, n: 4, active: true},
    {realm: 'intl', model: 'glm-5.3', avg: 5000, min: 5000, max: 5000,
     n: 1, active: false},
  ],
  models: [
    {realm: 'intl', model: 'deepseek-v4.1-flash', total_remaining: 380000000,
     budget: 200000000, pairs: 3, available: 2, cooling: 1, unknown: 0, estimated: 1},
  ],
  rows: [
    // 服务端算好的行：used 只含新窗口（1.2M），上一段（50M）作为 segments[0]。
    row({used: 1200000, remaining: 148800000, used_pct: 0.6,
         segments: [
      {start: 1, end: 2, used: 50000000, capped: true, current: false,
       start_iso: '2026-10-09 23:05:35', end_iso: '2026-10-10 14:00:00'},
      {start: 2, end: 3, used: 1200000, capped: false, current: true,
       start_iso: '2026-10-10 14:10:00', end_iso: '2026-10-10 15:00:00'},
    ]}),
  ],
});
const out2 = tbody.innerHTML;
check('已用只显示新窗口（1.2M）：上一段不并进已用，也不在估算里单独展示',
      out2.includes('>1.2M<') && !out2.includes('51.2M') && !out2.includes('+上段'),
      out2);
check('按模型总剩余画出可用组之和',
      budgetsEl.innerHTML.includes('总剩余') && budgetsEl.innerHTML.includes('380.0M'),
      budgetsEl.innerHTML);
check('总剩余标注可用与冷却组数',
      budgetsEl.innerHTML.includes('可用 2') && budgetsEl.innerHTML.includes('冷却 1'),
      budgetsEl.innerHTML);
check('同一模型的预算与总剩余并成一个徽章（模型名只出现一次）',
      budgetsEl.innerHTML.split('data-model="deepseek-v4.1-flash"').length === 2,
      budgetsEl.innerHTML);
check('未参与的预算样本仍列出并标未参与',
      budgetsEl.innerHTML.includes('glm-5.3') && budgetsEl.innerHTML.includes('未参与'),
      budgetsEl.innerHTML);

// 冷却行：已用单元格的悬停提示给出撞线截断时刻（窗口记账在撞线处截止）。
api.renderRemainingUsage({
  window_seconds: 86400, budgets: [], models: [],
  rows: [row({uid: 'uid-9', used: 40000000, remaining: 0, used_pct: 100.0,
              cooling: true, reset_iso: '2026-10-10 16:00:00',
              segments: [{start: 1, end: 2, used: 40000000, capped: true,
                          current: true, start_iso: '2026-10-09 16:00:00',
                          end_iso: '2026-10-10 15:00:00'}]})],
});
check('冷却行的已用标出撞线截断时刻',
      tbody.innerHTML.includes('已用到撞线时刻为止（10-10 15:00）'), tbody.innerHTML);

// ---- 2c. 模型筛选：点徽章只看一个模型，再点一次显示全部 ---------------------
api.renderRemainingUsage({
  window_seconds: 86400, budgets: [], models: [],
  rows: [row({uid: 'uid-1', model: 'deepseek-v4.1-flash'}),
         row({uid: 'uid-2', model: 'glm-5.3', realm: 'cn', nickname: 'acct-2'})],
});
check('筛选前两个模型的行都在',
      tbody.innerHTML.includes('deepseek-v4.1-flash') && tbody.innerHTML.includes('glm-5.3'),
      tbody.innerHTML);
api.toggleRemainingModelFilter('deepseek-v4.1-flash');
check('筛选后只剩选中模型的行',
      tbody.innerHTML.includes('deepseek-v4.1-flash') && !tbody.innerHTML.includes('glm-5.3'),
      tbody.innerHTML);
check('筛选后统计行按可见行算', summaryEl.textContent.includes('1 项'), summaryEl.textContent);
api.toggleRemainingModelFilter('deepseek-v4.1-flash');
check('再点一次显示全部',
      tbody.innerHTML.includes('deepseek-v4.1-flash') && tbody.innerHTML.includes('glm-5.3'),
      tbody.innerHTML);
check('取消筛选后统计恢复', summaryEl.textContent.includes('2 项'), summaryEl.textContent);

// ---- 2d. 撞线样本：逐窗口原始值 + 征集文本 --------------------------------
api.renderRemainingUsage({
  window_seconds: 86400, models: [], rows: [],
  budgets: [{realm: 'intl', model: 'deepseek-v4.1-flash', avg: 200000000,
             min: 199000000, max: 201000000, n: 2, active: true,
             samples_list: [
               {account: 'abcdef123456', at: 1, at_iso: '2026-10-09 10:00:00',
                reset: 2, reset_iso: '2026-10-10 10:00:00', sample: 199000000},
               {account: 'fedcba654321', at: 3, at_iso: '2026-10-10 11:00:00',
                reset: 4, reset_iso: '2026-10-11 11:00:00', sample: 201000000}]}],
});
const sampleBody = document.getElementById('remainingSamplesBody');
check('撞线样本区块列出逐窗口原始值',
      sampleBody.innerHTML.includes('abcdef12') && sampleBody.innerHTML.includes('199'),
      sampleBody.innerHTML);
check('征集文本按区域/模型/窗口值生成',
      api.remainingSamplesText().includes('国际版 / deepseek-v4.1-flash / 199') &&
      api.remainingSamplesText().includes('201'), api.remainingSamplesText());

// ---- 3. loadRemainingUsage() 打的就是这个接口 ------------------------------
requested.length = 0;
api.loadRemainingUsage().then(() => {
  check('loadRemainingUsage 请求 /usage/remaining',
        requested.some(u => String(u).includes('/usage/remaining')), requested.join(','));
  console.log('remaining usage panel assertions passed (' + checks + ' checks)');
}).catch(err => {
  console.error(err && err.stack ? err.stack : err);
  process.exit(1);
});
