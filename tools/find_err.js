const {dashboardScript} = require('./tests/_dashboard_source.js');
const s = dashboardScript();
const lines = s.split(String.fromCharCode(10));
let code = '';
for (let i = 0; i < lines.length; i++) {
  code += lines[i] + String.fromCharCode(10);
  try {
    new Function(code);
  } catch(e) {
    if (e.message.includes('Unexpected token') && !e.message.includes('Unexpected end of input')) {
      console.log('Error at script line:', i + 1, e.message);
      console.log('Line content:', lines[i]);
      console.log('Context:
' + lines.slice(Math.max(0, i-5), i+3).join(String.fromCharCode(10)));
      break;
    }
  }
}
