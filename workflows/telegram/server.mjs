import http from 'node:http';
import {callTelegram, deliver, ledger} from './delivery.mjs';

export function deliveryServer({db, chat, send = callTelegram, sleep}) {
  return http.createServer(async (req, res) => {
    function reply(status, data) {
      res.writeHead(status, {'content-type': 'application/json', 'cache-control': 'no-store'});
      res.end(JSON.stringify(data));
    }
    if (req.url === '/healthz') return reply(200, {ready: true});
    const route = /^\/bot([^/?#]{0,256})\/(getMe|sendMessage)$/.exec(req.url);
    if (!route) return reply(404, {error: 'unknown_method'});
    const token = route[1];
    const authenticatedSend = /^\d+:[A-Za-z0-9_-]+$/.test(token)
      ? send : async () => ({status: 401});
    try {
      if (route[2] === 'getMe') {
        const {status, data} = await authenticatedSend(token, 'getMe', {});
        if (status === 200 && data?.ok === true && data.result?.is_bot === true) {
          return reply(200, {ok: true, result: {is_bot: true}});
        }
        return reply(status === 401 || status === 404 ? 401 : 503, {error: 'credential_check_failed'});
      }
      if (req.method !== 'POST') return reply(405, {error: 'method'});
      const chunks = [];
      let size = 0;
      for await (const chunk of req) {
        size += chunk.length;
        if (size > 32768) return reply(413, {error: 'invalid_message'});
        chunks.push(chunk);
      }
      const body = JSON.parse(Buffer.concat(chunks).toString());
      return reply(200, {delivery: await deliver({db, chat, token, body, send: authenticatedSend, sleep})});
    } catch {
      // An internal failure could follow a confirmed Telegram write.
      return reply(200, {delivery: {status: 'unknown', error_code: 'check_chat_before_resending'}});
    }
  });
}

if (import.meta.main) {
  const db = ledger('/home/node/.n8n/eduflow-telegram.sqlite');
  deliveryServer({db, chat: process.env.EDUFLOW_TELEGRAM_CHAT_ID}).listen(5687, '127.0.0.1');
}
