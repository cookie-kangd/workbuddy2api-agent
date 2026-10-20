/* #474: 体验版/免费包的 CycleEndTime 只是每月计费边界。
 *
 * 上游的 DeductionEndTime 指向 8 年后时，后端把包标成 no_expiry、days_left
 * 为 null，并把它排除在 earliest_expiring 之外。前端此前不看这个标记，直接
 * 用 CycleEndTime 算倒计时，于是表格显示「剩 12 天」、最近到期卡片却是 21
 * 天，看起来像卡片算错了。现在两处口径一致，已用完的包默认也不再列出。
 *
 *   node tests/_test_credits_expiry_display.js
 */
const {dashboardHtml} = require('./_dashboard_source.js');
const html = dashboardHtml();

let pass = 0, fail = 0;
function check(label, ok, detail) {
  if (ok) { pass++; console.log('  ok   ' + label); }
  else { fail++; console.log('  FAIL ' + label + (detail ? '  ' + detail : '')); }
}

check('prepared packages keep the upstream no_expiry flag',
      /const noExpiry = !!p\.no_expiry;/.test(html));
check('no countdown is derived from CycleEndTime when no_expiry is set',
      /if\(days === null && !noExpiry && endTime/.test(html));
check('no_expiry rows render a 长期有效 badge',
      /else if\(p\.no_expiry\)\{\s*expiryBadge = '<span class="badge mini">长期有效<\/span>';/.test(html));
check('the earliest-expiring fallback skips no_expiry packages',
      /forEach\(p => \{\s*if\(p\.no_expiry\) return;/.test(html));
check('the package table defaults to the 有剩余 filter',
      /let CURRENT_PKG_FILTER = 'active';/.test(html)
      && /class="active" data-filter="active"[^>]*>有剩余</.test(html)
      && /data-filter="all"[^>]*>全部</.test(html));
check('opening the modal resets back to 有剩余',
      /CURRENT_CREDIT_DETAIL_UID = uid;\s*CURRENT_PKG_FILTER = 'active';/.test(html));

console.log('PASS=%d FAIL=%d', pass, fail);
process.exit(fail ? 1 : 0);
