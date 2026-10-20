/* #507: 成长任务支持手动领取。
 *
 * 批量运行默认仍自动领奖；取消「自动领取」后只推进进度，任务停在 completed，
 * 由任务表里的「领取」按钮按账号逐个结算，避免所有积分在同一分钟到期。
 *
 *   node tests/_test_manual_claim.js
 */
const {dashboardHtml} = require('./_dashboard_source.js');
const html = dashboardHtml();

let pass = 0, fail = 0;
function check(label, ok, detail) {
  if (ok) { pass++; console.log('  ok   ' + label); }
  else { fail++; console.log('  FAIL ' + label + (detail ? '  ' + detail : '')); }
}

check('the auto-claim checkbox exists and defaults on',
      /id="growthAutoClaim" checked/.test(html));
check('the choice is persisted',
      /localStorage\.setItem\('wb-growth-auto-claim'/.test(html));
check('runGrowthTasks sends the checkbox state',
      /payload\.claim = !autoClaim \|\| autoClaim\.checked;/.test(html));
check('completed tasks render a manual claim button',
      /data-code="' \+ esc\(t\.task_code\) \+ '" onclick="claimGrowthTask\(this\)"/.test(html));
check('claimGrowthTask posts to /tasks/claim',
      /postJSON\('\/tasks\/claim', \{uid, code\}\)/.test(html));

console.log('PASS=%d FAIL=%d', pass, fail);
process.exit(fail ? 1 : 0);
