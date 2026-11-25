// Run inside the pinned Compose n8n image with the test mounts in STAGE_5_TELEGRAM.md.
import assert from 'node:assert/strict';
import {execFile, spawn} from 'node:child_process';
import {randomBytes, randomUUID} from 'node:crypto';
import {once} from 'node:events';
import {mkdtemp, readFile, unlink, writeFile} from 'node:fs/promises';
import http from 'node:http';
import {DatabaseSync} from 'node:sqlite';
import {setTimeout as delay} from 'node:timers/promises';
import {promisify} from 'node:util';
import {ledger} from '../telegram/delivery.mjs';
import {deliveryServer} from '../telegram/server.mjs';

const home = await mkdtemp('/tmp/eduflow-telegram-qa-');
const token = '12345:' + randomBytes(24).toString('hex');
const chat = '10101';
const credential = {id: 'qaTelegram', name: 'QA Telegram'};
const offlineCredential = {id: 'qaTelegramOffline', name: 'QA offline relay'};
const env = {...process.env, N8N_USER_FOLDER: home};
const exec = promisify(execFile);
async function cli(...args) {
  return exec('n8n', args, {env, maxBuffer: 8_000_000, timeout: 90000});
}
const db = ledger(':memory:');
const messages = [];
let rejectSend = false;
const relay = deliveryServer({db, chat, send: async (receivedToken, method, body) => {
  assert.ok(receivedToken === token, 'expected isolated credential');
  assert.equal(method, 'sendMessage');
  assert.deepEqual(Object.keys(body).sort(), ['chat_id', 'text']);
  assert.equal(body.chat_id, chat);
  messages.push(body.text);
  if (rejectSend) return {status: 401, data: {ok: false, error_code: 401}};
  return {status: 200, data: {ok: true, result: {message_id: 42,
    chat: {id: Number(chat), type: 'private', first_name: 'UNLOGGABLE_TEST_PROFILE'}}}};
}});
const period = {start_date: '2026-06-01', end_date: '2026-08-31', cutoff_date: '2026-10-31'};
const zero = {paid_amount_rub: '0.00', paying_leads: 0, average_paid_check_rub: null, lesson_to_payment_pct: null};
const api = http.createServer((req, res) => {
  const params = new URL(req.url, 'http://localhost');
  const actualPeriod = Object.fromEntries(Object.keys(period).map(k => [k, params.searchParams.get(k)]));
  res.writeHead(200, {'content-type': 'application/json'});
  res.end(JSON.stringify({period: actualPeriod, total: params.pathname === '/missing' ? {} : zero}));
});
async function listen(server) {
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  return 'http://127.0.0.1:' + server.address().port;
}
const document = JSON.parse(await readFile('/opt/eduflow/daily_sales_report.json', 'utf8'));
const node = (flow, name) => flow.nodes.find(n => n.name === name);
const native = node(document, 'Telegram Send Message');
assert.ok(native.disabled && !native.credentials);
assert.equal(document.nodes.filter(n => n.type.includes('scheduleTrigger')).length, 1);
assert.doesNotMatch(JSON.stringify(document), /\b\d{5,}:[\w-]{20,}/);
const [relayUrl, apiUrl] = await Promise.all([listen(relay), listen(api)]);
function variant(name) {
  const flow = structuredClone(document);
  flow.id = 'qaTelegram' + name;
  flow.name = 'QA Telegram ' + name;
  flow.versionId = randomUUID();
  const telegram = node(flow, 'Telegram Send Message');
  telegram.disabled = false;
  telegram.credentials = {telegramApi: name === 'offline' ? offlineCredential : credential};
  const prepare = node(flow, 'Prepare Telegram delivery').parameters;
  prepare.jsCode = prepare.jsCode.replace('sendManual = false', 'sendManual = true')
    .replace('sendScheduled = false', 'sendScheduled = true');
  if (name === 'timer') node(flow, 'Daily 09:00').parameters.rule.interval[0].expression = '*/10 * * * * *';
  if (name === 'zero' || name === 'auth') {
    const params = node(flow, 'Report period').parameters;
    params.jsCode = params.jsCode.replace('2026-10-31', name === 'zero' ? '2026-11-01' : '2026-11-02');
  }
  if (['zero', 'auth', 'missing'].includes(name)) node(flow, 'Fetch SQL metrics').parameters.url = apiUrl + '/' + name;
  return flow;
}
async function run(name, expectedError) {
  let output;
  try { output = (await cli('execute', '--id=qaTelegram' + name, '--rawOutput')).stdout; }
  catch (error) { output = error.stdout ?? ''; }
  assert.ok(!output.includes(token) && !output.includes('UNLOGGABLE_TEST_PROFILE'), 'sensitive execution data');
  const start = output.indexOf('\n{\n');
  const end = output.indexOf('\n}\n', start + 3);
  assert.ok(start >= 0 && end > start, 'missing execution JSON: ' + name);
  const {error, runData} = JSON.parse(output.slice(start + 1, end + 2)).data.resultData;
  if (expectedError) assert.ok(error?.message.startsWith(expectedError), name + ': ' + error?.message);
  else assert.equal(error, undefined);
  return runData['Delivery receipt']?.[0].data.main[0][0].json;
}
let timerProcess;
try {
  const credentialFile = home + '/credential.json';
  const credentials = [[credential, relayUrl], [offlineCredential, 'http://127.0.0.1:1']].map(([identity, baseUrl]) => ({
    ...identity, type: 'telegramApi', data: {accessToken: token, baseUrl},
  }));
  await writeFile(credentialFile, JSON.stringify(credentials), {mode: 0o600});
  try { await cli('import:credentials', '--input=' + credentialFile); }
  finally { await unlink(credentialFile); }
  const input = home + '/workflows.json';
  await writeFile(input, JSON.stringify(['manual','zero','auth','missing','offline','timer'].map(variant)));
  await cli('import:workflow', '--input=' + input);
  const manual = await run('manual');
  assert.equal(manual.status, 'sent', JSON.stringify(manual));
  assert.equal(manual.message_id, 42);
  assert.equal(manual.attempts, 1);
  assert.deepEqual(manual.period, period);
  for (const value of ['EduFlow AI', '8 506 500,00 ₽', '105', '81 014,29 ₽', '39,33 %', '2026-10-31', 'Отчёт сформирован']) {
    assert.ok(messages[0].includes(value), value);
  }
  assert.ok(messages[0].length <= 4096);
  const repeat = await run('manual');
  assert.equal(repeat.status, 'already_sent');
  assert.equal(repeat.attempts, 0);
  assert.equal(messages.length, 1);
  console.log('PASS native Send Message, SQL KPIs, safe receipt, duplicate suppression after CLI restart');
  assert.equal((await run('zero')).status, 'sent');
  assert.match(messages[1], /0,00 ₽/);
  assert.match(messages[1], /нет данных/);
  assert.doesNotMatch(messages[1], /null|NaN/);
  assert.equal(await run('missing', 'Metrics API returned missing or invalid KPI fields'), undefined);
  assert.equal(messages.length, 2);
  rejectSend = true;
  const denied = await run('auth', 'Telegram: authentication');
  assert.equal(denied.attempts, 1);
  assert.equal(denied.status, 'failed');
  rejectSend = false;
  const offline = await run('offline', 'Telegram: delivery_confirmation_missing');
  assert.equal(offline.status, 'unknown');
  assert.equal(offline.attempts, null);
  console.log('PASS zero sales, absent KPI blocks send, safe Telegram error in execution history');
  await cli('publish:workflow', '--id=qaTelegramtimer');
  timerProcess = spawn('n8n', ['start'], {env, stdio: 'ignore'});
  const history = new DatabaseSync(home + '/.n8n/database.sqlite', {readOnly: true});
  try {
    const stored = history.prepare('SELECT data FROM credentials_entity WHERE id=?').get(credential.id);
    assert.ok(stored?.data && !stored.data.includes(token));
    const query = history.prepare("SELECT e.status,d.data FROM execution_entity e JOIN execution_data d ON d.executionId=e.id WHERE e.workflowId='qaTelegramtimer' AND e.mode='trigger' AND e.status IN ('error','success') ORDER BY e.id LIMIT 1");
    let row;
    for (let n = 0; n < 75 && !row; n++) {
      await delay(1000);
      row = query.get();
    }
    assert.equal(row?.status, 'success');
    assert.ok(!row.data.includes(token) && !row.data.includes('UNLOGGABLE_TEST_PROFILE'));
    // n8n stores saved JSON as a table of references.
    const table = JSON.parse(row.data);
    const field = (...path) => path.reduce((obj, key) => {
      const value = obj[key];
      return typeof value === 'string' ? table[Number(value)] : value;
    }, table[0]);
    const receipt = ['resultData', 'runData', 'Delivery receipt', 0, 'data', 'main', 0, 0, 'json'];
    assert.equal(field(...receipt, 'status'), 'sent');
    assert.equal(field(...receipt, 'message_id'), 42);
    assert.equal(messages.length, 4);
    console.log('PASS scheduled native send using persisted credential; one message for the scheduled period');
  } finally { history.close(); }
} finally {
  timerProcess?.kill('SIGTERM');
  relay.close();
  api.close();
  db.close();
}
