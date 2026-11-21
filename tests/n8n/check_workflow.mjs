// Run inside the n8n image, on the Compose reports network (see STAGE_5_N8N.md).
import assert from 'node:assert/strict';
import {execFile, spawn} from 'node:child_process';
import {once} from 'node:events';
import {mkdtemp, readFile, writeFile} from 'node:fs/promises';
import http from 'node:http';
import {DatabaseSync} from 'node:sqlite';
import {setTimeout as delay} from 'node:timers/promises';
import {promisify} from 'node:util';

const exec = promisify(execFile);
const home = await mkdtemp('/tmp/eduflow-qa-');
const env = {...process.env, N8N_USER_FOLDER: home, N8N_DIAGNOSTICS_ENABLED: 'false'};
const original = JSON.parse(await readFile('/opt/eduflow/daily_sales_report.json', 'utf8'));
const node = (workflow, name) => workflow.nodes.find(n => n.name === name);
assert.equal(original.settings.timezone, 'Europe/Moscow');
assert.equal(node(original, 'Daily 09:00').parameters.rule.interval[0].expression, '0 9 * * *');
const api = 'http://report-api:8000';
const spec = await (await fetch(api + '/openapi.json')).json();
const params = spec.paths['/api/v1/metrics'].get.parameters.map(p => p.name);
for (const key of ['start_date', 'end_date', 'cutoff_date', 'source', 'manager_id']) assert.ok(params.includes(key));
const period = {start_date:'2026-06-01', end_date:'2026-08-31', cutoff_date:'2026-10-31'};
const total = {paid_amount_rub:'0.00', paying_leads:0, average_paid_check_rub:null, lesson_to_payment_pct:null};
const hits = new Map();
const mock = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const scenario = url.pathname.slice(1);
  hits.set(scenario, (hits.get(scenario) ?? 0) + 1);
  let body = {period, total};
  let status = 200;
  if (scenario === 'timeout') return;
  if (scenario === 'invalidJson') {
    res.writeHead(200, {'content-type':'application/json'});
    return res.end('{');
  }
  if (scenario === 'missing') body = {period, total:{paying_leads:0}};
  if (scenario === 'wrongDate') body = {period:{...period, end_date:'2026-07-31'}, total};
  if (scenario === 'wrongType') body = {period, total:{...total, paying_leads:'0'}};
  if (scenario === 'badRequest') status = 422;
  if (scenario === 'unavailable' || (scenario === 'recover' && hits.get(scenario) === 1)) status = 503;
  res.writeHead(status, {'content-type':'application/json'});
  res.end(JSON.stringify(status === 200 ? body : {detail:'test fixture'}));
});
mock.listen(0, '127.0.0.1');
await once(mock, 'listening');
const mockUrl = 'http://127.0.0.1:' + mock.address().port;
const cases = [
  ['zero', 1], ['recover', 2], ['badRequest', 1, 'HTTP 422'],
  ['unavailable', 3, 'HTTP 503'], ['offline', 3, 'unavailable or timed out'],
  ['timeout', 3, 'unavailable or timed out'], ['invalidJson', 1, 'invalid JSON'],
  ['missing', 1, 'invalid KPI fields'], ['wrongType', 1, 'invalid KPI fields'],
  ['wrongDate', 1, 'unexpected report period'],
];
function variant(name) {
  const workflow = structuredClone(original);
  workflow.id = 'qa' + name;
  workflow.name = 'QA ' + name;
  workflow.versionId = crypto.randomUUID();
  return workflow;
}
const timer = variant('timer');
node(timer, 'Daily 09:00').parameters.rule.interval[0].expression = '*/10 * * * * *';
const filtered = variant('filtered');
const filterSettings = node(filtered, 'Report period').parameters;
filterSettings.jsCode = filterSettings.jsCode.replace('const source = null;', "const source = 'organic_search';");
const fixtures = cases.map(([name]) => {
  const workflow = variant(name);
  const fetchNode = node(workflow, 'Fetch SQL metrics');
  fetchNode.parameters.url = name === 'offline' ? 'http://127.0.0.1:1/' : mockUrl + '/' + name;
  if (name === 'timeout') fetchNode.parameters.options.timeout = 300;
  return workflow;
});
async function cli(...args) {
  return exec('n8n', args, {env, maxBuffer: 8_000_000, timeout: 90000});
}
async function execution(id, expectedError) {
  let output;
  try {
    output = (await cli('execute', '--id=' + id, '--rawOutput')).stdout;
  } catch (error) {
    if (!expectedError || !error.stdout.includes(expectedError)) throw new Error('Unexpected CLI failure for ' + id);
    output = error.stdout;
  }
  const start = output.indexOf('\n{\n');
  const end = output.indexOf('\n}\n', start + 3);
  assert.ok(start >= 0 && end > start, 'Missing execution JSON for ' + id);
  const {data} = JSON.parse(output.slice(start + 1, end + 2));
  const {error, runData} = data.resultData;
  if (expectedError) {
    assert.ok(error?.message.includes(expectedError), id);
    assert.equal(runData['Daily sales report'], undefined);
  } else {
    assert.ok(!error, id);
  }
  return {attempts:runData['Fetch SQL metrics'].length, report:runData['Daily sales report']?.[0].data.main[0][0].json};
}
let server;
try {
  const input = home + '/workflows.json';
  await writeFile(input, JSON.stringify([original, timer, filtered, ...fixtures]));
  await cli('import:workflow', '--input=' + input);
  const first = (await execution(original.id)).report;
  const repeat = (await execution(original.id)).report;
  assert.deepEqual(first, repeat);
  const sqlReport = await (await fetch(api + '/api/v1/metrics?' + new URLSearchParams(period))).json();
  assert.deepEqual(first.total, sqlReport.total);
  assert.deepEqual(first.period, period);
  assert.deepEqual(first.by_source ?? [], sqlReport.by_source);
  assert.deepEqual(first.by_manager ?? [], sqlReport.by_manager);
  assert.deepEqual(first.total, {paid_amount_rub:'8506500.00',paying_leads:105,average_paid_check_rub:'81014.29',lesson_to_payment_pct:'39.33'});
  console.log('PASS manual demo, standard SQL KPIs, breakdowns, identical repeat');
  const sourceReport = (await execution(filtered.id)).report;
  assert.equal(sourceReport.filters.source, 'organic_search');
  assert.equal(sourceReport.total.paid_amount_rub, '2867000.00');
  assert.notEqual(sourceReport.report_id, first.report_id);
  console.log('PASS source filter, organic_search');
  for (const [scenario, expectedAttempts, message] of cases) {
    const result = await execution('qa' + scenario, message);
    assert.equal(result.attempts, expectedAttempts, scenario);
    if (scenario !== 'offline') assert.equal(hits.get(scenario), expectedAttempts, scenario);
    if (!message) {
      assert.deepEqual(result.report.total, total);
      assert.ok(!result.report.by_source && !result.report.by_manager);
    }
    console.log('PASS', scenario, expectedAttempts);
  }

  await cli('publish:workflow', '--id=' + timer.id);
  server = spawn('n8n', ['start'], {env, stdio:'ignore'});
  const db = new DatabaseSync(home + '/.n8n/database.sqlite', {readOnly:true});
  try {
    const latest = db.prepare("SELECT e.status, e.startedAt, d.data FROM execution_entity e LEFT JOIN execution_data d ON d.executionId=e.id WHERE e.workflowId=? AND e.mode='trigger' ORDER BY e.id DESC LIMIT 1");
    let row;
    for (let i=0; i<75; i++) {
      row = latest.get(timer.id);
      if (row?.status === 'success' || row?.status === 'error') break;
      await delay(1000);
    }
    assert.equal(row?.status, 'success', 'published Schedule Trigger');
    // n8n serializes runData as a flatted value table. Inspect the final node.
    const table = JSON.parse(row.data);
    function at(value, ...path) {
      for (const key of path) {
        const ref = value[key];
        value = typeof ref === 'string' ? table[Number(ref)] : ref;
      }
      return value;
    }
    const report = at(table[0], 'resultData', 'runData', 'Daily sales report', 0, 'data', 'main', 0, 0, 'json');
    const trigger = at(table[0], 'resultData', 'runData', 'Daily 09:00', 0, 'data', 'main', 0, 0, 'json', 'timestamp');
    const yesterday = new Intl.DateTimeFormat('sv-SE',{timeZone:'Europe/Moscow'}).format(new Date(Date.parse(trigger)-86400000));
    for (const field of Object.keys(period)) assert.equal(at(report, 'period', field), yesterday);
    const dayQuery = new URLSearchParams({start_date: yesterday, end_date: yesterday, cutoff_date: yesterday});
    const dayResponse = await fetch(api + '/api/v1/metrics?' + dayQuery);
    assert.equal(dayResponse.status, 200);
    const dayReport = await dayResponse.json();
    for (const [key, value] of Object.entries(dayReport.total)) {
      assert.equal(at(report, 'total', key), value);
    }
    console.log('PASS published schedule, previous Moscow day:', yesterday);
  } finally { db.close(); }
} finally {
  if (server) server.kill('SIGTERM');
  mock.closeAllConnections();
  mock.close();
}
