import assert from 'node:assert/strict';
import {once} from 'node:events';
import {mkdtempSync, rmdirSync, unlinkSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {test} from 'node:test';
import {deliver, ledger} from '../../workflows/telegram/delivery.mjs';
import {deliveryServer} from '../../workflows/telegram/server.mjs';

const chat = '10101';
const body = {
  chat_id: JSON.stringify({kind: 'eduflow-report', execution_id: '123',
    report_id: JSON.stringify(['2026-06-01','2026-06-01','2026-06-01',null,null])}),
  text: 'EduFlow AI | Отчёт по продажам',
};
const success = {status: 200, data: {ok: true, result: {message_id: 7, chat: {type: 'private', id: Number(chat), first_name: 'PRIVATE'}}}};
const denied = (status, extra = {}) => ({status, data: {ok: false, error_code: status, ...extra}});

for (const [name, replies, status, code, waits] of [
  ['confirmed', [success], 'sent', null, []],
  ['wrong token', [denied(401)], 'failed', 'authentication', []],
  ['wrong chat', [denied(400)], 'failed', 'bad_request_or_chat', []],
  ['blocked', [denied(403)], 'failed', 'bot_blocked', []],
  ['rate limit', [denied(429, {parameters: {retry_after: 4}}), success], 'sent', null, [4000]],
  ['long cooldown', [denied(429, {parameters: {retry_after: 120}})], 'failed', 'rate_limit', []],
  ['rate limit exhausted', Array(3).fill(denied(429)), 'failed', 'rate_limit', [1000, 2000]],
  ['explicit server rejection', [denied(503), success], 'sent', null, [1000]],
  ['connection refused', Array(3).fill({network: 'ECONNREFUSED'}), 'failed', 'network_unavailable', [1000, 2000]],
  ['unconfirmed gateway failure', [{status: 502, data: null}], 'unknown', 'delivery_unknown', []],
  ['ambiguous timeout', [{network: 'unknown'}], 'unknown', 'delivery_unknown', []],
  ['wrong recipient confirmation', [{status: 200, data: {ok: true, result: {chat: {id: 2, type: 'private'}, message_id: 7}}}], 'unknown', 'delivery_unknown', []],
  ['missing message id', [{status: 200, data: {ok: true}}], 'unknown', 'delivery_unknown', []],
]) {
  test(name, async () => {
    const db = ledger(':memory:');
    let calls = 0;
    const delays = [];
    const send = async (_, method, payload) => {
      assert.equal(method, 'sendMessage');
      assert.deepEqual(payload, {chat_id: chat, text: body.text});
      return replies[calls++];
    };
    try {
      const result = await deliver({db, chat, body, send, sleep: ms => delays.push(ms)});
      assert.equal(result.status, status);
      assert.equal(result.message_id, status === 'sent' ? 7 : null);
      assert.equal(result.error_code, code);
      assert.equal(result.attempts, replies.length);
      assert.equal(result.attempt_log.length, calls);
      assert.deepEqual(delays, waits);
      assert.ok(!JSON.stringify(result).includes('PRIVATE'));
      if (status !== 'failed') {
        const repeat = await deliver({db, chat, body, send});
        assert.equal(repeat.attempts, 0);
        assert.equal(repeat.status, status === 'sent' ? 'already_sent' : 'unknown');
        assert.equal(calls, replies.length);
      }
    } finally { db.close(); }
  });
}

test('reservation survives concurrent invocation and reopening the ledger', async t => {
  const dir = mkdtempSync(join(tmpdir(), 'eduflow-ledger-'));
  const path = join(dir, 'ledger.sqlite');
  let db = ledger(path);
  const {promise, resolve} = Promise.withResolvers();
  const first = deliver({db, chat, body, send: () => promise});
  const noSend = () => assert.fail('unexpected duplicate');
  const concurrent = await deliver({db, chat, body, send: noSend});
  assert.equal(concurrent.attempts, 0);
  resolve(success);
  await first;
  db.close();
  db = ledger(path);
  t.after(() => {
    db.close();
    unlinkSync(path);
    rmdirSync(dir);
  });
  const repeat = await deliver({db, chat, body, send: noSend});
  assert.equal(repeat.status, 'already_sent');
  assert.equal(repeat.message_id, 7);
});

test('only configured private chat and bounded text can be sent', async t => {
  const db = ledger(':memory:');
  t.after(() => db.close());
  const send = () => assert.fail('unexpected request');
  for (const invalid of ['', '-1000123', '@example', undefined]) {
    assert.equal((await deliver({db, chat: invalid, body, send})).attempts, 0);
  }
  assert.equal((await deliver({db, chat, body: {...body, text: 'x'.repeat(4097)}, send})).error_code, 'invalid_message');
  await assert.rejects(deliver({db, chat, body: {...body, chat_id: '555'}, send}));
});

test('credential test returns only bot readiness; malformed tokens never leave the relay', async t => {
  const db = ledger(':memory:');
  let calls = 0;
  const send = async (_, method) => {
    calls++;
    assert.equal(method, 'getMe');
    return {status: 200, data: {ok: true, result: {is_bot: true, username: 'fixture'}}};
  };
  const server = deliveryServer({db, chat, send});
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  t.after(async () => {
    await new Promise(resolve => server.close(resolve));
    db.close();
  });
  const base = 'http://127.0.0.1:' + server.address().port;
  const check = await fetch(base + '/bot123:fixture/getMe');
  assert.deepEqual(await check.json(), {ok: true, result: {is_bot: true}});
  assert.equal((await fetch(base + '/botinvalid/getMe')).status, 401);
  const sendResult = await fetch(base + '/botinvalid/sendMessage', {method: 'POST', body: JSON.stringify(body)});
  assert.equal((await sendResult.json()).delivery.error_code, 'authentication');
  assert.equal((await fetch(base + '/bot123:fixture/getUpdates')).status, 404);
  assert.equal(calls, 1);
});
