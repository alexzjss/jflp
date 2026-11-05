/**
 * JFLP Studio - receptor da planilha do Google (protocolo versão 2: planilha + hub de sincronização).
 *
 * Como instalar (leva uns 5 minutos):
 *  1. Abra a planilha vazia que vai receber os resultados.
 *  2. No menu, vá em Extensões > Apps Script e apague o conteúdo do arquivo Code.gs.
 *  3. Cole este arquivo inteiro no lugar e clique em Salvar.
 *  4. Em Configurações do projeto (engrenagem) > Propriedades do script, adicione a propriedade
 *     JFLP_TOKEN com o token mostrado na tela Ambiente do JFLP Studio.
 *  5. Clique em Implantar > Nova implantação > tipo "App da web". Em "Executar como" escolha
 *     "Eu" e em "Quem pode acessar" escolha "Qualquer pessoa". Autorize o acesso quando pedido.
 *     A primeira execução pede também permissão para o Google Drive (os pacotes das execuções
 *     ficam na pasta "JFLP pacotes", criada ao lado da planilha); aceite.
 *  6. Copie a URL do app da web e cole na tela Ambiente do JFLP Studio, no campo da planilha.
 *
 * Para ATUALIZAR este código numa instalação existente: cole o arquivo novo, salve e use
 * Implantar > Gerenciar implantações > Editar (lápis) > Versão: Nova versão > Implantar.
 * Sem criar a nova versão, a URL continua servindo o código antigo.
 *
 * O JFLP Studio manda um POST com JSON {token, acao, ...}. Sem "acao" vale "linhas" (app antigo):
 * {token, colunas, linhas}; cada linha começa pela chave do bug (benchmark/projeto/ramo) e as linhas
 * novas ou alteradas substituem a linha da mesma chave na aba "Bugs". A aba "Resumo" é refeita com
 * fórmulas que contam as linhas da aba "Bugs". Ações: linhas, pacote, puxar, baixar, reservar, liberar
 * (contrato em docs/superpowers/specs/2026-11-05-jflp-sincronia-design.md). A aba oculta "_sync" guarda
 * o estado de cada bug por etapa, com um número de sequência para as máquinas puxarem só o que mudou.
 */

var VERSAO = 2;
var ABA_BUGS = 'Bugs';
var ABA_RESUMO = 'Resumo';
var ABA_SYNC = '_sync';
var NOME_PASTA = 'JFLP pacotes';
var ESTADOS = ['ok', 'falhou', 'nao_exec'];
var LIMITE_PADRAO = 200;
var LIMITE_MAXIMO = 500;
var MAX_PARTES = 2000;
var TTL_MAXIMO_S = 86400;

/** Relógio do servidor em ms (função própria para os testes poderem trocar). */
function agora_() {
  return new Date().getTime();
}

function formato_() {
  return { ok: false, erro: 'formato' };
}

function ehObjeto_(v) {
  return !!v && typeof v === 'object' && !Array.isArray(v);
}

function ehTexto_(v) {
  return typeof v === 'string' && v !== '';
}

function ehInteiro_(v) {
  return typeof v === 'number' && isFinite(v) && Math.floor(v) === v;
}

function doPost(e) {
  var trava = LockService.getScriptLock();
  var travado = false;
  try {
    var corpo = null;
    try {
      corpo = JSON.parse(e.postData.contents);
    } catch (err) {
      corpo = null;
    }
    if (!corpo || typeof corpo !== 'object' || Array.isArray(corpo)) {
      return resposta_({ ok: false, erro: 'json' });
    }
    var token = PropertiesService.getScriptProperties().getProperty('JFLP_TOKEN');
    if (!token || corpo.token !== token) {
      return resposta_({ ok: false, erro: 'token' });
    }
    var acao = corpo.acao === undefined || corpo.acao === null ? 'linhas' : corpo.acao;
    var tratadores = {
      linhas: acaoLinhas_, pacote: acaoPacote_, puxar: acaoPuxar_, baixar: acaoBaixar_,
      reservar: acaoReservar_, liberar: acaoLiberar_
    };
    var somenteLeitura = { puxar: true, baixar: true };
    if (typeof acao !== 'string' || !tratadores.hasOwnProperty(acao)) return resposta_(formato_());
    var tratador = tratadores[acao];
    if (!somenteLeitura[acao]) {
      try {
        trava.waitLock(30000);
        travado = true;
      } catch (err) {
        return resposta_({ ok: false, erro: 'lock' });
      }
    }
    return resposta_(tratador(corpo));
  } catch (err) {
    return resposta_({ ok: false, erro: String(err) });
  } finally {
    if (travado) trava.releaseLock();
  }
}

function doGet() {
  return resposta_({ ok: true, versao: VERSAO, mensagem: 'JFLP Studio: receptor da planilha no ar' });
}

function resposta_(obj) {
  obj.versao = VERSAO;
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

// ---------------------------------------------------------------------------------------------
// ação "linhas"
// ---------------------------------------------------------------------------------------------

function acaoLinhas_(corpo) {
  if (!Array.isArray(corpo.colunas) || !Array.isArray(corpo.linhas)) return formato_();
  var temSync = corpo.sync !== undefined && corpo.sync !== null;
  if (temSync) {
    if (!validarSync_(corpo.sync) || !ehTexto_(corpo.maquina)) return formato_();
    if (corpo.maquina_nome !== undefined && corpo.maquina_nome !== null && typeof corpo.maquina_nome !== 'string') {
      return formato_();
    }
  }
  var r = gravar_(corpo.colunas, corpo.linhas);
  atualizarResumo_(corpo.colunas, r.projetos, r.cabecalho_mudou);
  if (temSync) aplicarSync_(corpo.sync, corpo.maquina, corpo.maquina_nome || '');
  var saida = { ok: true, n: r.n };
  if (r.cabecalho_mudou) saida.cabecalho_mudou = true;
  return saida;
}

/** {chave: {kind: {estado, revisao, run_rel}}} com tipos corretos. */
function validarSync_(sync) {
  if (!ehObjeto_(sync)) return false;
  var chaves = Object.keys(sync);
  for (var i = 0; i < chaves.length; i++) {
    var etapas = sync[chaves[i]];
    if (!ehObjeto_(etapas)) return false;
    var kinds = Object.keys(etapas);
    for (var j = 0; j < kinds.length; j++) {
      var d = etapas[kinds[j]];
      if (!ehObjeto_(d)) return false;
      if (ESTADOS.indexOf(d.estado) < 0) return false;
      if (typeof d.revisao !== 'string' || typeof d.run_rel !== 'string') return false;
    }
  }
  return true;
}

// ---------------------------------------------------------------------------------------------
// aba oculta "_sync" (chave | seq | dados JSON) e contador JFLP_SEQ
// ---------------------------------------------------------------------------------------------

/** Lê a aba "_sync" inteira com um getValues (cria e oculta a aba se faltar). */
function lerSync_() {
  var planilha = SpreadsheetApp.getActiveSpreadsheet();
  var aba = planilha.getSheetByName(ABA_SYNC);
  if (!aba) {
    aba = planilha.insertSheet(ABA_SYNC);
    aba.getRange(1, 1, 1, 3).setValues([['chave', 'seq', 'dados']]).setFontWeight('bold');
    aba.setFrozenRows(1);
    aba.hideSheet();
  }
  var ultima = Math.max(aba.getLastRow(), 1);
  var mapa = {};
  if (ultima > 1) {
    var valores = aba.getRange(2, 1, ultima - 1, 3).getValues();
    for (var i = 0; i < valores.length; i++) {
      var chave = String(valores[i][0]);
      if (chave === '') continue;
      var dados = {};
      try {
        var lido = JSON.parse(valores[i][2]);
        if (ehObjeto_(lido)) dados = lido;
      } catch (err) {
        dados = {};
      }
      mapa[chave] = { linha: i + 2, seq: Number(valores[i][1]) || 0, dados: dados };
    }
  }
  return { aba: aba, mapa: mapa, ultima: ultima };
}

function lerSeq_() {
  var v = PropertiesService.getScriptProperties().getProperty('JFLP_SEQ');
  return { valor: Number(v) || 0, sujo: false };
}

function proximoSeq_(contador) {
  contador.valor += 1;
  contador.sujo = true;
  return contador.valor;
}

function salvarSeq_(contador) {
  if (contador.sujo) PropertiesService.getScriptProperties().setProperty('JFLP_SEQ', String(contador.valor));
}

/** Grava as linhas alteradas de _sync (lista de chaves em estado.mapa): blocos contíguos para as existentes e um bloco para as novas. */
function gravarSync_(estado, alteradas) {
  var aba = estado.aba;
  var existentes = [];
  var novas = [];
  alteradas.forEach(function (chave) {
    var m = estado.mapa[chave];
    var linha = [chave, m.seq, JSON.stringify(m.dados)];
    if (m.linha) {
      existentes.push([m.linha, linha]);
    } else {
      novas.push(linha);
    }
  });
  existentes.sort(function (a, b) { return a[0] - b[0]; });
  var bloco = [];
  var inicio = 0;
  function descarregar() {
    if (bloco.length) aba.getRange(inicio, 1, bloco.length, 3).setValues(bloco);
    bloco = [];
  }
  existentes.forEach(function (p) {
    if (bloco.length && p[0] !== inicio + bloco.length) descarregar();
    if (!bloco.length) inicio = p[0];
    bloco.push(p[1]);
  });
  descarregar();
  if (novas.length) {
    var primeira = estado.ultima + 1;
    if (aba.getMaxRows() < estado.ultima + novas.length) {
      aba.insertRowsAfter(aba.getMaxRows(), estado.ultima + novas.length - aba.getMaxRows());
    }
    aba.getRange(primeira, 1, novas.length, 3).setValues(novas);
    var k = 0;
    alteradas.forEach(function (chave) {
      if (!estado.mapa[chave].linha) estado.mapa[chave].linha = primeira + k++;
    });
    estado.ultima += novas.length;
  }
}

/** Aplica o "sync" do envio de linhas; só mexe (e só incrementa seq) quando estado, revisao ou run_rel mudaram. */
function aplicarSync_(sync, maquina, nome) {
  var estado = lerSync_();
  var contador = lerSeq_();
  var alteradas = [];
  Object.keys(sync).forEach(function (chave) {
    var etapas = sync[chave];
    var m = estado.mapa[chave];
    var mudou = false;
    Object.keys(etapas).forEach(function (kind) {
      var novo = etapas[kind];
      if (!m) {
        m = estado.mapa[chave] = { linha: 0, seq: 0, dados: {} };
      }
      var atual = m.dados[kind];
      if (atual && atual.estado === novo.estado && atual.revisao === novo.revisao && atual.run_rel === novo.run_rel) {
        return;
      }
      // mesmo run_rel (só o estado mudou): o pacote já enviado continua valendo
      var pacote = atual && atual.run_rel === novo.run_rel && atual.pacote ? atual.pacote : null;
      m.dados[kind] = {
        estado: novo.estado, maquina: maquina, maquina_nome: nome,
        revisao: novo.revisao, run_rel: novo.run_rel, pacote: pacote
      };
      mudou = true;
    });
    if (mudou) {
      m.seq = proximoSeq_(contador);
      alteradas.push(chave);
    } else if (m && !m.linha && !Object.keys(m.dados).length) {
      delete estado.mapa[chave];   // chave sem etapas: não cria linha vazia
    }
  });
  if (alteradas.length) {
    gravarSync_(estado, alteradas);
    salvarSeq_(contador);
  }
}

// ---------------------------------------------------------------------------------------------
// Drive: pasta "JFLP pacotes"
// ---------------------------------------------------------------------------------------------

/** Pasta dos pacotes; id em JFLP_PASTA. Cria ao lado da pasta-mãe da planilha (ou na raiz) se faltar. */
function pasta_(criar) {
  var props = PropertiesService.getScriptProperties();
  var id = props.getProperty('JFLP_PASTA');
  if (id) {
    try {
      return DriveApp.getFolderById(id);
    } catch (err) {
      if (!criar) return null;
    }
  }
  if (!criar) return null;
  var pai = null;
  try {
    var pais = DriveApp.getFileById(SpreadsheetApp.getActiveSpreadsheet().getId()).getParents();
    if (pais.hasNext()) pai = pais.next();
  } catch (err) {
    pai = null;
  }
  if (!pai) pai = DriveApp.getRootFolder();
  var pasta = pai.createFolder(NOME_PASTA);
  props.setProperty('JFLP_PASTA', pasta.getId());
  return pasta;
}

// ---------------------------------------------------------------------------------------------
// ação "pacote"
// ---------------------------------------------------------------------------------------------

function acaoPacote_(c) {
  if (!ehTexto_(c.maquina) || !ehTexto_(c.chave) || !ehTexto_(c.kind) || typeof c.run_rel !== 'string') return formato_();
  if (typeof c.sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(c.sha256)) return formato_();
  if (!ehInteiro_(c.parte) || !ehInteiro_(c.partes) || c.partes < 1 || c.partes > MAX_PARTES) return formato_();
  if (c.parte < 0 || c.parte >= c.partes || typeof c.dados_b64 !== 'string') return formato_();
  var pasta = pasta_(true);
  var nome = c.sha256 + '.parte' + c.parte;
  var existentes = pasta.getFilesByName(nome);
  if (!existentes.hasNext()) {
    var bytes = Utilities.base64Decode(c.dados_b64);
    pasta.createFile(Utilities.newBlob(bytes, 'application/octet-stream', nome));
  }
  // todas as partes já presentes?
  var ids = [];
  var total = 0;
  for (var i = 0; i < c.partes; i++) {
    var it = pasta.getFilesByName(c.sha256 + '.parte' + i);
    if (!it.hasNext()) return { ok: true, completo: false };
    var arq = it.next();
    ids.push(arq.getId());
    total += arq.getSize();
  }
  var estado = lerSync_();
  var m = estado.mapa[c.chave];
  var d = m && m.dados[c.kind];
  if (!d || d.run_rel !== c.run_rel || d.maquina !== c.maquina) return { ok: true, completo: true, obsoleto: true };
  var p = d.pacote;
  if (p && p.sha256 === c.sha256 && p.partes === c.partes) return { ok: true, completo: true };   // reenvio idempotente
  d.pacote = { sha256: c.sha256, partes: c.partes, ids: ids, bytes: total };
  var contador = lerSeq_();
  m.seq = proximoSeq_(contador);
  gravarSync_(estado, [c.chave]);
  salvarSeq_(contador);
  return { ok: true, completo: true };
}

// ---------------------------------------------------------------------------------------------
// reservas vivas (propriedades r:<chave>)
// ---------------------------------------------------------------------------------------------

/** Todas as reservas vivas {chave: {maquina, maquina_nome, expira}}; as vencidas vão em "vencidas" (chaves). */
function lerReservas_() {
  var props = PropertiesService.getScriptProperties().getProperties();
  var t = agora_();
  var vivas = {};
  var vencidas = [];
  Object.keys(props).forEach(function (k) {
    if (k.indexOf('r:') !== 0) return;
    var r = null;
    try {
      r = JSON.parse(props[k]);
    } catch (err) {
      r = null;
    }
    if (!r || typeof r.expira !== 'number' || r.expira <= t) {
      vencidas.push(k);
    } else {
      vivas[k.substring(2)] = r;
    }
  });
  return { vivas: vivas, vencidas: vencidas, agora: t };
}

function infoReserva_(r, agora) {
  return { maquina: r.maquina, maquina_nome: r.maquina_nome || '', expira_em_s: Math.max(1, Math.ceil((r.expira - agora) / 1000)) };
}

function listaDeTextos_(v) {
  if (!Array.isArray(v)) return null;
  for (var i = 0; i < v.length; i++) if (!ehTexto_(v[i])) return null;
  return v;
}

function acaoReservar_(c) {
  var chaves = listaDeTextos_(c.chaves);
  if (!ehTexto_(c.maquina) || !chaves || typeof c.ttl_s !== 'number' || !isFinite(c.ttl_s) || c.ttl_s <= 0) {
    return formato_();
  }
  if (c.maquina_nome !== undefined && c.maquina_nome !== null && typeof c.maquina_nome !== 'string') return formato_();
  var ttl = Math.min(c.ttl_s, TTL_MAXIMO_S);
  var props = PropertiesService.getScriptProperties();
  var lidas = lerReservas_();
  lidas.vencidas.forEach(function (k) { props.deleteProperty(k); });
  var concedidas = [];
  var negadas = {};
  var novas = {};
  chaves.forEach(function (chave) {
    var r = lidas.vivas[chave];
    if (r && r.maquina !== c.maquina) {
      negadas[chave] = infoReserva_(r, lidas.agora);
    } else {
      concedidas.push(chave);
      novas['r:' + chave] = JSON.stringify({
        maquina: c.maquina, maquina_nome: c.maquina_nome || '', expira: lidas.agora + Math.round(ttl * 1000)
      });
    }
  });
  if (Object.keys(novas).length) props.setProperties(novas, false);
  return { ok: true, concedidas: concedidas, negadas: negadas };
}

function acaoLiberar_(c) {
  var chaves = listaDeTextos_(c.chaves);
  if (!ehTexto_(c.maquina) || !chaves) return formato_();
  var props = PropertiesService.getScriptProperties();
  var lidas = lerReservas_();
  var n = 0;
  chaves.forEach(function (chave) {
    var r = lidas.vivas[chave];
    if (r && r.maquina === c.maquina) {
      props.deleteProperty('r:' + chave);
      n++;
    }
  });
  return { ok: true, n: n };
}

// ---------------------------------------------------------------------------------------------
// ações "puxar" e "baixar" (só leitura)
// ---------------------------------------------------------------------------------------------

function acaoPuxar_(c) {
  if (!ehTexto_(c.maquina) || typeof c.cursor !== 'number' || !isFinite(c.cursor) || c.cursor < 0) return formato_();
  var limite = LIMITE_PADRAO;
  if (c.limite !== undefined && c.limite !== null) {
    if (!ehInteiro_(c.limite) || c.limite < 1) return formato_();
    limite = Math.min(c.limite, LIMITE_MAXIMO);
  }
  var estado = lerSync_();
  var lista = [];
  Object.keys(estado.mapa).forEach(function (chave) {
    var m = estado.mapa[chave];
    if (m.seq > c.cursor) lista.push({ chave: chave, seq: m.seq, dados: m.dados });
  });
  lista.sort(function (a, b) { return a.seq - b.seq; });
  var mais = lista.length > limite;
  var mudancas = lista.slice(0, limite);
  var cursor = mudancas.length ? mudancas[mudancas.length - 1].seq : c.cursor;
  var lidas = lerReservas_();
  var reservas = {};
  Object.keys(lidas.vivas).forEach(function (chave) {
    var r = lidas.vivas[chave];
    if (r.maquina !== c.maquina) reservas[chave] = infoReserva_(r, lidas.agora);
  });
  return { ok: true, cursor: cursor, mais: mais, mudancas: mudancas, reservas: reservas };
}

/** Devolve um arquivo da pasta de pacotes em base64; arquivos de fora da pasta são recusados. */
function acaoBaixar_(c) {
  if (!ehTexto_(c.id)) return formato_();
  var pasta = pasta_(false);
  if (!pasta) return { ok: false, erro: 'pasta' };
  var arq = null;
  try {
    arq = DriveApp.getFileById(c.id);
  } catch (err) {
    return { ok: false, erro: 'arquivo' };
  }
  var dentro = false;
  var pais = arq.getParents();
  while (pais.hasNext()) {
    if (pais.next().getId() === pasta.getId()) dentro = true;
  }
  if (!dentro) return { ok: false, erro: 'arquivo' };
  return { ok: true, dados_b64: Utilities.base64Encode(arq.getBlob().getBytes()) };
}

/** Cria a aba "Bugs" com o cabeçalho (Chave + colunas) se faltar; reescreve o cabeçalho se as colunas mudaram. */
function abaBugs_(colunas) {
  var planilha = SpreadsheetApp.getActiveSpreadsheet();
  var aba = planilha.getSheetByName(ABA_BUGS);
  var cabecalho = ['Chave'].concat(colunas);
  var mudou = false;
  if (!aba) {
    aba = planilha.insertSheet(ABA_BUGS);
    aba.setFrozenRows(1);
  }
  if (aba.getMaxColumns() < cabecalho.length) {
    aba.insertColumnsAfter(aba.getMaxColumns(), cabecalho.length - aba.getMaxColumns());
  }
  var atual = aba.getRange(1, 1, 1, cabecalho.length).getValues()[0];
  if (atual.join('\u0001') !== cabecalho.join('\u0001')) {
    // cabeçalho novo numa aba que já tinha dados: avisa o app (que pode reenviar tudo)
    mudou = aba.getLastRow() > 1;
    aba.getRange(1, 1, 1, cabecalho.length).setValues([cabecalho]).setFontWeight('bold');
  }
  return { aba: aba, mudou: mudou };
}

/** Textos que o Sheets leria como fórmula (começam com = + - @): a célula vira texto puro antes de gravar. */
function protegerTexto_(aba, linhaInicial, bloco) {
  var perigosas = [];
  for (var i = 0; i < bloco.length; i++) {
    for (var j = 0; j < bloco[i].length; j++) {
      var v = bloco[i][j];
      if (typeof v === 'string' && /^[=+\-@]/.test(v)) {
        perigosas.push(aba.getRange(linhaInicial + i, j + 1).getA1Notation());
      }
    }
  }
  for (var k = 0; k < perigosas.length; k += 100) {
    aba.getRangeList(perigosas.slice(k, k + 100)).setNumberFormat('@');
  }
}

function escrever_(aba, linhaInicial, bloco, largura) {
  protegerTexto_(aba, linhaInicial, bloco);
  aba.getRange(linhaInicial, 1, bloco.length, largura).setValues(bloco);
}

/** Upsert pela coluna A, um setValues por bloco de linhas contíguas. Devolve {n, projetos, cabecalho_mudou}. */
function gravar_(colunas, linhas) {
  var info = abaBugs_(colunas);
  var aba = info.aba;
  var largura = colunas.length + 1;
  var projetos = {};
  var iProj = colunas.indexOf('Projeto') + 1;   // posição na linha (a chave ocupa a posição 0)
  if (!linhas.length) return { n: 0, projetos: [], cabecalho_mudou: info.mudou };
  var ultima = aba.getLastRow();
  var posicao = {};
  if (ultima > 1) {
    var chaves = aba.getRange(2, 1, ultima - 1, 1).getValues();
    for (var i = 0; i < chaves.length; i++) posicao[chaves[i][0]] = i + 2;
  }
  var novas = [];
  var existentes = [];   // [linha da planilha, valores]
  linhas.forEach(function (l) {
    var valores = l.slice(0, largura);
    while (valores.length < largura) valores.push('');
    if (iProj > 0 && valores[iProj] !== '') projetos[valores[iProj]] = true;
    if (posicao[valores[0]]) {
      existentes.push([posicao[valores[0]], valores]);
    } else {
      posicao[valores[0]] = ultima + 1 + novas.length;
      novas.push(valores);
    }
  });
  existentes.sort(function (a, b) { return a[0] - b[0]; });
  var bloco = [];
  var inicio = 0;
  existentes.forEach(function (p) {
    if (bloco.length && p[0] !== inicio + bloco.length) {
      escrever_(aba, inicio, bloco, largura);
      bloco = [];
    }
    if (!bloco.length) inicio = p[0];
    bloco.push(p[1]);
  });
  if (bloco.length) escrever_(aba, inicio, bloco, largura);
  if (novas.length) {
    if (aba.getMaxRows() < ultima + novas.length) {
      aba.insertRowsAfter(aba.getMaxRows(), ultima + novas.length - aba.getMaxRows());
    }
    escrever_(aba, ultima + 1, novas, largura);
  }
  return { n: linhas.length, projetos: Object.keys(projetos), cabecalho_mudou: info.mudou };
}

/** Letra da coluna (1 = A) a partir do nome no cabeçalho da aba "Bugs"; '' se não existir. */
function letra_(cabecalho, nome) {
  var i = cabecalho.indexOf(nome);
  if (i < 0) return '';
  var n = i + 1, s = '';
  while (n > 0) {
    var r = (n - 1) % 26;
    s = String.fromCharCode(65 + r) + s;
    n = Math.floor((n - 1) / 26);
  }
  return s;
}

/**
 * Refaz a aba "Resumo" (uma linha por projeto, com COUNTIFS sobre a aba "Bugs"). As fórmulas se recalculam
 * sozinhas; por isso só reconstrói se a aba não existe, se apareceu projeto novo ou se o cabeçalho mudou.
 */
function atualizarResumo_(colunas, projetosRecebidos, cabecalhoMudou) {
  var planilha = SpreadsheetApp.getActiveSpreadsheet();
  var bugs = planilha.getSheetByName(ABA_BUGS);
  if (!bugs) return;
  var existente = planilha.getSheetByName(ABA_RESUMO);
  if (existente && !cabecalhoMudou) {
    var n = existente.getLastRow();
    var conhecidos = {};
    if (n > 1) {
      existente.getRange(2, 1, n - 1, 1).getValues().forEach(function (c) { conhecidos[c[0]] = true; });
    }
    var novo = (projetosRecebidos || []).some(function (p) { return !conhecidos[p]; });
    if (!novo) return;
  }
  var cabecalho = ['Chave'].concat(colunas);
  var cProj = letra_(cabecalho, 'Projeto'), cSit = letra_(cabecalho, 'Situação');
  var cV1 = letra_(cabecalho, 'Veredito J1'), cV2 = letra_(cabecalho, 'Veredito J2');
  if (!cProj || !cSit) return;
  var ultima = bugs.getLastRow();
  var projetos = [];
  if (ultima > 1) {
    var col = bugs.getRange(2, cabecalho.indexOf('Projeto') + 1, ultima - 1, 1).getValues();
    var vistos = {};
    col.forEach(function (c) {
      if (c[0] !== '' && !vistos[c[0]]) { vistos[c[0]] = true; projetos.push(c[0]); }
    });
    projetos.sort();
  }
  var aba = planilha.getSheetByName(ABA_RESUMO) || planilha.insertSheet(ABA_RESUMO);
  aba.clear();
  var titulos = ['Projeto', 'Bugs', 'Rodados', 'OK', 'Parcial', 'Falhou', 'Não executável',
                 'J1 ✓', 'J1 ~', 'J1 ✗', 'J2 ✓', 'J2 ~', 'J2 ✗'];
  aba.getRange(1, 1, 1, titulos.length).setValues([titulos]).setFontWeight('bold');
  aba.setFrozenRows(1);
  var inter = "Bugs!$" + cProj + ":$" + cProj;
  var sit = "Bugs!$" + cSit + ":$" + cSit;
  var v1 = cV1 ? "Bugs!$" + cV1 + ":$" + cV1 : null;
  var v2 = cV2 ? "Bugs!$" + cV2 + ":$" + cV2 : null;
  function contar(intervalo, valor, linha) {
    // no COUNTIFS o til escapa curinga: para achar um "~" literal é preciso escrever "~~"
    return '=COUNTIFS(' + inter + ',$A' + linha + ',' + intervalo + ',"' + valor.replace('~', '~~') + '*")';
  }
  var formulas = projetos.map(function (p, i) {
    var l = i + 2;
    return [
      p,
      '=COUNTIFS(' + inter + ',$A' + l + ')',
      '=SUM(D' + l + ':G' + l + ')',
      contar(sit, 'OK', l), contar(sit, 'parcial', l), contar(sit, 'falhou', l), contar(sit, 'não executável', l),
      v1 ? contar(v1, '✓', l) : '', v1 ? contar(v1, '~', l) : '', v1 ? contar(v1, '✗', l) : '',
      v2 ? contar(v2, '✓', l) : '', v2 ? contar(v2, '~', l) : '', v2 ? contar(v2, '✗', l) : ''
    ];
  });
  if (formulas.length) {
    aba.getRange(2, 1, formulas.length, titulos.length).setFormulas(formulas);
    var t = formulas.length + 2;
    var total = ['Total'];
    for (var c = 2; c <= titulos.length; c++) {
      var L = String.fromCharCode(64 + c);
      total.push('=SUM(' + L + '2:' + L + (t - 1) + ')');
    }
    aba.getRange(t, 1, 1, titulos.length).setFormulas([total]).setFontWeight('bold');
  }
}
