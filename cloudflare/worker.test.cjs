const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');
const path = require('node:path');
const { DatabaseSync } = require('node:sqlite');
const { webcrypto } = require('node:crypto');

const source = fs.readFileSync(path.join(__dirname, 'worker.js'), 'utf8');
const deferred = () => { let resolve; const promise = new Promise(r => resolve = r); return {promise, resolve}; };
class LocalD1 {
  constructor(repos = []) {
    this.sqlite = new DatabaseSync(':memory:');
    this.sqlite.exec(`
      CREATE TABLE subscriptions(repo TEXT PRIMARY KEY, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
      CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);
      CREATE TABLE processed_updates(update_id INTEGER PRIMARY KEY, processed_at TEXT DEFAULT CURRENT_TIMESTAMP);
    `);
    for (const repo of repos) this.sqlite.prepare('INSERT INTO subscriptions(repo) VALUES (?)').run(repo);
  }
  prepare(sql) {
    const db = this;
    let params = [];
    const execute = async method => {
      if (db.before) await db.before({sql, params, method});
      const stmt = db.sqlite.prepare(sql);
      let result;
      if (method === 'first') result = stmt.get(...params) || null;
      else if (method === 'all') result = {results: stmt.all(...params)};
      else result = {meta: {changes: Number(stmt.run(...params).changes)}};
      if (db.after) await db.after({sql, params, method, result});
      return result;
    };
    return {
      bind(...values) {params = values; return this;},
      first() {return execute('first');},
      all() {return execute('all');},
      run() {return execute('run');},
    };
  }
}

function setup(repos = []) {
  const DB = new LocalD1(repos);
  const calls = [];
  const env = {DB, TELEGRAM_BOT_TOKEN:'test-token', TELEGRAM_CHAT_ID:'42', WEBHOOK_SECRET:'test-webhook-secret', SYNC_SECRET:'test-sync-secret'};
  const context = {
    Request, Response, URL, TextEncoder, AbortSignal, crypto:webcrypto,
    console:{log(){}, warn(){}, error(){}},
    async fetch(url, options = {}) {
      calls.push({url, options});
      if (url.startsWith('https://api.telegram.org/bottest-token/')) {
        return Response.json({ok:true, result:{message_id:100 + calls.length}});
      }
      if (url.startsWith('https://api.github.com/repos/')) {
        if (url.endsWith('/dispatches')) return new Response(null, {status:204});
        const repo = new URL(url).pathname.slice('/repos/'.length);
        return Response.json({full_name:repo, private:false, archived:false});
      }
      throw new Error('Unexpected network target; no real networking is permitted');
    },
  };
  vm.createContext(context);
  vm.runInContext(source.replace('export default {', 'globalThis.worker = {'), context);
  const sendUpdate = (id, text, from = 42, chat = 42, chatType = 'private', secret = env.WEBHOOK_SECRET) => context.worker.fetch(new Request('https://worker.test/telegram', {
    method:'POST', headers:{'Content-Type':'application/json','X-Telegram-Bot-Api-Secret-Token':secret},
    body:JSON.stringify({update_id:id, message:{text, chat:{id:chat,type:chatType}, from:{id:from}}}),
  }), env);
  const sendReport = report => context.worker.fetch(new Request('https://worker.test/report', {
    method:'POST', headers:{'Content-Type':'application/json',Authorization:'Bearer '+env.SYNC_SECRET}, body:JSON.stringify(report),
  }), env);
  const telegramSends = () => calls.filter(x=>x.url.endsWith('/sendMessage'));
  return {DB, env, context, calls, sendUpdate, sendReport, telegramSends};
}

test('list shows linked latest published version from cached Cloudflare catalog', async () => {
  const s = setup(['drakmor/ShadowMountPlus', 'itsPLK/ps5-y2jb-autoloader', 'owner/no-releases']);
  const catalog = {
    completed_at:new Date().toISOString(),
    releases:{
      'drakmor/ShadowMountPlus':{tag:'1.7beta11', url:'https://github.com/drakmor/ShadowMountPlus/releases/tag/1.7beta11'},
      'itsPLK/ps5-y2jb-autoloader':{tag:'v1.1.0', url:'https://github.com/itsPLK/ps5-y2jb-autoloader/releases/tag/v1.1.0'},
      'owner/no-releases':{tag:'',url:''},
    },
  };
  const url = 'https://worker.test/catalog';
  let response = await s.context.worker.fetch(new Request(url, {
    method:'POST',headers:{Authorization:'Bearer test-sync'},body:JSON.stringify(catalog),
  }),s.env);
  assert.equal(response.status,200);
  await s.sendUpdate(1001,'/list');
  const message = JSON.parse(s.telegramSends()[0].options.body).text;
  assert.match(message,/ShadowMountPlus/);
  assert.match(message,/1\.7beta11/);
  assert.match(message,/ps5-y2jb-autoloader/);
  assert.match(message,/v1\.1\.0/);
  assert.match(message,/нет опубликованных релизов/);
  assert.match(message,/<a href="https:\/\/github\.com\/drakmor\/ShadowMountPlus\/releases\/tag\/1\.7beta11">/);
  assert.equal(s.calls.filter(x=>x.url.startsWith('https://api.github.com')).length,0);
});

test('latest releases retain cached values on partial scans and reject delayed reports', async () => {
  const s = setup(['owner/one','owner/two']);
  const url='https://worker.test/catalog';
  const send = async payload => s.context.worker.fetch(new Request(url, {
    method:'POST',headers:{Authorization:'Bearer test-sync'},body:JSON.stringify(payload),
  }),s.env);
  const t = Date.now();
  const first = new Date(t-60000).toISOString();
  const second = new Date(t-10000).toISOString();
  assert.equal((await send({completed_at:first,releases:{
    'owner/one':{tag:'v1',url:'https://github.com/owner/one/releases/tag/v1'},
    'owner/two':{tag:'v2',url:'https://github.com/owner/two/releases/tag/v2'},
  }})).status,200);
  assert.equal((await send({completed_at:second,releases:{
    'owner/one':{tag:'v1.1-alpha',url:'https://github.com/owner/one/releases/tag/v1.1-alpha'},
  }})).status,200);
  assert.equal((await send({completed_at:first,releases:{
    'owner/one':{tag:'OLD',url:'https://github.com/owner/one/releases/tag/OLD'},
  }})).status,200);
  const saved=JSON.parse(s.DB.sqlite.prepare("SELECT value FROM settings WHERE key='monitor_latest_releases'").get().value);
  assert.equal(saved.releases['owner/one'].tag,'v1.1-alpha');
  assert.equal(saved.releases['owner/two'].tag,'v2');
  assert.equal(saved.completed_at,second);
});

test('catalog input requires shared secret and rejects unsafe HTML or links', async () => {
  const s = setup(['owner/one']);
  const url='https://worker.test/catalog';
  const content = {completed_at:new Date().toISOString(),releases:{
    'owner/one':{tag:'<b>v2</b>',url:'https://github.com/owner/one/releases/tag/v2'},
  }};
  const mk = (p,auth) => new Request(url,{method:'POST',
    headers:{Authorization:auth},body:JSON.stringify(p)});
  assert.equal((await s.context.worker.fetch(mk(content,'wrong'),s.env)).status,401);
  assert.equal((await s.context.worker.fetch(mk({...content,releases:{
    'owner/one':{tag:'v2',url:'https://evil.example/releases/tag/v2'},
  }},'Bearer test-sync'),s.env)).status,400);
  assert.equal((await s.context.worker.fetch(mk(content,'Bearer test-sync'),s.env)).status,200);
  await s.sendUpdate(1002,'/list');
  const message = JSON.parse(s.telegramSends()[0].options.body).text;
  assert.match(message,/&lt;b&gt;v2&lt;\/b&gt;/);
  assert.doesNotMatch(message,/<b>v2<\/b>/);
});

test('list remains available with no catalog and splits long lists into Telegram-safe messages', async () => {
  const repos = Array.from({length:30},(_,i)=>'owner/project-'+String(i).padStart(2,'0')+'-long-name');
  const s=setup(repos);
  await s.sendUpdate(1003,'/list');
  let messages = s.telegramSends().map(x=>JSON.parse(x.options.body).text);
  assert.match(messages[0],/ожидается первая проверка/);
  const catalog={completed_at:new Date().toISOString(),releases:Object.fromEntries(
    repos.map(repo=>[repo,{tag:'v'+('3'.repeat(110)),
      url:'https://github.com/'+repo+'/releases/tag/v3'}]))};
  assert.equal((await s.context.worker.fetch(new Request('https://worker.test/catalog',{
    method:'POST',headers:{Authorization:'Bearer test-sync'},body:JSON.stringify(catalog),
  }),s.env)).status,200);
  await s.sendUpdate(1004,'/list');
  messages = s.telegramSends().slice(1).map(x=>JSON.parse(x.options.body).text);
  assert.ok(messages.length>=2);
  assert.ok(messages.every(x=>x.length<4096));
  assert.ok(messages.join('\n').includes('project-29'));
});

test('webhook authentication and owner/private-chat authorization', async () => {
  const s = setup(['owner/project']);
  assert.equal((await s.sendUpdate(1,'/list',42,42,'private','wrong-secret')).status,403);
  assert.equal((await s.sendUpdate(2,'/translate_off',99,99)).status,200);
  assert.equal((await s.sendUpdate(3,'/translate_off',42,42,'group')).status,200);
  assert.equal(s.telegramSends().length,0);
  assert.equal(s.DB.sqlite.prepare("SELECT count(*) AS n FROM settings WHERE key='translate_enabled'").get().n,0);
  assert.equal((await s.context.worker.fetch(new Request('https://worker.test/sync'),s.env)).status,401);
  assert.equal((await s.context.worker.fetch(new Request('https://worker.test/report',{method:'POST',body:'{}'}),s.env)).status,401);
});

test('completed updates are not executed on a sequential retry', async () => {
  const s = setup(['owner/project']);
  await s.sendUpdate(4,'/list');
  await s.sendUpdate(4,'/list');
  assert.equal(s.telegramSends().length,1);
});

test('report validation and atomic ordering retain the newer run', async () => {
  const s = setup();
  const newest = {run_id:'20',completed_at:new Date(Date.now()-10000).toISOString(),status:'success',checked:8,total:8,notifications:0,errors:0,retry_at:null};
  const older = {...newest,run_id:'19',completed_at:new Date(Date.now()-70000).toISOString(),status:'error',checked:1,errors:1};
  assert.equal((await s.sendReport(newest)).status,200);
  assert.equal((await s.sendReport(older)).status,200);
  const saved=JSON.parse(s.DB.sqlite.prepare("SELECT value FROM settings WHERE key='monitor_latest_run'").get().value);
  assert.equal(saved.run_id,'20');
  assert.equal((await s.sendReport({...newest,checked:9})).status,400);
  await s.sendUpdate(5,'/status');
  assert.match(JSON.parse(s.telegramSends()[0].options.body).text,/Проверено: 8\/8/);
});

test('delayed concurrent duplicate cannot claim a completed update', async () => {
  const s = setup(['owner/project']);
  const bothRead = deferred(), firstFinished = deferred();
  let reads = 0, claims = 0, deletes = 0;
  s.DB.after = async e => {
    if (e.method==='first' && e.sql.startsWith('SELECT update_id') && e.params[0]===500) {
      if (++reads===2) bothRead.resolve();
      if (reads<=2) await bothRead.promise;
    }
    if (e.method==='run' && e.sql==='DELETE FROM processed_updates WHERE update_id = ?' && e.params[0]===-501 && ++deletes===1) firstFinished.resolve();
  };
  s.DB.before = async e => {
    if (e.method==='run' && e.sql.startsWith('INSERT OR IGNORE INTO processed_updates') && e.params[0]===-501 && ++claims===2) await firstFinished.promise;
  };
  const results = await Promise.all([s.sendUpdate(500,'/list'),s.sendUpdate(500,'/list')]);
  assert.deepEqual(results.map(r=>r.status),[200,200]);
  assert.equal(s.telegramSends().length,1);
  assert.equal(s.DB.sqlite.prepare('SELECT count(*) AS n FROM processed_updates WHERE update_id < 0').get().n,0);
});

test('active claims request retry; expired abandoned claims recover', async () => {
  const s=setup();
  s.DB.sqlite.prepare('INSERT INTO processed_updates(update_id) VALUES (?)').run(-801);
  assert.equal((await s.sendUpdate(800,'/list')).status,503);
  assert.equal(s.telegramSends().length,0);
  s.DB.sqlite.prepare("UPDATE processed_updates SET processed_at = datetime('now','-120 seconds') WHERE update_id = ?").run(-801);
  assert.equal((await s.sendUpdate(800,'/list')).status,200);
  assert.equal(s.telegramSends().length,1);
  assert.equal((await s.sendUpdate(800,'/list')).status,200);
  assert.equal(s.telegramSends().length,1);
});

function overlapRefreshes(s) {
  let repoReads=0;
  const bothRefreshed=deferred();
  s.DB.after=async e=>{
    if(e.method==='all' && e.sql.startsWith('SELECT repo FROM subscriptions')) {
      repoReads++;
      if(repoReads===4) bothRefreshed.resolve();
      if(repoReads===3 || repoReads===4) await bothRefreshed.promise;
    }
  };
}

test('concurrent additions cannot exceed the 30-subscription limit', async () => {
  const s=setup(Array.from({length:29},(_,i)=>'owner/repo'+i));
  overlapRefreshes(s);
  const results=await Promise.all([s.sendUpdate(601,'https://github.com/owner/new-first'),s.sendUpdate(602,'https://github.com/owner/new-second')]);
  assert.deepEqual(results.map(r=>r.status),[200,200]);
  assert.equal(s.DB.sqlite.prepare('SELECT count(*) AS n FROM subscriptions').get().n,30);
  const texts=s.telegramSends().map(x=>JSON.parse(x.options.body).text);
  assert.equal(texts.filter(x=>x.includes('Подписка добавлена')).length,1);
  assert.equal(texts.filter(x=>x.includes('Лимит: 30')).length,1);
});

test('case variants cannot create concurrent duplicate subscriptions', async () => {
  // Deliberately use a case-sensitive SQLite primary key. The SQL operation
  // itself must prevent duplicates without requiring a schema migration.
  const s=setup();
  overlapRefreshes(s);
  await Promise.all([s.sendUpdate(701,'https://github.com/owner/project'),s.sendUpdate(702,'https://github.com/OWNER/PROJECT')]);
  assert.equal(s.DB.sqlite.prepare('SELECT count(*) AS n FROM subscriptions').get().n,1);
  const texts=s.telegramSends().map(x=>JSON.parse(x.options.body).text);
  assert.equal(texts.filter(x=>x.includes('Подписка добавлена')).length,1);
  assert.equal(texts.filter(x=>x.includes('уже подписан')).length,1);
});

test('optional manual dispatch has a network timeout', async () => {
  const s=setup();
  s.env.GITHUB_DISPATCH_TOKEN='test-dispatch-token';
  await s.sendUpdate(900,'/check');
  const call=s.calls.find(c=>c.url.endsWith('/dispatches'));
  assert.ok(call.options.signal instanceof AbortSignal);
});

test('health reports success only with configuration and a working database', async () => {
  const s=setup();
  const getHealth=()=>s.context.worker.fetch(new Request('https://worker.test/health'),s.env);
  const good=await getHealth();
  assert.equal(good.status,200);
  assert.equal((await good.json()).db_ok,true);
  s.DB.before=async()=>{throw new Error('Database unavailable')};
  const bad=await getHealth();
  assert.equal(bad.status,503);
  const payload=await bad.json();
  assert.equal(payload.ready,false);
  assert.equal(payload.ok,false);
  assert.equal(payload.db_ok,false);
  assert.equal(payload.db_bound,true);
});

