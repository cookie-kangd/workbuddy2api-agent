/* 「未活跃打卡一键打卡」与「今日活跃打卡状态」卡片悬停名单。
 *
 * 这条按钮的意义就是「只补跑今天还没打过卡的国际版账号」：打重了不会多给积分，
 * 只会白烧账号额度。所以两件事必须钉住：
 *
 *   1. 按钮就在「每日活跃打卡 (国际版)」右边，样式与它一致（都是 .primary）；
 *   2. 要打的那批账号 = 卡片上显示未打卡的那批 = 悬停时列出来的那批，三者同源
 *      （都走 activityStatusOf()），且请求体里只带这批 uid。
 *
 * 判断「今天打过卡没有」的口径本身由 activityStatusOf() 决定，这里不重造一份，
 * 只验证按钮/卡片/请求体取的是同一份结果。Run with Node:
 *   node tests/_test_daily_chat_pending.js
 */
'use strict';
const assert = require('assert');
const {dashboardHtml} = require('./_dashboard_source.js');
const {loadDashboard} = require('./_dashboard_sandbox.js');

// installDom() 会把 global.setTimeout 换成 no-op（面板启动时会挂轮询 interval，
// 真定时器会把进程吊住）。要等异步落定的测试得自己先把平台定时器抓在手里。
const PLATFORM_SET_TIMEOUT = setTimeout;

const html = dashboardHtml();

let checks = 0;
function check(label, ok, detail){
  assert.ok(ok, label + (detail === undefined ? '' : '  -> ' + detail));
  checks += 1;
}

/* ---- 1. 结构：按钮位置与样式 ---------------------------------------- */

const chatBtn = html.indexOf('id="btnPageDailyChat"');
const pendingBtn = html.indexOf('id="btnPageDailyChatPending"');
check('国际版福利区应有「未活跃打卡一键打卡」按钮', pendingBtn > 0);
check('新按钮必须在「每日活跃打卡 (国际版)」右边', pendingBtn > chatBtn);
check('两个按钮之间不能隔着别的按钮', pendingBtn > chatBtn && !/<button/.test(html.slice(
  html.indexOf('</button>', chatBtn), html.lastIndexOf('<button', pendingBtn))),
  html.slice(html.indexOf('</button>', chatBtn), html.lastIndexOf('<button', pendingBtn)).slice(0, 120));
check('新按钮的样式与现有按钮一致（primary）',
  /<button class="primary" id="btnPageDailyChatPending"[^>]*onclick="doDailyChatPending\(this\)"/.test(html));
check('状态卡片有可挂 title 的容器 #cardIntlTodayActive',
  /<div class="card accent2" id="cardIntlTodayActive">[\s\S]{0,200}?id="intlTodayActiveState"/.test(html));
check('DICT 里有按钮文案的英文词条',
  /'未活跃打卡一键打卡':\s*'[^']+'/.test(html), 'DICT 里缺少「未活跃打卡一键打卡」的英文词条');

/* ---- 2. 行为：名单与请求体 ------------------------------------------ */

const pad = n => String(n).padStart(2, '0');
const d = new Date();
const TODAY = d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());

let POSTS = [];
let HISTORY = {rows: []};
// 服务端侧的账号池快照。doDailyChatPending 收尾会调 loadAccounts()（不 await），
// 它会把 window.ACCOUNTS 换成服务端返回的那份；这里让假服务端一直回同一份，
// 免得异步回来的空列表把测试自己设的 ACCOUNTS 冲掉。
let SERVER_ACCOUNTS = [];

function jsonResponse(payload){
  return Promise.resolve({
    ok: true, status: 200,
    json: () => Promise.resolve(payload),
    text: () => Promise.resolve(JSON.stringify(payload)),
  });
}

const page = loadDashboard({
  fetch: (url, opts) => {
    const u = String(url);
    if(opts && opts.method === 'POST'){
      POSTS.push({url: u, body: JSON.parse(String(opts.body || '{}'))});
      return jsonResponse({results: [], accounts: SERVER_ACCOUNTS, slots: []});
    }
    if(u.indexOf('/activity/history') === 0) return jsonResponse(HISTORY);
    if(u.indexOf('/accounts') === 0) return jsonResponse({accounts: SERVER_ACCOUNTS, slots: [], data: [], results: []});
    if(u.indexOf('/realm') === 0) return jsonResponse({current: 'intl', accounts: SERVER_ACCOUNTS, slots: []});
    return jsonResponse({rows: [], data: [], results: [], accounts: SERVER_ACCOUNTS, slots: []});
  },
});

// 页面里 doDailyChatPending 收尾会调这些；沙箱里它们本来就是全局函数，显式指一下
// 免得万一换实现时静默 ReferenceError。
page.updateUI = page.updateUI || (() => {});

// 捕获 toast 文案（toast() 第一句就是 console.log('[wb]', msg)）。
const logs = [];
const realLog = console.log;
console.log = (...args) => logs.push(args.join(' '));

const stateEl = document.getElementById('intlTodayActiveState');
const cardEl = document.getElementById('cardIntlTodayActive');

// 让异步的 loadAccounts/loadModels 先落定，避免它们的渲染插在断言中间。
const settle = () => new Promise(r => PLATFORM_SET_TIMEOUT(r, 0));
// 就地改同一个数组：loadAccounts 链上可能有已经拿到旧引用的回调在稍后写回
// window.ACCOUNTS，换新数组会被它按旧内容盖掉，改内容就不会。
const setAccounts = list => {
  SERVER_ACCOUNTS.length = 0;
  list.forEach(a => SERVER_ACCOUNTS.push(a));
  page.ACCOUNTS = SERVER_ACCOUNTS;
};

// a1 今天已打过（lastDailyChat 是今天）；a2 今天有成功流水；a3/a4 没打过；
// a5 停用；a6 是国内版账号。只有 a3、a4 该被补跑。
setAccounts([
  {uid: 'a1', nickname: '已打卡一', realm: 'intl', enabled: true, lastDailyChat: TODAY + ' 08:00:00'},
  {uid: 'a2', nickname: '流水成功', realm: 'intl', enabled: true},
  {uid: 'a3', nickname: '没打过一', realm: 'intl', enabled: true},
  {uid: 'a4', nickname: '上次失败', realm: 'intl', enabled: true},
  {uid: 'a5', nickname: '已停用', realm: 'intl', enabled: false},
  {uid: 'a6', nickname: '国内账号', realm: 'cn', enabled: true},
]);
HISTORY = {rows: [
  {uid: 'a2', task: 'daily_chat', ok: true, ts: TODAY + 'T07:00:00', message: 'ok'},
  {uid: 'a4', task: 'daily_chat', ok: false, ts: TODAY + 'T07:00:00', message: '网页通道失败'},
]};

(async () => {
  await page.loadActivityToday();
  page.updateTaskPageCheckinCards();

  const pending = page.pendingDailyChatAccounts().map(a => a.uid);
  check('待打卡名单只含今天没打过的国际版启用账号',
    JSON.stringify(pending) === JSON.stringify(['a3', 'a4']), JSON.stringify(pending));

  check('卡片显示「部分已打卡 (2/4)」', stateEl.innerHTML.includes('部分已打卡 (2/4)'),
    stateEl.innerHTML);
  check('卡片 title 列出未打卡账号', /没打过一/.test(stateEl.title) && /上次失败/.test(stateEl.title),
    JSON.stringify(stateEl.title));
  check('title 不含已打卡的账号', !/已打卡一/.test(stateEl.title) && !/流水成功/.test(stateEl.title),
    JSON.stringify(stateEl.title));
  check('title 带未打卡数量', stateEl.title.includes('(2)'), JSON.stringify(stateEl.title));
  check('整个卡片容器也挂上同一份 title（鼠标落在卡片任何位置都能看到）',
    cardEl.title === stateEl.title, JSON.stringify(cardEl.title));

  // 点按钮：请求体里只带待打卡的那批 uid。
  POSTS = [];
  await page.doDailyChatPending({disabled: false, textContent: '未活跃打卡一键打卡'});
  await settle();
  const chatPost = POSTS.find(p => p.url === '/accounts/daily-chat');
  check('点了按钮会 POST /accounts/daily-chat', !!chatPost, JSON.stringify(POSTS));
  check('请求体只带未打卡账号的 uid',
    chatPost && JSON.stringify(chatPost.body.uids) === JSON.stringify(['a3', 'a4']),
    chatPost && JSON.stringify(chatPost.body));

  // 全部打完：不再发请求，只给一句提示。今天的最新流水会盖过 lastDailyChat
  // （activityStatusOf 先看流水），所以两样都置成「今天已完成」。
  HISTORY = {rows: [
    {uid: 'a2', task: 'daily_chat', ok: true, ts: TODAY + 'T09:00:00', message: 'ok'},
    {uid: 'a4', task: 'daily_chat', ok: true, ts: TODAY + 'T09:00:00', message: 'ok'},
  ]};
  await page.loadActivityToday();
  setAccounts(SERVER_ACCOUNTS.map(a => (a.realm === 'intl' && a.enabled
    ? Object.assign({}, a, {lastDailyChat: TODAY + ' 09:00:00'}) : a)));
  page.updateTaskPageCheckinCards();
  check('全部打卡后待打卡名单为空', page.pendingDailyChatAccounts().length === 0);
  check('全部打卡后卡片显示「今日已打卡 (4/4)」',
    stateEl.innerHTML.includes('今日已打卡 (4/4)'), stateEl.innerHTML);
  check('全部打卡后 title 说明都已打卡', stateEl.title.includes('都已打卡'), stateEl.title);

  POSTS = [];
  logs.length = 0;
  await page.doDailyChatPending({disabled: false, textContent: '未活跃打卡一键打卡'});
  await settle();
  check('全部打卡时不再发打卡请求',
    !POSTS.some(p => p.url === '/accounts/daily-chat'), JSON.stringify(POSTS));
  check('全部打卡时给出提示', logs.some(l => l.includes('都已打卡')), JSON.stringify(logs));

  // 没有国际版账号：卡片说明、按钮点了也不发请求。
  setAccounts([{uid: 'c1', nickname: '国内', realm: 'cn', enabled: true}]);
  page.updateTaskPageCheckinCards();
  check('无国际账号时卡片显示「无国际账号」', stateEl.textContent === '无国际账号', stateEl.textContent);
  check('无国际账号时 title 清空', stateEl.title === '' && cardEl.title === '', JSON.stringify(stateEl.title));
  logs.length = 0;
  await page.doDailyChatPending({disabled: false, textContent: '未活跃打卡一键打卡'});
  await settle();
  check('无国际账号时也不发打卡请求',
    !POSTS.some(p => p.url === '/accounts/daily-chat'), JSON.stringify(POSTS));

  console.log = realLog;
  console.log('daily chat pending assertions passed (' + checks + ' checks)');
})().catch(e => { console.log = realLog; console.error(e); process.exit(1); });
