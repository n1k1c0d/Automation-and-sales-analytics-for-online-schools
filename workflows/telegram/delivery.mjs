import { createHash } from 'node:crypto';
import { DatabaseSync } from 'node:sqlite';
import { setTimeout as delay } from 'node:timers/promises';

const maxAttempts = 3;
const datePattern = /^\d{4}-\d{2}-\d{2}$/;
export const validChat = value => /^[1-9]\d{0,15}$/.test(value ?? '') && Number.isSafeInteger(Number(value));

export function ledger(path) {
  const db = new DatabaseSync(path);
  db.exec(`PRAGMA busy_timeout=3000;
    CREATE TABLE IF NOT EXISTS deliveries (
      key TEXT PRIMARY KEY, status TEXT NOT NULL, message_id INTEGER
    )`);
  return db;
}

// Request URLs contain the credential. Only return status, data or a network code.
export async function callTelegram(token, method, body) {
  try {
    const response = await fetch('https://api.telegram.org/bot' + token + '/' + method, {
      method: 'POST', redirect: 'error', signal: AbortSignal.timeout(6000),
      headers: {'content-type': 'application/json'}, body: JSON.stringify(body),
    });
    const data = await response.json().catch(() => null);
    return {status: response.status, data};
  } catch (error) {
    return {network: error.cause?.code ?? 'unknown'};
  }
}

function outcome(reply, chat) {
  if (reply.network) {
    // Only failures positively known to precede transmission can be retried.
    const beforeSend = ['ECONNREFUSED', 'ENOTFOUND', 'EAI_AGAIN', 'ENETUNREACH', 'EHOSTUNREACH'].includes(reply.network);
    return beforeSend ? {code: 'network_unavailable', retry: true} : {code: 'delivery_unknown', uncertain: true};
  }
  const {status, data} = reply;
  const message = data?.result;
  if (status === 200 && data?.ok === true && Number.isSafeInteger(message?.message_id) &&
      message.message_id > 0 && String(message.chat?.id) === chat && message.chat?.type === 'private') {
    return {message_id: message.message_id};
  }
  if (status === 401 || status === 404) return {code: 'authentication'};
  if (status === 403) return {code: 'bot_blocked'};
  if (status === 400) return {code: 'bad_request_or_chat'};
  if (status === 429) {
    const wait = data?.parameters?.retry_after;
    if (wait !== undefined && (!Number.isSafeInteger(wait) || wait < 0 || wait > 15)) {
      return {code: 'rate_limit'};
    }
    return {code: 'rate_limit', retry: true, wait};
  }
  // A gateway 502 or truncated success is not proof of non-delivery.
  if (status >= 500 && status <= 599 && data?.ok === false && data.error_code === status) {
    return {code: 'telegram_unavailable', retry: true};
  }
  return {code: 'delivery_unknown', uncertain: true};
}

export async function deliver({db, chat, token, body, send = callTelegram, sleep = delay}) {
  const target = JSON.parse(body.chat_id);
  const {execution_id, report_id, kind} = target;
  const fields = JSON.parse(report_id);
  if (kind !== 'eduflow-report' || !/^\d+$/.test(execution_id) || !Array.isArray(fields) ||
      fields.length !== 5 || !fields.slice(0, 3).every(v => typeof v === 'string' && datePattern.test(v))) {
    throw new Error('invalid_report');
  }
  const audit = {
    started_at: new Date().toISOString(), execution_id,
    period: {start_date: fields[0], end_date: fields[1], cutoff_date: fields[2]},
    attempts: 0, attempt_log: [], status: 'failed', error_code: null, message_id: null,
  };
  if (!validChat(chat)) return {...audit, error_code: 'recipient_not_configured'};
  if (typeof body.text !== 'string' || !body.text.trim() || body.text.length > 4096) {
    return {...audit, error_code: 'invalid_message'};
  }
  const key = createHash('sha256').update(JSON.stringify([chat, fields])).digest('hex');
  // Autocommit before network I/O. A process crash leaves a conservative reservation.
  const claim = db.prepare("INSERT OR IGNORE INTO deliveries(key,status) VALUES(?, 'sending')").run(key);
  if (!claim.changes) {
    const last = db.prepare('SELECT status,message_id FROM deliveries WHERE key=?').get(key);
    return last.status === 'sent'
      ? {...audit, status: 'already_sent', message_id: last.message_id}
      : {...audit, status: 'unknown', error_code: 'check_chat_before_resending'};
  }
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    const started_at = new Date().toISOString();
    const response = await send(token, 'sendMessage', {chat_id: chat, text: body.text});
    const result = outcome(response, chat);
    audit.attempts = attempt;
    let status = 'failed';
    if (result.message_id) status = 'sent';
    if (result.uncertain) status = 'unknown';
    audit.attempt_log.push({
      attempt, started_at, status, error_code: result.code ?? null, message_id: result.message_id ?? null,
    });
    if (result.message_id) {
      db.prepare("UPDATE deliveries SET status='sent', message_id=? WHERE key=?").run(result.message_id, key);
      return {...audit, status: 'sent', message_id: result.message_id};
    }
    if (result.retry && attempt < maxAttempts) {
      await sleep(1000 * Math.max(attempt, result.wait ?? 0));
      continue;
    }
    if (result.uncertain) {
      db.prepare("UPDATE deliveries SET status='unknown' WHERE key=?").run(key);
      return {...audit, status: 'unknown', error_code: result.code};
    }
    db.prepare('DELETE FROM deliveries WHERE key=?').run(key);
    return {...audit, error_code: result.code};
  }
}
