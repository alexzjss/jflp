/**
 * JFLP Studio - receptor da planilha do Google.
 *
 * Como instalar (leva uns 5 minutos):
 *  1. Abra a planilha vazia que vai receber os resultados.
 *  2. No menu, vá em Extensões > Apps Script e apague o conteúdo do arquivo Code.gs.
 *  3. Cole este arquivo inteiro no lugar e clique em Salvar.
 *  4. Em Configurações do projeto (engrenagem) > Propriedades do script, adicione a propriedade
 *     JFLP_TOKEN com o token mostrado na tela Ambiente do JFLP Studio.
 *  5. Clique em Implantar > Nova implantação > tipo "App da web". Em "Executar como" escolha
 *     "Eu" e em "Quem pode acessar" escolha "Qualquer pessoa". Autorize o acesso quando pedido.
 *  6. Copie a URL do app da web e cole na tela Ambiente do JFLP Studio, no campo da planilha.
 *
 * Depois de mudar este código, use Implantar > Gerenciar implantações > Editar > Nova versão.
 *
 * O JFLP Studio manda um POST com JSON {token, colunas, linhas}; cada linha começa pela chave do bug
 * (benchmark/projeto/ramo) e as linhas novas ou alteradas substituem a linha da mesma chave na aba "Bugs".
 * A aba "Resumo" é refeita com fórmulas que contam as linhas da aba "Bugs".
 */

var ABA_BUGS = 'Bugs';
var ABA_RESUMO = 'Resumo';

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
    if (!Array.isArray(corpo.colunas) || !Array.isArray(corpo.linhas)) {
      return resposta_({ ok: false, erro: 'formato' });
    }
    try {
      trava.waitLock(30000);
      travado = true;
    } catch (err) {
      return resposta_({ ok: false, erro: 'lock' });
    }
    var r = gravar_(corpo.colunas, corpo.linhas);
    atualizarResumo_(corpo.colunas, r.projetos, r.cabecalho_mudou);
    var saida = { ok: true, n: r.n };
    if (r.cabecalho_mudou) saida.cabecalho_mudou = true;
    return resposta_(saida);
  } catch (err) {
    return resposta_({ ok: false, erro: String(err) });
  } finally {
    if (travado) trava.releaseLock();
  }
}

function doGet() {
  return resposta_({ ok: true, mensagem: 'JFLP Studio: receptor da planilha no ar' });
}

function resposta_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
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
