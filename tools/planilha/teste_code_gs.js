// Testes do Code.gs (Apps Script) sem dependências: node tools/planilha/teste_code_gs.js
// Carrega o Code.gs real num contexto vm com stubs em memória dos serviços do Google.
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const crypto = require('crypto');

const CODIGO = fs.readFileSync(path.join(__dirname, 'Code.gs'), 'utf8');
const TOKEN = 'segredo';

// ---------------------------------------------------------------- stubs
function novaAba(nome) {
  const aba = { nome, g: [], maxL: 1000, maxC: 26, oculta: false, congeladas: 0, formatos: [], negrito: [] };
  const val = (r, c) => (aba.g[r - 1] && aba.g[r - 1][c - 1] !== undefined ? aba.g[r - 1][c - 1] : '');
  aba.getRange = (r, c, nl, nc) => {
    nl = nl || 1; nc = nc || 1;
    const rg = {
      getValues() {
        const out = [];
        for (let i = 0; i < nl; i++) { const l = []; for (let j = 0; j < nc; j++) l.push(val(r + i, c + j)); out.push(l); }
        return out;
      },
      setValues(m) {
        if (m.length !== nl || m.some(l => l.length !== nc)) throw new Error('dimensões de setValues');
        for (let i = 0; i < nl; i++) for (let j = 0; j < nc; j++) {
          while (aba.g.length < r + i) aba.g.push([]);
          aba.g[r + i - 1][c + j - 1] = m[i][j];
        }
        return rg;
      },
      setFormulas(m) { return rg.setValues(m); },
      setFontWeight(w) { aba.negrito.push(w); return rg; },
      setNumberFormat(f) { aba.formatos.push(f); return rg; },
      getA1Notation() { return String.fromCharCode(64 + c) + r; },
    };
    return rg;
  };
  aba.getRangeList = (lista) => ({ setNumberFormat(f) { aba.formatos.push(f); return this; } });
  aba.getLastRow = () => { let u = 0; aba.g.forEach((l, i) => { if (l.some(x => x !== '' && x !== undefined)) u = i + 1; }); return u; };
  aba.getMaxRows = () => aba.maxL;
  aba.getMaxColumns = () => aba.maxC;
  aba.insertRowsAfter = (_, n) => { aba.maxL += n; };
  aba.insertColumnsAfter = (_, n) => { aba.maxC += n; };
  aba.setFrozenRows = (n) => { aba.congeladas = n; };
  aba.clear = () => { aba.g = []; };
  aba.hideSheet = () => { aba.oculta = true; };
  return aba;
}

function novoAmbiente() {
  const abas = {};
  const props = {};
  let contaId = 0;
  const arquivos = {};   // id -> arquivo
  const pastas = {};     // id -> pasta
  const novoId = (p) => p + (++contaId);

  function mkPasta(nome, pai) {
    const f = { id: novoId('pasta'), nome, pai, filhos: [] };
    pastas[f.id] = f;
    f.getId = () => f.id;
    f.getName = () => f.nome;
    f.createFolder = (n) => mkPasta(n, f);
    f.createFile = (blob) => mkArq(blob.getName(), blob.getBytes(), f);
    f.getFilesByName = (n) => iter(f.filhos.filter(a => a.nome === n));
    return f;
  }
  function mkArq(nome, bytes, pai) {
    const a = { id: novoId('arq'), nome, bytes: Buffer.from(bytes), pai };
    arquivos[a.id] = a;
    if (pai) pai.filhos.push(a);
    a.getId = () => a.id;
    a.getName = () => a.nome;
    a.getSize = () => a.bytes.length;
    a.getBlob = () => ({ getBytes: () => Array.from(a.bytes), getName: () => a.nome });
    a.getParents = () => iter(a.pai ? [a.pai] : []);
    return a;
  }
  function iter(lista) {
    let i = 0;
    return { hasNext: () => i < lista.length, next: () => lista[i++] };
  }

  const raiz = mkPasta('Meu Drive', null);
  const pastaDaPlanilha = mkPasta('Projetos', raiz);
  const arqPlanilha = mkArq('Planilha', [], pastaDaPlanilha);

  const planilha = {
    getId: () => arqPlanilha.id,
    getSheetByName: (n) => abas[n] || null,
    insertSheet: (n) => (abas[n] = novaAba(n)),
  };
  const ctx = {
    SpreadsheetApp: { getActiveSpreadsheet: () => planilha },
    PropertiesService: {
      getScriptProperties: () => ({
        getProperty: (k) => (k in props ? props[k] : null),
        setProperty: (k, v) => { props[k] = String(v); },
        deleteProperty: (k) => { delete props[k]; },
        getProperties: () => Object.assign({}, props),
        setProperties: (o) => { Object.keys(o).forEach(k => { props[k] = String(o[k]); }); },
      }),
    },
    LockService: {
      getScriptLock: () => ({ waitLock() { ctx.__travas++; }, releaseLock() { ctx.__travas--; } }),
    },
    DriveApp: {
      getFileById: (id) => { if (!arquivos[id]) throw new Error('não achei ' + id); return arquivos[id]; },
      getFolderById: (id) => { if (!pastas[id]) throw new Error('não achei ' + id); return pastas[id]; },
      getRootFolder: () => raiz,
    },
    Utilities: {
      base64Decode: (s) => Array.from(Buffer.from(s, 'base64')),
      base64Encode: (b) => Buffer.from(b).toString('base64'),
      newBlob: (bytes, tipo, nome) => ({ getBytes: () => bytes, getName: () => nome }),
    },
    ContentService: {
      MimeType: { JSON: 'json' },
      createTextOutput: (s) => ({ s, setMimeType() { return this; } }),
    },
    JSON, Math, Date, Object, Array, String, Number, isFinite,
    __travas: 0,
  };
  props.JFLP_TOKEN = TOKEN;
  vm.createContext(ctx);
  vm.runInContext(CODIGO, ctx, { filename: 'Code.gs' });

  let relogio = 1000000;
  ctx.agora_ = () => relogio;
  const env = {
    ctx, abas, props, arquivos, pastas, raiz, pastaDaPlanilha,
    avancar(ms) { relogio += ms; },
    post(corpo, comToken = true) {
      const c = comToken ? Object.assign({ token: TOKEN }, corpo) : corpo;
      const r = ctx.doPost({ postData: { contents: JSON.stringify(c) } });
      if (ctx.__travas !== 0) throw new Error('trava não liberada');
      return JSON.parse(r.s);
    },
  };
  return env;
}

// ---------------------------------------------------------------- mini framework
const testes = [];
const teste = (nome, fn) => testes.push([nome, fn]);
function igual(a, b, msg) {
  const x = JSON.stringify(a), y = JSON.stringify(b);
  if (x !== y) throw new Error((msg || 'esperava igual') + '\n  obtido:   ' + x + '\n  esperado: ' + y);
}
function ok(cond, msg) { if (!cond) throw new Error(msg || 'falhou'); }

const COLS = ['Projeto', 'Situação', 'Veredito J1', 'Veredito J2'];
const linha = (chave, proj, sit) => [chave, proj, sit, '', ''];
const b64 = (buf) => Buffer.from(buf).toString('base64');
const sha = (buf) => crypto.createHash('sha256').update(buf).digest('hex');

function enviarSync(env, chave, kind, runRel, maquina, extra) {
  return env.post(Object.assign({
    colunas: COLS, linhas: [linha(chave, 'P', 'OK')], maquina, maquina_nome: 'nome-' + maquina,
    sync: { [chave]: { [kind]: { estado: 'ok', revisao: '2026-11-05T10:00:00', run_rel: runRel } } },
  }, extra || {}));
}
function dadosSync(env, chave) {
  const aba = env.abas['_sync'];
  for (let i = 2; i <= aba.getLastRow(); i++) {
    const v = aba.getRange(i, 1, 1, 3).getValues()[0];
    if (v[0] === chave) return { seq: v[1], dados: JSON.parse(v[2]) };
  }
  return null;
}

// ---------------------------------------------------------------- casos
teste('doGet', () => {
  const env = novoAmbiente();
  const r = JSON.parse(env.ctx.doGet().s);
  ok(r.ok === true && r.versao === 2 && typeof r.mensagem === 'string');
});

teste('token errado e ausente são recusados em todas as ações', () => {
  const env = novoAmbiente();
  ['linhas', 'pacote', 'puxar', 'baixar', 'reservar', 'liberar'].forEach(acao => {
    const r = env.post({ acao, token: 'errado', colunas: [], linhas: [] }, false);
    igual([r.ok, r.erro, r.versao], [false, 'token', 2], acao);
  });
  const r = env.post({ colunas: [], linhas: [] }, false);
  igual(r.erro, 'token');
  const r2 = JSON.parse(env.ctx.doPost({ postData: { contents: 'nao json' } }).s);
  igual(r2.erro, 'json');
});

teste('ação desconhecida e formatos inválidos', () => {
  const env = novoAmbiente();
  igual(env.post({ acao: 'x' }).erro, 'formato');
  igual(env.post({ acao: 'linhas', colunas: 'a', linhas: [] }).erro, 'formato');
  igual(env.post({ acao: 'puxar', maquina: 'a', cursor: 'zero' }).erro, 'formato');
  igual(env.post({ acao: 'puxar', maquina: 'a', cursor: 0, limite: 'x' }).erro, 'formato');
  igual(env.post({ acao: 'reservar', maquina: 'a', chaves: 'k', ttl_s: 10 }).erro, 'formato');
  igual(env.post({ acao: 'reservar', maquina: 'a', chaves: ['k'], ttl_s: 'x' }).erro, 'formato');
  igual(env.post({ acao: 'liberar', maquina: 'a', chaves: [1] }).erro, 'formato');
  igual(env.post({ acao: 'baixar', id: 5 }).erro, 'formato');
  igual(env.post({ acao: 'pacote', maquina: 'a', chave: 'k', kind: 'J1', run_rel: 'r', sha256: 'curto', parte: 0, partes: 1, dados_b64: '' }).erro, 'formato');
  // sync malformado não grava nada
  const r = env.post({ colunas: COLS, linhas: [linha('k', 'P', 'OK')], maquina: 'a', sync: { k: { J1: { estado: 'bogus', revisao: 'x', run_rel: 'y' } } } });
  igual(r.erro, 'formato');
  ok(!env.abas['Bugs'], 'não deveria ter criado a aba');
});

teste('linhas sem acao (legado): upsert na aba Bugs e Resumo', () => {
  const env = novoAmbiente();
  let r = env.post({ colunas: COLS, linhas: [linha('b/p/1', 'P', 'OK'), linha('b/p/2', 'P', 'falhou')] });
  igual([r.ok, r.n, r.versao], [true, 2, 2]);
  const bugs = env.abas['Bugs'];
  igual(bugs.getRange(1, 1, 1, 5).getValues()[0], ['Chave'].concat(COLS));
  igual(bugs.getLastRow(), 3);
  // upsert: altera a linha 2 e acrescenta uma nova
  r = env.post({ colunas: COLS, linhas: [linha('b/p/1', 'P', 'parcial'), linha('b/p/3', 'Q', 'OK')] });
  igual(r.n, 2);
  igual(bugs.getLastRow(), 4);
  igual(bugs.getRange(2, 3, 1, 1).getValues()[0][0], 'parcial');
  igual(bugs.getRange(4, 1, 1, 1).getValues()[0][0], 'b/p/3');
  const resumo = env.abas['Resumo'];
  ok(resumo, 'Resumo criado');
  igual(resumo.getRange(2, 1, 2, 1).getValues().map(l => l[0]), ['P', 'Q']);
  // sem sync: nada de _sync
  ok(!env.abas['_sync']);
  // cabeçalho mudou numa aba com dados
  r = env.post({ colunas: COLS.concat(['Máquina']), linhas: [] });
  igual(r.cabecalho_mudou, true);
});

teste('sync: grava em _sync (oculta), no-echo não incrementa seq', () => {
  const env = novoAmbiente();
  let r = enviarSync(env, 'b/p/1', 'maven', 'runs/a', 'A');
  igual(r.ok, true);
  ok(env.abas['_sync'].oculta, '_sync oculta');
  let d = dadosSync(env, 'b/p/1');
  igual(d.seq, 1);
  igual(d.dados.maven, { estado: 'ok', maquina: 'A', maquina_nome: 'nome-A', revisao: '2026-11-05T10:00:00', run_rel: 'runs/a', pacote: null });
  igual(env.props.JFLP_SEQ, '1');
  // idêntico: nada muda
  enviarSync(env, 'b/p/1', 'maven', 'runs/a', 'A');
  igual(env.props.JFLP_SEQ, '1');
  igual(dadosSync(env, 'b/p/1').seq, 1);
  // outro run_rel: seq novo
  enviarSync(env, 'b/p/1', 'maven', 'runs/b', 'A');
  igual(dadosSync(env, 'b/p/1').seq, 2);
  // outra revisão
  enviarSync(env, 'b/p/1', 'maven', 'runs/b', 'A', { sync: { 'b/p/1': { maven: { estado: 'ok', revisao: 'outra', run_rel: 'runs/b' } } } });
  igual(dadosSync(env, 'b/p/1').seq, 3);
  // outro kind na mesma chave e outra chave: um seq por linha
  r = env.post({
    colunas: COLS, linhas: [], maquina: 'A', maquina_nome: 'n',
    sync: {
      'b/p/1': { J1: { estado: 'falhou', revisao: 'r', run_rel: 'x' } },
      'b/p/2': { J1: { estado: 'nao_exec', revisao: 'r', run_rel: 'y' } },
    },
  });
  igual(r.ok, true);
  igual(dadosSync(env, 'b/p/1').seq, 4);
  igual(Object.keys(dadosSync(env, 'b/p/1').dados).sort(), ['J1', 'maven']);
  igual(dadosSync(env, 'b/p/2').seq, 5);
  igual(env.props.JFLP_SEQ, '5');
});

function enviarPacote(env, chave, kind, runRel, maquina, buf, tam) {
  const partes = Math.ceil(buf.length / tam);
  const h = sha(buf);
  const respostas = [];
  for (let i = 0; i < partes; i++) {
    respostas.push(env.post({
      acao: 'pacote', maquina, chave, kind, run_rel: runRel, sha256: h, parte: i, partes,
      dados_b64: b64(buf.subarray(i * tam, (i + 1) * tam)),
    }));
  }
  return { h, partes, respostas };
}

teste('pacote em várias partes completa e grava pacote; reenvio é idempotente', () => {
  const env = novoAmbiente();
  enviarSync(env, 'b/p/1', 'J1', 'runs/a', 'A');
  const buf = crypto.randomBytes(2500);
  const { h, respostas } = enviarPacote(env, 'b/p/1', 'J1', 'runs/a', 'A', buf, 1000);
  igual(respostas.map(r => r.completo), [false, false, true]);
  ok(respostas.every(r => r.ok && !r.obsoleto));
  const d = dadosSync(env, 'b/p/1');
  igual(d.dados.J1.pacote.sha256, h);
  igual(d.dados.J1.pacote.partes, 3);
  igual(d.dados.J1.pacote.bytes, 2500);
  igual(d.dados.J1.pacote.ids.length, 3);
  igual(d.seq, 2);
  // pasta criada ao lado da planilha e id guardado
  const pasta = env.pastas[env.props.JFLP_PASTA];
  igual(pasta.nome, 'JFLP pacotes');
  ok(pasta.pai === env.pastaDaPlanilha, 'pasta na mesma pasta-mãe da planilha');
  igual(pasta.filhos.length, 3);
  // reenviar tudo: sem duplicar arquivos nem novo seq
  const again = enviarPacote(env, 'b/p/1', 'J1', 'runs/a', 'A', buf, 1000);
  igual(again.respostas.map(r => r.completo), [true, true, true]);
  igual(pasta.filhos.length, 3);
  igual(dadosSync(env, 'b/p/1').seq, 2);
});

teste('pacote sem pasta-mãe cai na raiz', () => {
  const env = novoAmbiente();
  env.pastaDaPlanilha.filhos.length = 0;
  // faz a planilha ficar sem pai
  const idPlan = Object.keys(env.arquivos).find(k => env.arquivos[k].nome === 'Planilha');
  env.arquivos[idPlan].pai = null;
  enviarSync(env, 'k', 'J1', 'r', 'A');
  enviarPacote(env, 'k', 'J1', 'r', 'A', Buffer.from('abc'), 10);
  ok(env.pastas[env.props.JFLP_PASTA].pai === env.raiz);
});

teste('pacote obsoleto quando run_rel ou máquina não conferem', () => {
  const env = novoAmbiente();
  enviarSync(env, 'k', 'J1', 'runs/novo', 'A');
  const antes = dadosSync(env, 'k');
  let { respostas } = enviarPacote(env, 'k', 'J1', 'runs/velho', 'A', Buffer.from('xyz'), 10);
  igual([respostas[0].completo, respostas[0].obsoleto], [true, true]);
  ({ respostas } = enviarPacote(env, 'k', 'J1', 'runs/novo', 'B', Buffer.from('xyz'), 10));
  igual(respostas[0].obsoleto, true);
  // chave que nem existe em _sync
  ({ respostas } = enviarPacote(env, 'nada', 'J1', 'runs/novo', 'A', Buffer.from('xyz'), 10));
  igual(respostas[0].obsoleto, true);
  igual(dadosSync(env, 'k'), antes);
  igual(env.props.JFLP_SEQ, '1');
});

teste('puxar: cursor, limite, mais, ordenação e reservas só de outras máquinas', () => {
  const env = novoAmbiente();
  for (let i = 1; i <= 5; i++) enviarSync(env, 'b/p/' + i, 'maven', 'r' + i, i % 2 ? 'A' : 'B');
  let r = env.post({ acao: 'puxar', maquina: 'A', cursor: 0 });
  igual([r.ok, r.versao, r.mais, r.cursor], [true, 2, false, 5]);
  igual(r.mudancas.map(m => m.seq), [1, 2, 3, 4, 5]);
  // não filtra por máquina: inclui as de A
  igual(r.mudancas.map(m => m.dados.maven.maquina), ['A', 'B', 'A', 'B', 'A']);
  igual(r.mudancas[0].chave, 'b/p/1');
  // limite
  r = env.post({ acao: 'puxar', maquina: 'A', cursor: 0, limite: 2 });
  igual([r.mais, r.cursor, r.mudancas.length], [true, 2, 2]);
  r = env.post({ acao: 'puxar', maquina: 'A', cursor: 2, limite: 2 });
  igual([r.mais, r.cursor, r.mudancas.map(m => m.seq)], [true, 4, [3, 4]]);
  r = env.post({ acao: 'puxar', maquina: 'A', cursor: 4, limite: 2 });
  igual([r.mais, r.cursor, r.mudancas.map(m => m.seq)], [false, 5, [5]]);
  // nada novo: cursor volta o próprio
  r = env.post({ acao: 'puxar', maquina: 'A', cursor: 5 });
  igual([r.mais, r.cursor, r.mudancas], [false, 5, []]);
  // limite acima do máximo vira 500 (sem erro)
  igual(env.post({ acao: 'puxar', maquina: 'A', cursor: 0, limite: 100000 }).ok, true);
  // reservas
  env.post({ acao: 'reservar', maquina: 'A', maquina_nome: 'nA', chaves: ['x'], ttl_s: 60 });
  env.post({ acao: 'reservar', maquina: 'B', maquina_nome: 'nB', chaves: ['y'], ttl_s: 120 });
  r = env.post({ acao: 'puxar', maquina: 'A', cursor: 5 });
  igual(r.reservas, { y: { maquina: 'B', maquina_nome: 'nB', expira_em_s: 120 } });
  r = env.post({ acao: 'puxar', maquina: 'B', cursor: 5 });
  igual(Object.keys(r.reservas), ['x']);
  // reserva vencida some do puxar
  env.avancar(61 * 1000);
  r = env.post({ acao: 'puxar', maquina: 'B', cursor: 5 });
  igual(r.reservas, {});
});

teste('baixar: ida e volta dos pedaços', () => {
  const env = novoAmbiente();
  enviarSync(env, 'k', 'J1', 'r', 'A');
  const buf = crypto.randomBytes(3000);
  enviarPacote(env, 'k', 'J1', 'r', 'A', buf, 1024);
  const p = env.post({ acao: 'puxar', maquina: 'B', cursor: 0 }).mudancas[0].dados.J1.pacote;
  const bufs = p.ids.map(id => {
    const r = env.post({ acao: 'baixar', id });
    igual([r.ok, r.versao], [true, 2]);
    return Buffer.from(r.dados_b64, 'base64');
  });
  const junto = Buffer.concat(bufs);
  ok(junto.equals(buf), 'bytes iguais');
  igual(sha(junto), p.sha256);
});

teste('baixar recusa arquivo fora da pasta e id inexistente', () => {
  const env = novoAmbiente();
  // sem pasta ainda
  igual(env.post({ acao: 'baixar', id: 'qualquer' }).ok, false);
  enviarSync(env, 'k', 'J1', 'r', 'A');
  enviarPacote(env, 'k', 'J1', 'r', 'A', Buffer.from('oi'), 10);
  const idPlan = Object.keys(env.arquivos).find(k => env.arquivos[k].nome === 'Planilha');
  const r = env.post({ acao: 'baixar', id: idPlan });
  igual([r.ok, r.erro], [false, 'arquivo']);
  ok(!('dados_b64' in r));
  igual(env.post({ acao: 'baixar', id: 'naoexiste' }).erro, 'arquivo');
});

teste('reservar: concede, nega, renova, expira', () => {
  const env = novoAmbiente();
  let r = env.post({ acao: 'reservar', maquina: 'A', maquina_nome: 'nA', chaves: ['k1', 'k2'], ttl_s: 100 });
  igual([r.ok, r.versao, r.concedidas, r.negadas], [true, 2, ['k1', 'k2'], {}]);
  ok('r:k1' in env.props);
  // outra máquina: negada nas duas
  r = env.post({ acao: 'reservar', maquina: 'B', maquina_nome: 'nB', chaves: ['k1', 'k2', 'k3'], ttl_s: 100 });
  igual(r.concedidas, ['k3']);
  igual(r.negadas, { k1: { maquina: 'A', maquina_nome: 'nA', expira_em_s: 100 }, k2: { maquina: 'A', maquina_nome: 'nA', expira_em_s: 100 } });
  // passa o tempo e A renova
  env.avancar(60 * 1000);
  r = env.post({ acao: 'reservar', maquina: 'B', maquina_nome: 'nB', chaves: ['k1'], ttl_s: 100 });
  igual(r.negadas.k1.expira_em_s, 40);
  r = env.post({ acao: 'reservar', maquina: 'A', maquina_nome: 'nA', chaves: ['k1'], ttl_s: 100 });
  igual(r.concedidas, ['k1']);
  env.avancar(60 * 1000);   // k2 (sem renovação) venceu; k1 ainda vive
  r = env.post({ acao: 'reservar', maquina: 'B', maquina_nome: 'nB', chaves: ['k1', 'k2'], ttl_s: 100 });
  igual(r.concedidas, ['k2']);
  igual(Object.keys(r.negadas), ['k1']);
  igual(r.negadas.k1.expira_em_s, 40);
  // reservas vencidas são podadas
  env.avancar(10 * 60 * 1000);
  env.post({ acao: 'reservar', maquina: 'C', maquina_nome: 'nC', chaves: ['z'], ttl_s: 10 });
  igual(Object.keys(env.props).filter(k => k.startsWith('r:')), ['r:z']);
});

teste('liberar remove só reservas da própria máquina', () => {
  const env = novoAmbiente();
  env.post({ acao: 'reservar', maquina: 'A', maquina_nome: 'nA', chaves: ['k1', 'k2'], ttl_s: 100 });
  env.post({ acao: 'reservar', maquina: 'B', maquina_nome: 'nB', chaves: ['k3'], ttl_s: 100 });
  let r = env.post({ acao: 'liberar', maquina: 'A', chaves: ['k1', 'k3', 'inexistente'] });
  igual([r.ok, r.versao, r.n], [true, 2, 1]);
  igual(Object.keys(env.props).filter(k => k.startsWith('r:')).sort(), ['r:k2', 'r:k3']);
  // depois de liberar, outra máquina consegue
  r = env.post({ acao: 'reservar', maquina: 'B', maquina_nome: 'nB', chaves: ['k1'], ttl_s: 10 });
  igual(r.concedidas, ['k1']);
});

teste('conflito de _sync: estado só muda estado quando run_rel igual preserva pacote', () => {
  const env = novoAmbiente();
  enviarSync(env, 'k', 'J1', 'r', 'A');
  enviarPacote(env, 'k', 'J1', 'r', 'A', Buffer.from('dados'), 100);
  env.post({ colunas: COLS, linhas: [], maquina: 'A', maquina_nome: 'n', sync: { k: { J1: { estado: 'falhou', revisao: '2026-11-05T10:00:00', run_rel: 'r' } } } });
  const d = dadosSync(env, 'k');
  igual(d.dados.J1.estado, 'falhou');
  ok(d.dados.J1.pacote && d.dados.J1.pacote.partes === 1, 'pacote preservado');
  igual(d.seq, 3);
  // novo run_rel zera o pacote
  enviarSync(env, 'k', 'J1', 'r2', 'A');
  igual(dadosSync(env, 'k').dados.J1.pacote, null);
});

// ---------------------------------------------------------------- execução
let falhas = 0;
testes.forEach(([nome, fn]) => {
  try {
    fn();
    console.log('ok   ' + nome);
  } catch (e) {
    falhas++;
    console.log('FALHA ' + nome + '\n  ' + String(e && e.stack || e).split('\n').slice(0, 6).join('\n  '));
  }
});
console.log('\n' + (testes.length - falhas) + '/' + testes.length + ' testes passaram');
process.exit(falhas ? 1 : 0);
