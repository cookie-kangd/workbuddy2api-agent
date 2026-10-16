/* 把看板页面脚本装进一个**显式沙箱**，返回沙箱（也就是页面的 window/global）。
 *
 * 看板套件都需要拿页面顶层的函数（accountRow / renderDisabled / testAccounts…）
 * 配一套假 DOM 跑一遍。此前两种写法并存：`new Function(script + ...)` 和
 * `vm.runInThisContext(script)`——前者要拼字符串，后者直接跑在宿主上下文里。
 * 这里把沙箱写明白：上下文里只有 _dom_stub 造的假 DOM 与一点最小运行环境，页面
 * 代码够不到宿主进程；函数以沙箱属性的形式拿回来，断言直接写 `page.xxx(...)`。
 *
 *   const page = loadDashboard({fetch: fakeFetch});
 *   await page.testAccounts(btn);
 *
 * 被测源码是仓库自己的 dashboard.html（_dashboard_source.js 读的），不是外部输入。
 */
'use strict';
const vm = require('node:vm');
const {dashboardScript} = require('./_dashboard_source.js');
const dom = require('./_dom_stub.js');

// 页面运行时会用到的 Web 平台对象：沙箱是干净的上下文，这些要么显式透传，要么
// 页面在启动流程里就 ReferenceError（首个撞上的是 URLSearchParams）。
const WEB_GLOBALS = [
  'URL', 'URLSearchParams', 'TextEncoder', 'TextDecoder', 'AbortController',
  'Headers', 'Request', 'Response', 'FormData', 'Blob', 'File', 'performance',
  'queueMicrotask', 'structuredClone', 'atob', 'btoa', 'crypto', 'reportError',
];

function loadDashboard(options) {
  const opts = options || {};
  dom.installDom(opts);                       // 假 DOM 先落在宿主 global 上
  const sandbox = Object.assign({}, global.window, {
    console: console,
    setTimeout: global.setTimeout, clearTimeout: global.clearTimeout,
    setInterval: global.setInterval, clearInterval: global.clearInterval,
    document: global.document,
    navigator: global.navigator,
  });
  for (const name of WEB_GLOBALS) {
    if (!(name in sandbox) && name in globalThis) sandbox[name] = globalThis[name];
  }
  // 页面两种写法都用：window.VIEW_REALM 与裸的 getElementById(...)，所以
  // window / globalThis 都指回沙箱本身。
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(dashboardScript(), sandbox, {filename: 'dashboard-inline.js'});
  return sandbox;
}

module.exports = {loadDashboard};
