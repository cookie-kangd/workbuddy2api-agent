/* #505: the model catalogue shows a one-way "free account blocked" label.
 *
 * The mark is written by a runtime 11158 and cleared by a later success; the
 * dashboard only renders what /v1/models carries. No mark means no label - it
 * never claims a model is available.
 *
 *   node tests/_test_entitlement_badge.js
 */
const {dashboardHtml} = require('./_dashboard_source.js');
const html = dashboardHtml();

let pass = 0, fail = 0;
function check(label, ok, detail) {
  if (ok) { pass++; console.log('  ok   ' + label); }
  else { fail++; console.log('  FAIL ' + label + (detail ? '  ' + detail : '')); }
}

check('the table reads m.entitlement',
      /const ent = m\.entitlement;/.test(html));
check('only code 11158 is labelled',
      /Number\(ent\.code\) === 11158/.test(html));
check('the label says 免费账号不可用',
      /免费账号不可用<\/span>/.test(html));
check('the tooltip refuses to imply availability',
      /未标记不代表可用/.test(html));
check('the label sits next to the credits badge',
      /credBadge \+= ' <span class="badge warn"/.test(html));

console.log('PASS=%d FAIL=%d', pass, fail);
process.exit(fail ? 1 : 0);
