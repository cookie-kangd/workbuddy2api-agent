/* The Token timeline's throughput metric must count generated tokens.
 *
 * #509: total_tokens is the whole conversation re-sent on every turn, so
 * dividing it by the bucket width reported tens of thousands of tok/s on
 * long sessions. The chart, its peak and the tooltip now use
 * completion_tokens; the Token metric still shows total_tokens.
 *
 * This is a source-level check: the dashboard script is one large inline
 * block, and evaluating only the metric helpers drags in the whole page.
 *
 *   node tests/_test_series_throughput.js
 */
const {dashboardHtml} = require('./_dashboard_source.js');
const html = dashboardHtml();

let pass = 0, fail = 0;
function check(label, ok, detail) {
  if (ok) { pass++; console.log('  ok   ' + label); }
  else { fail++; console.log('  FAIL ' + label + (detail ? '  ' + detail : '')); }
}

check('metricValue divides completion_tokens by the bucket width',
      /return \(b\.completion_tokens \|\| 0\) \/ step;/.test(html));
check('peakThroughput divides completion_tokens by the bucket width',
      /Math\.max\(m, \(b\.completion_tokens\|\|0\) \/ s\.bucket_seconds\)/.test(html));
check('the hover tooltip divides completion_tokens by the bucket width',
      /'\u541e\u5410', \(\(b\.completion_tokens\|\|0\)\/step\)/.test(html));
check('no throughput path still divides total_tokens',
      !/total_tokens\s*\|\|\s*0\)\s*\/\s*(step|s\.bucket_seconds)/.test(html));

console.log('PASS=%d FAIL=%d', pass, fail);
process.exit(fail ? 1 : 0);

