/* Exercise the account row renderer shipped in dashboard.html. Run with Node. */
const assert = require('assert');
const {dashboardScript} = require('./_dashboard_source.js');

const script = dashboardScript();
// One shared fake DOM for every dashboard suite: tests/_dom_stub.js.
const dom = require('./_dom_stub.js');
dom.installDom({
  fetch: () => Promise.resolve({ok: true, status: 200, json: () => Promise.resolve({})}),
});

const {accountRow, coolRow, limitRow, fmtCoolAt, disabledRows, renderDisabled} = new Function(script + `
  return {accountRow, coolRow, limitRow, fmtCoolAt, disabledRows, renderDisabled};`)();
const base = overrides => Object.assign({
  uid: 'uid-cn-0001', nickname: 'synthetic', realm: 'cn', enabled: true,
  source: 'oauth', expiresIn: '24 hours', lastError: '', inCooldown: false,
  cooldownFor: null, machineId: '', credits: null, product: 'cli',
}, overrides);
const until = Math.floor(Date.now() / 1000) + 600;
const d = new Date(until * 1000);
const pad = n => String(n).padStart(2, '0');
const fmtAt = epoch => {
  const dd = new Date(epoch * 1000);
  return dd.getFullYear() + '-' + pad(dd.getMonth() + 1) + '-' + pad(dd.getDate())
    + ' ' + pad(dd.getHours()) + ':' + pad(dd.getMinutes());
};
const localTime = fmtAt(until);

// 胶囊现在独占一整行（coolRow），不再挤在账号列里——账号列只有 180px，长模型名
// 必然被省略号切掉，整行才放得下完整文本。count 类断言改用整段渲染。
assert.equal(coolRow(base({})), '');
assert.equal(coolRow(base({modelCooldowns: []})), '');
assert.equal(fmtCoolAt(until), localTime);
assert.equal(fmtCoolAt('bad-value'), '');

const row = accountRow(base({modelCooldowns: [{model: 'glm-5.3', expiresAt: until}]}));
assert(row.includes('glm-5.3 · 恢复时刻 ' + localTime));
// 可见文本要交代时间口径，并且把模型名与恢复时刻写全（不再靠 title 兜底）。
assert(row.includes('时间为本地时间'));
assert(row.includes('>可用</span>')); // The account still serves other models.
assert(!row.includes('冷却 600s'));
const throttled = accountRow(base({lastError: 'HTTP 429 (model throttled)',
  modelCooldowns: [{model: 'glm-5.3', expiresAt: until}]}));
assert(!throttled.includes('HTTP 429 (model throttled)'));
const otherError = accountRow(base({lastError: 'HTTP 401',
  modelCooldowns: [{model: 'glm-5.3', expiresAt: until}]}));
assert(otherError.includes('HTTP 401'));

const multi = accountRow(base({modelCooldowns: [
  {model: 'glm-5.2', expiresAt: until}, {model: 'glm-5.3', expiresAt: until + 60},
]}));
assert(multi.indexOf('glm-5.2') < multi.indexOf('glm-5.3'));
assert.equal((multi.match(/class="cool-pill"/g) || []).length, 2);

// 6004 撞线（cap 持久化，重启加载后仍在）的 pill：撞线时间 + 恢复时间都要画。
// 文案里「撞线」这个词本身就区分了两类：临时限流没有撞线时刻。
const hit = until - 7200;
const capPill = accountRow(base({modelCooldowns: [
  {model: 'hy3', expiresAt: until, cappedAt: hit},
]}));
assert(capPill.includes('hy3 · 撞线 ' + fmtAt(hit) + ' · 恢复 ' + localTime));
assert(capPill.includes('时间为本地时间'));
assert(capPill.includes('>可用</span>'));  // 同账号其他模型照常。
// 撞线时间坏值时回退成普通 pill，不能画成「撞线 」。
const badCap = accountRow(base({modelCooldowns: [
  {model: 'hy3', expiresAt: until, cappedAt: 'bad-value'},
]}));
assert(badCap.includes('hy3 · 恢复时刻 ' + localTime));
assert(!badCap.includes('撞线'));

// 「当前禁用账号与模型」：两类模型级限流都有真实的起始时刻——6004 用上游给的
// 撞线时刻（cappedAt），临时 429 用 hub 记下的限流时刻（startedAt）。旧实现
// 只把 cappedAt 画进「撞线时间」列，临时 429 那行永远是个「—」，把 60s 的短时
// parking 和几小时的 6004 画成同一副样子。
const started = until - 300;   // 这次 429 是何时发生的
window.ACCOUNTS = [base({modelCooldowns: [
  {model: 'hy3', expiresAt: until, cappedAt: hit, startedAt: hit},
  {model: 'glm-5.3', expiresAt: until, startedAt: started},
]})];
window.VIEW_REALM = 'cn';
const disabled = disabledRows();
const capEntry = disabled.find(r => r.scope === 'hy3');
const plainEntry = disabled.find(r => r.scope === 'glm-5.3');
assert.equal(capEntry.cappedAt, fmtAt(hit));
assert.equal(capEntry.until, localTime);
assert.equal(plainEntry.cappedAt, '');
assert.equal(plainEntry.until, localTime);
assert.equal(capEntry.startedAt, fmtAt(hit), '6004 行的起始时刻就是撞线时刻');
assert.equal(plainEntry.startedAt, fmtAt(started), '429 行的起始时刻是限流时刻');

// 两类模型级限流的「原因」不能共用一句：带 cappedAt 的是上游 6004 配额耗尽
// （恢复时间以小时计），不带 cappedAt 的只是一次 429 临时限流（60s~10m 的短
// 窗口）。旧文案把后者也写成「配额耗尽（code 6004）」，会让人以为要等几小时。
assert.ok(/配额耗尽/.test(capEntry.reason) && /6004/.test(capEntry.reason),
  '6004 撞线行如实说配额耗尽：' + capEntry.reason);
assert.ok(!/6004/.test(plainEntry.reason),
  '429 临时限流行不得提 code 6004：' + plainEntry.reason);
assert.ok(/临时限流/.test(plainEntry.reason) && /429/.test(plainEntry.reason),
  '429 临时限流行如实说临时限流：' + plainEntry.reason);

renderDisabled();
const table = dom.byId('disabledList').innerHTML;
assert(table.includes('开始时间'), '禁用总览表头带开始时间列');
assert(!table.includes('撞线时间'), '旧的「撞线时间」列头已被中性措辞取代');
assert(table.includes('hy3') && table.includes(fmtAt(hit)), '6004 行画出撞线时刻');
assert(table.includes(fmtAt(started)), '429 行画出限流时刻（不再是「—」）');
assert(table.includes('该模型配额耗尽（code 6004）'), '6004 行按配额耗尽呈现');
assert(table.includes('该模型被上游临时限流（429）'), '429 行按临时限流呈现');

// 起始时刻缺失时（老数据 / 后端还没回填）该列回落成「—」，不得编造。
window.ACCOUNTS = [base({modelCooldowns: [{model: 'glm-5.3', expiresAt: until}]})];
const noStart = disabledRows().find(r => r.scope === 'glm-5.3');
assert.equal(noStart.startedAt, '');
renderDisabled();
assert(dom.byId('disabledList').innerHTML.includes('—'),
  '没有起始时刻时该列留「—」而不是假值');

const unsafe = accountRow(base({modelCooldowns: [
  {model: '"><img src=x onerror=alert(1)>', expiresAt: until},
  {model: 'invalid', expiresAt: 'bad-value'},
]}));
assert(!unsafe.includes('<img src=x'));
assert(unsafe.includes('&quot;&gt;&lt;img'));
assert(!unsafe.includes('invalid ·'));

console.log('model cooldown dashboard assertions passed');
