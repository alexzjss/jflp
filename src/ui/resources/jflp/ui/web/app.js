/* JFLP UI — a single-page app over the local API. No dependencies. */
"use strict";

const S = { token: null, heuristics: [], workspace: "", timers: [], route: "" };

// ---------------------------------------------------------------- utilities

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const view = () => $("#view");

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (init.method !== "GET") init.headers["X-JFLP-Token"] = S.token;
  if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  const r = await fetch("/api/" + path, init);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

function toast(msg, bad) {
  const t = document.createElement("div");
  t.className = "toast" + (bad ? " bad" : "");
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), bad ? 7000 : 3500);
}

function every(ms, fn) {
  const id = setInterval(fn, ms);
  S.timers.push(id);
  return id;
}

function ago(iso) {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "agora";
  if (s < 3600) return Math.floor(s / 60) + " min atrás";
  if (s < 86400) return Math.floor(s / 3600) + " h atrás";
  return new Date(iso).toLocaleDateString("pt-BR");
}

const num = (n) => (n == null ? "—" : Number(n).toLocaleString("pt-BR"));
const susp = (n) => (n == null ? "—" : Number(n).toFixed(3));
const rank = (n) => (n == null ? "—" : Number.isInteger(n) ? String(n) : Number(n).toFixed(1));
const dur = (s) => (s == null ? "—" : s < 60 ? s.toFixed(1) + " s" : Math.floor(s / 60) + " min " + Math.round(s % 60) + " s");

function badge(status) {
  const m = {
    ready: ["ok", "pronto"], done: ["ok", "concluída"], acquiring: ["run", "obtendo fonte"], queued: ["run", "na fila"],
    running: ["run", "executando"], failed: ["bad", "falhou"], error: ["bad", "erro"], cancelled: ["warn", "cancelada"],
  }[status] || ["", status];
  return `<span class="badge ${m[0]}"><span class="dot"></span>${esc(m[1])}</span>`;
}

function heat(v, max) {
  if (!v || v <= 0) return "transparent";
  const x = Math.max(0.08, Math.min(1, max > 0 ? v / max : v));
  return `rgba(${Math.round(245 - 6 * x)}, ${Math.round(158 - 90 * x)}, ${Math.round(11 + 57 * x)}, ${0.18 + 0.62 * x})`;
}

const ICON = {
  home: '<path d="M3 11 12 4l9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
  git: '<circle cx="6" cy="6" r="2.5"/><circle cx="6" cy="18" r="2.5"/><circle cx="18" cy="9" r="2.5"/><path d="M6 8.5v7M18 11.5c0 4-6 3-11 5"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  bug: '<rect x="7" y="8" width="10" height="12" rx="5"/><path d="M12 8V5M9 5l1.5 2M15 5l-1.5 2M3 13h4M17 13h4M4 19l3-2M20 19l-3-2M4 8l3 2M20 8l-3 2"/>',
  jar: '<path d="M8 3h8M9 3v3l-3 4v9a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2v-9l-3-4V3"/><path d="M6 12h12"/>',
  bear: '<circle cx="7" cy="6" r="2.5"/><circle cx="17" cy="6" r="2.5"/><circle cx="12" cy="13" r="7"/><circle cx="12" cy="15" r="2"/>',
  play: '<path d="M7 4l13 8-13 8z"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M5 7l1 13h12l1-13M9 7V4h6v3"/>',
  dl: '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
};
const icon = (n) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${ICON[n]}</svg>`;

// ---------------------------------------------------------------- shell

function shell() {
  const r = S.route;
  const on = (p) => (r === p || (p !== "" && r.startsWith(p)) ? "on" : "");
  $("#nav").innerHTML = `
    <a href="#/" class="${r === "" ? "on" : ""}">${icon("home")} Painel</a>
    <a href="#/new" class="${on("new")}">${icon("plus")} Nova análise</a>
    <a href="#/settings" class="${on("settings")}">${icon("gear")} Ambiente</a>
    <a href="javascript:void 0" id="theme">${icon("sun")} Tema claro/escuro</a>`;
  $("#theme").onclick = () => {
    const light = document.documentElement.dataset.theme !== "light";
    document.documentElement.dataset.theme = light ? "light" : "dark";
    try { localStorage.setItem("jflp-theme", light ? "light" : "dark"); } catch (e) { /* private mode */ }
  };
  $("#foot").textContent = "workspace: " + S.workspace;
  api("projects").then((ps) => {
    $("#recent").innerHTML = ps.slice(0, 8).map((p) => `<a href="#/project/${p.id}">${esc(p.name)}</a>`).join("")
      || '<span class="faint" style="padding:6px 10px;display:block;font-size:12px">nenhum ainda</span>';
  }).catch(() => {});
}

async function route() {
  S.timers.forEach(clearInterval);
  S.timers = [];
  const gen = S.gen = (S.gen || 0) + 1;
  const h = location.hash.replace(/^#\/?/, "");
  const [path, query] = h.split("?");
  S.route = path;
  const q = Object.fromEntries(new URLSearchParams(query || ""));
  const p = path.split("/");
  shell();
  window.scrollTo(0, 0);
  try {
    if (p[0] === "" ) await dashboard();
    else if (p[0] === "new") await newSource(q.type || "git");
    else if (p[0] === "project") await project(p[1]);
    else if (p[0] === "run") await run(p[1], p[2] || "overview", q);
    else if (p[0] === "settings") await settings();
    else view().innerHTML = '<div class="empty">Página não encontrada.</div>';
  } catch (e) {
    // A page still loading when the user moved on fails on elements that are gone:
    // only the current page may show its error.
    if (gen !== S.gen) return;
    view().innerHTML = `<div class="card"><h3>Algo deu errado</h3><p class="muted">${esc(e.message)}</p></div>`;
  }
}

// ---------------------------------------------------------------- dashboard

const SOURCES = [
  { type: "git", icon: "git", name: "Repositório Git", hint: "qualquer URL clonável" },
  { type: "local", icon: "folder", name: "Pasta local", hint: "um projeto já no disco" },
  { type: "defects4j", icon: "bug", name: "Defects4J", hint: "17 projetos, 800+ bugs" },
  { type: "bugsjar", icon: "jar", name: "Bugs.jar", hint: "8 projetos Apache" },
  { type: "bears", icon: "bear", name: "Bears", hint: "bugs de CI reais" },
];

async function dashboard() {
  const [projects, runs] = await Promise.all([api("projects"), api("runs?limit=10")]);
  const done = runs.filter((r) => r.status === "done").length;
  const failingTests = runs.reduce((a, r) => a + ((r.summary && r.summary.failed) || 0), 0);
  view().innerHTML = `
    <div class="hero">
      <h2>Encontre a linha defeituosa a partir dos testes que falham</h2>
      <p>O JFLP mede a cobertura de cada teste com um agente Java e ordena o código pela suspeição
      (Ochiai e mais 9 heurísticas). Funciona em qualquer projeto Maven, Gradle, Ant ou Defects4J, sem mudar o build,
      e reproduz o Jaguar2 bit a bit quando você precisar.</p>
      <div class="row">
        <a class="btn primary" href="#/new">${icon("plus")} Nova análise</a>
        <a class="btn" href="#/settings">${icon("gear")} Verificar ambiente</a>
      </div>
    </div>
    <div class="grid g4" style="margin-bottom:20px">
      <div class="card kpi"><div class="l">Projetos</div><div class="v">${num(projects.length)}</div><div class="h">fontes no workspace</div></div>
      <div class="card kpi"><div class="l">Análises</div><div class="v">${num(runs.length)}</div><div class="h">${done} concluídas</div></div>
      <div class="card kpi bad"><div class="l">Testes falhos analisados</div><div class="v">${num(failingTests)}</div><div class="h">nas últimas análises</div></div>
      <div class="card kpi ok"><div class="l">Heurísticas</div><div class="v">${S.heuristics.length}</div><div class="h">Ochiai, Tarantula, Op, Zoltar…</div></div>
    </div>
    <h3 style="margin:8px 0 12px">Começar a partir de…</h3>
    <div class="sources">${SOURCES.map((s) => `
      <a class="src" href="#/new?type=${s.type}"><div class="ic">${icon(s.icon)}</div><b>${s.name}</b><small>${s.hint}</small></a>`).join("")}
    </div>
    <div class="grid g2">
      <div class="card">
        <h3>Análises recentes</h3>
        ${runs.length ? `<table class="t"><tr><th>Projeto</th><th>Status</th><th class="num">Testes</th><th class="num">Falhos</th><th class="num">Rank do bug</th><th>Quando</th></tr>
          ${runs.map((r) => `<tr class="click" onclick="location.hash='#/run/${r.id}'">
            <td><b>${esc(r.projectName)}</b></td><td>${badge(r.status)}</td>
            <td class="num">${num(r.summary && r.summary.tests)}</td><td class="num">${num(r.summary && r.summary.failed)}</td>
            <td class="num">${rank(r.summary && r.summary.bestRank)}</td><td class="faint">${ago(r.created)}</td></tr>`).join("")}</table>`
          : `<div class="empty">${icon("play")}<p>Nenhuma análise ainda. Adicione uma fonte para começar.</p></div>`}
      </div>
      <div class="card">
        <h3>Projetos</h3>
        ${projects.length ? `<table class="t"><tr><th>Nome</th><th>Fonte</th><th>Build</th><th>Status</th><th class="num">Análises</th></tr>
          ${projects.map((p) => `<tr class="click" onclick="location.hash='#/project/${p.id}'">
            <td><b>${esc(p.name)}</b></td><td><span class="badge accent">${esc(p.source.type)}</span></td>
            <td class="muted">${esc(p.build || "—")}</td><td>${badge(p.status)}</td><td class="num">${num(p.runs)}</td></tr>`).join("")}</table>`
          : `<div class="empty">${icon("folder")}<p>Sem projetos.</p></div>`}
      </div>
    </div>`;
}

// ---------------------------------------------------------------- new source

async function newSource(type) {
  view().innerHTML = `
    <div class="top"><div><div class="crumbs"><a href="#/">Painel</a> / nova análise</div>
      <h1>Nova análise</h1><div class="sub">Escolha de onde vem o código. O JFLP baixa, compila e prepara o projeto.</div></div></div>
    <div class="sources">${SOURCES.map((s) => `
      <a class="src ${s.type === type ? "on" : ""}" href="#/new?type=${s.type}"><div class="ic">${icon(s.icon)}</div><b>${s.name}</b><small>${s.hint}</small></a>`).join("")}
    </div>
    <div class="grid g3">
      <form class="card span2" id="srcform">${sourceFields(type)}
        <label class="f"><span>Nome do projeto (opcional)</span><input type="text" name="name" placeholder="gerado a partir da fonte"></label>
        <div class="row"><button class="btn primary" type="submit">${icon("plus")} Adicionar e preparar</button><span class="faint" id="hint"></span></div>
      </form>
      <div class="card"><h3>Como funciona</h3>
        <ol class="muted" style="padding-left:18px;margin:0;line-height:1.9">
          <li>A fonte é obtida (clone, checkout do benchmark ou pasta local).</li>
          <li>O build é detectado: Maven, Gradle, Ant, Defects4J ou comandos seus.</li>
          <li>Os testes rodam com o agente via <code>JAVA_TOOL_OPTIONS</code>; nenhum arquivo do projeto muda.</li>
          <li>Você explora o ranking, o código colorido pela suspeição e, nos benchmarks, onde está o bug real.</li>
        </ol></div>
    </div>`;
  await fillCatalogs(type);
  $("#srcform").onsubmit = async (e) => {
    e.preventDefault();
    const fd = Object.fromEntries(new FormData(e.target).entries());
    const source = { type };
    for (const k of Object.keys(fd)) if (k !== "name" && fd[k] !== "") source[k] = fd[k];
    try {
      const p = await api("projects", { method: "POST", body: { name: fd.name, source } });
      location.hash = "#/project/" + p.id;
    } catch (err) { toast(err.message, true); }
  };
}

function sourceFields(type) {
  switch (type) {
    case "git": return `<h3>Repositório Git</h3>
      <label class="f"><span>URL</span><input type="text" name="url" required placeholder="https://github.com/jhy/jsoup"></label>
      <div class="grid g2">
        <label class="f"><span>Ref (branch, tag ou commit)</span><input type="text" name="ref" placeholder="padrão do repositório"></label>
        <label class="f"><span>Módulo (opcional)</span><input type="text" name="module" placeholder="ex.: core"></label>
      </div>
      <label class="f"><span>Ref da correção (opcional)</span><input type="text" name="fixRef" placeholder="commit que corrige o bug">
        <em>Com ela, o JFLP marca as linhas que a correção mudou e mostra em que posição do ranking o bug real ficou.</em></label>`;
    case "local": return `<h3>Pasta local</h3>
      <label class="f"><span>Caminho absoluto</span><input type="text" name="path" required placeholder="/home/voce/projetos/meu-app">
        <em>A pasta é usada no lugar: o build grava em target/ ou build/ dela.</em></label>`;
    case "defects4j": return `<h3>Defects4J</h3>
      <div class="grid g2">
        <label class="f"><span>Projeto</span><select name="project" id="d4jp"><option>carregando…</option></select></label>
        <label class="f"><span>Bug</span><select name="bug" id="d4jb"></select></label>
      </div><em class="faint">Precisa do Defects4J instalado (configure em Ambiente). A versão bugada é obtida com <code>defects4j checkout</code>.</em>`;
    case "bugsjar": return `<h3>Bugs.jar</h3>
      <div class="grid g2">
        <label class="f"><span>Projeto</span><select name="project" id="bjp"></select></label>
        <label class="f"><span>Issue</span><select name="issue" id="bji"><option>escolha um projeto</option></select></label>
      </div><em class="faint">Cada bug é um branch de github.com/bugs-dot-jar; a lista vem do GitHub.</em>`;
    case "bears": return `<h3>Bears</h3>
      <label class="f"><span>Bug</span><select name="bug" id="bears"><option>carregando o índice do Bears…</option></select></label>
      <em class="faint">Bugs reais de builds de CI; a versão defeituosa e o patch vêm do branch do bug.</em>`;
    default: return "";
  }
}

async function fillCatalogs(type) {
  const opts = (sel, items, label = (x) => x, value = (x) => x) => {
    $(sel).innerHTML = items.map((x) => `<option value="${esc(value(x))}">${esc(label(x))}</option>`).join("");
  };
  try {
    if (type === "defects4j") {
      const ps = await api("catalog/defects4j/projects");
      opts("#d4jp", ps);
      const load = async () => { $("#d4jb").innerHTML = "<option>carregando…</option>"; opts("#d4jb", await api("catalog/defects4j/bugs?project=" + encodeURIComponent($("#d4jp").value))); };
      $("#d4jp").onchange = load;
      if (ps.includes("Lang")) $("#d4jp").value = "Lang";
      await load();
    } else if (type === "bugsjar") {
      opts("#bjp", await api("catalog/bugsjar/projects"));
      const load = async () => {
        $("#bji").innerHTML = "<option>consultando o GitHub…</option>";
        opts("#bji", await api("catalog/bugsjar/bugs?project=" + encodeURIComponent($("#bjp").value)));
      };
      $("#bjp").onchange = load;
      $("#bjp").value = "commons-math";
      await load();
    } else if (type === "bears") {
      opts("#bears", await api("catalog/bears/bugs"), (b) => `${b.id} — ${b.project}`, (b) => b.id);
    }
  } catch (e) {
    $("#hint").textContent = e.message;
  }
}

// ---------------------------------------------------------------- project

async function project(id) {
  const [p, jdks] = await Promise.all([api("projects/" + id), api("jdks")]);
  const env = { jdks };
  const src = p.source || {};
  const where = src.url || src.path || [src.project, src.bug || src.issue].filter(Boolean).join(" ");
  view().innerHTML = `
    <div class="top"><div><div class="crumbs"><a href="#/">Painel</a> / projeto</div>
      <h1>${esc(p.name)} ${badge(p.status)}</h1>
      <div class="sub"><span class="badge accent">${esc(src.type)}</span> <span class="mono">${esc(where)}</span>
      ${p.commit ? ` · commit <code>${esc(p.commit)}</code>` : ""}</div></div>
      <div class="grow"></div>
      <button class="btn sm" id="refetch">Obter de novo</button>
      <button class="btn sm danger" id="del">${icon("trash")} Remover</button></div>
    <div class="grid g3">
      <div class="card span2" id="runbox"></div>
      <div class="card"><h3>Projeto</h3>
        <table class="t">
          <tr><td class="faint">Build</td><td>${esc(p.build || "—")}</td></tr>
          <tr><td class="faint">Pasta</td><td class="mono" style="word-break:break-all">${esc(p.dir || "—")}</td></tr>
          <tr><td class="faint">Módulo</td><td>${esc(p.module || "raiz")}</td></tr>
          <tr><td class="faint">Bug conhecido</td><td>${p.truth ? '<span class="badge ok">patch da correção</span>' : '<span class="faint">não</span>'}</td></tr>
          <tr><td class="faint">Falhos esperados</td><td>${p.expectedFailing ? p.expectedFailing.map((t) => `<div class="mono">${esc(t)}</div>`).join("") : '<span class="faint">—</span>'}</td></tr>
        </table></div>
    </div>
    <div class="card" style="margin-top:16px"><h3>Análises</h3>
      ${p.runList.length ? `<table class="t"><tr><th>Quando</th><th>Status</th><th>Opções</th><th class="num">Testes</th><th class="num">Falhos</th><th class="num">Rank do bug</th><th class="num">Duração</th></tr>
        ${p.runList.map((r) => `<tr class="click" onclick="location.hash='#/run/${r.id}'"><td>${ago(r.created)}</td><td>${badge(r.status)}</td>
          <td class="muted">${esc(optsText(r.options))}</td><td class="num">${num(r.summary && r.summary.tests)}</td>
          <td class="num">${num(r.summary && r.summary.failed)}</td><td class="num">${rank(r.summary && r.summary.bestRank)}</td>
          <td class="num">${dur(r.seconds)}</td></tr>`).join("")}</table>` : '<div class="empty">Nenhuma análise deste projeto.</div>'}
    </div>`;
  $("#del").onclick = async () => {
    if (!confirm("Remover o projeto e suas análises do workspace?")) return;
    await api("projects/" + id, { method: "DELETE" });
    location.hash = "#/";
  };
  $("#refetch").onclick = async () => { await api("projects/" + id + "/refetch", { method: "POST" }); route(); };

  if (p.status === "ready") {
    runForm(p, env);
  } else {
    $("#runbox").innerHTML = `<h3>${p.status === "error" ? "Não foi possível preparar a fonte" : '<span class="spinner"></span> Preparando a fonte'}</h3>
      ${p.error ? `<p class="muted">${esc(p.error)}</p>` : ""}<pre class="log" id="alog"></pre>`;
    const poll = async () => {
      const l = await api("projects/" + id + "/log");
      $("#alog").innerHTML = colorLog(l.text);
      $("#alog").scrollTop = 1e9;
      if (l.status !== "acquiring") route();
    };
    await poll();
    if (p.status === "acquiring") every(1500, poll);
  }
}

function optsText(o) {
  if (!o) return "";
  return [o.heuristic || "ochiai", o.lines === "jacoco" ? "linhas JaCoCo" : null, o.branches ? "ramos" : null, o.duas ? "DUAs" : null]
    .filter(Boolean).join(" · ");
}

function runForm(p, env) {
  const jdks = env.jdks || [];
  const d4j = p.build === "defects4j";
  const pick = jdks.find((j) => j.major === 11) || jdks[0];
  $("#runbox").innerHTML = `
    <h3>Analisar <small>os testes rodam com o agente; o projeto não é alterado</small></h3>
    <form id="runform">
      <div class="grid g2">
        <label class="f"><span>Heurística</span><select name="heuristic">${S.heuristics.map((h) => `<option ${h === "ochiai" ? "selected" : ""}>${h}</option>`).join("")}</select>
          <em>Ochiai é a padrão do Jaguar2; dá para trocar depois sem rodar de novo.</em></label>
        <label class="f"><span>JDK dos testes</span><select name="jdk">${jdks.map((j) => `<option value="${esc(j.path)}" ${pick && j.path === pick.path ? "selected" : ""}>Java ${j.major} — ${esc(j.version)}</option>`).join("")}</select>
          <em>${d4j ? "O Defects4J exige o JDK 11." : "Projetos antigos (source 1.5/1.6) compilam só até o JDK 8."}</em></label>
      </div>
      <label class="f"><span>Semântica de linha</span>
        <div class="seg" id="seg"><button type="button" data-v="entry" class="on">JFLP (linha alcançada)</button><button type="button" data-v="jacoco">JaCoCo (igual ao Jaguar2)</button></div>
        <em>No modo JFLP a linha que lança a exceção conta como coberta — em geral é a linha do bug.</em></label>
      <div class="grid g2">
        <label class="switch"><input type="checkbox" name="branches"><span><b>Ramos</b><small>cada aresta de if/switch vira elemento</small></span></label>
        <label class="switch"><input type="checkbox" name="duas"><span><b>Fluxo de dados (DUAs)</b><small>associações definição-uso, como o BA-DUA</small></span></label>
      </div>
      <details class="adv"><summary>Opções avançadas</summary>
        <div class="grid g2">
          <label class="f"><span>Módulo</span><input type="text" name="module" value="${esc(p.module || "")}" placeholder="raiz"></label>
          <label class="f"><span>Tempo limite (min)</span><input type="number" name="timeoutMinutes" value="120" min="1"></label>
        </div>
        <label class="f"><span>Comando de build (opcional)</span><input type="text" name="buildCommand" placeholder="padrão para ${esc(p.build)}"></label>
        <label class="f"><span>Comando de teste (opcional)</span><input type="text" name="testCommand" placeholder="padrão para ${esc(p.build)}"></label>
        <label class="f"><span>Pastas de classes (opcional)</span><input type="text" name="classDirs" placeholder="detectadas: target/classes, build/classes/java/main…">
          <em>Separe por vírgula; relativas à pasta do projeto.</em></label>
      </details>
      <button class="btn primary" type="submit">${icon("play")} Rodar análise</button>
    </form>`;
  let lines = "entry";
  $$("#seg button").forEach((b) => b.onclick = () => { lines = b.dataset.v; $$("#seg button").forEach((x) => x.classList.toggle("on", x === b)); });
  $("#runform").onsubmit = async (e) => {
    e.preventDefault();
    const f = e.target;
    const body = {
      heuristic: f.heuristic.value, jdk: f.jdk.value, lines, branches: f.branches.checked, duas: f.duas.checked,
      module: f.module.value, timeoutMinutes: Number(f.timeoutMinutes.value) || 120,
      buildCommand: f.buildCommand.value, testCommand: f.testCommand.value, classDirs: f.classDirs.value,
    };
    if (lines === "jacoco" && (body.branches || body.duas)) { toast("Ramos e DUAs precisam da semântica JFLP.", true); return; }
    try {
      const r = await api("projects/" + p.id + "/runs", { method: "POST", body });
      location.hash = "#/run/" + r.id;
    } catch (err) { toast(err.message, true); }
  };
}

// ---------------------------------------------------------------- run

const STEP_NAMES = { prepare: "Preparar", build: "Compilar", classes: "Classes", test: "Testes + agente", report: "Relatório", analyze: "Análise" };

async function run(id, tab, q) {
  const r = await api("runs/" + id);
  const header = () => `
    <div class="top"><div><div class="crumbs"><a href="#/">Painel</a> / <a href="#/project/${r.projectId}">${esc(r.projectName)}</a> / análise</div>
      <h1>${esc(r.projectName)} ${badge(r.status)}</h1>
      <div class="sub">${esc(optsText(r.options))} · iniciada ${ago(r.created)}${r.seconds ? " · " + dur(r.seconds) : ""}</div></div>
      <div class="grow"></div>
      ${r.status === "running" || r.status === "queued" ? `<button class="btn sm danger" id="cancel">${icon("stop")} Cancelar</button>` : ""}
      <a class="btn sm" href="#/project/${r.projectId}">Nova análise</a></div>
    <div class="steps" id="steps">${r.steps.map((s) => `<div class="step ${s.status}"><div class="n">${esc(s.status === "pending" ? "aguardando" : s.status === "running" ? "em andamento" : s.status === "done" ? "ok" : s.status)}</div>
      <b>${STEP_NAMES[s.id] || s.id}</b><small title="${esc(s.detail || "")}">${esc(s.detail || " ")}</small></div>`).join("")}</div>`;

  if (r.status !== "done") {
    view().innerHTML = header() + `
      ${r.error ? `<div class="card" style="margin-top:16px;border-color:var(--bad)"><b>${esc(r.error)}</b></div>` : ""}
      <div class="card" style="margin-top:16px"><h3>Log ${r.status === "running" ? '<span class="spinner"></span>' : ""}</h3><pre class="log" id="log"></pre></div>`;
    if ($("#cancel")) $("#cancel").onclick = () => api("runs/" + id + "/cancel", { method: "POST" });
    let offset = 0;
    const poll = async () => {
      const l = await api("runs/" + id + "/log?offset=" + offset);
      if (l.text) {
        $("#log").insertAdjacentHTML("beforeend", colorLog(l.text));
        $("#log").scrollTop = 1e9;
      }
      offset = l.offset;
      const fresh = await api("runs/" + id);
      if (fresh.status !== r.status || JSON.stringify(fresh.steps) !== JSON.stringify(r.steps)) {
        if (fresh.status !== r.status && !["queued", "running"].includes(fresh.status)) { route(); return; }
        Object.assign(r, fresh);
        $("#steps").outerHTML = header().match(/<div class="steps"[\s\S]*$/)[0];
      }
    };
    await poll();
    if (r.status === "running" || r.status === "queued") every(1200, poll);
    return;
  }

  const sum = await api("runs/" + id + "/summary");
  const tabs = [["overview", "Visão geral"], ["ranking", "Ranking"], ["code", "Código"], ["tests", "Testes", sum.tests],
    ["branches", "Ramos", sum.branches], ["duas", "DUAs", sum.duas], ["files", "Arquivos"], ["log", "Log"]]
    .filter((t) => !(t[0] === "branches" && sum.branches == null) && !(t[0] === "duas" && sum.duas == null));
  view().innerHTML = header() + `<div class="tabs">${tabs.map((t) => `<a href="#/run/${id}/${t[0]}" class="${t[0] === tab ? "on" : ""}">${t[1]}${t[2] != null ? `<span class="badge">${num(t[2])}</span>` : ""}</a>`).join("")}</div><div id="tab"></div>`;
  const T = { overview, ranking, code, tests, branches: elements, duas: elements, files, log: runLog }[tab] || overview;
  await T(id, sum, q, tab);
}

function colorLog(text) {
  return esc(text).split("\n").map((l) =>
    /ERROR|FAIL|Exception/.test(l) ? `<span class="e">${l}</span>` : /WARN/.test(l) ? `<span class="w">${l}</span>` : /^\$ /.test(l) ? `<span class="c">${l}</span>` : l).join("\n");
}

function histogram(bins) {
  const max = Math.max(1, ...bins);
  const w = 34, gap = 8, h = 120;
  return `<svg class="hist" viewBox="0 0 ${bins.length * (w + gap)} ${h + 22}" width="100%" height="160" preserveAspectRatio="none">
    <defs><linearGradient id="hg" x1="0" y1="1" x2="0" y2="0"><stop offset="0" stop-color="#f59e0b"/><stop offset="1" stop-color="#ef4444"/></linearGradient></defs>
    ${bins.map((b, i) => { const bh = Math.max(b ? 3 : 0, (b / max) * h); return `<rect class="b" x="${i * (w + gap)}" y="${h - bh}" width="${w}" height="${bh}" rx="4"><title>${b} linhas</title></rect>
      <text x="${i * (w + gap) + w / 2}" y="${h + 15}" text-anchor="middle">${(i / 10).toFixed(1)}</text>`; }).join("")}</svg>`;
}

async function overview(id, s) {
  const t = s.truth;
  const top = await api("runs/" + id + "/ranking?limit=8");
  const max = top.rows.length ? top.rows[0].susp : 1;
  $("#tab").innerHTML = `
    <div class="grid g5" style="margin-bottom:16px">
      <div class="card kpi"><div class="l">Testes</div><div class="v">${num(s.tests)}</div><div class="h">${num(s.passed)} passaram</div></div>
      <div class="card kpi bad"><div class="l">Falharam</div><div class="v">${num(s.failed)}</div><div class="h">a evidência do defeito</div></div>
      <div class="card kpi"><div class="l">Linhas de código</div><div class="v">${num(s.lines)}</div><div class="h">${num(s.suspicious)} suspeitas</div></div>
      <div class="card kpi"><div class="l">Maior suspeição</div><div class="v">${susp(s.topSusp)}</div><div class="h">${esc(s.heuristic)}</div></div>
      ${t ? `<div class="card kpi ok"><div class="l">Bug real no ranking</div><div class="v">${t.bestRank == null ? "—" : "#" + rank(t.bestRank)}</div>
        <div class="h">${t.exam == null ? "linhas do bug sem código" : "EXAM " + (t.exam * 100).toFixed(2) + "% do código"}</div></div>`
        : `<div class="card kpi"><div class="l">Ramos / DUAs</div><div class="v">${num(s.branches)} / ${num(s.duas)}</div><div class="h">elementos extras</div></div>`}
    </div>
    <div class="grid g2">
      <div class="card"><h3>Mais suspeitas <small><a href="#/run/${id}/ranking">ver ranking completo →</a></small></h3>
        <table class="t"><tr><th class="num">#</th><th>Linha</th><th>Suspeição</th><th class="num">cef</th><th class="num">cep</th></tr>
        ${top.rows.map((r) => rankRow(id, r, max)).join("")}</table></div>
      <div class="card"><h3>Distribuição da suspeição <small>linhas com suspeição &gt; 0</small></h3>${histogram(s.histogram)}
        <h3 style="margin-top:14px">Testes que falharam</h3>
        ${s.failing.map((f) => `<div class="mono" style="padding:3px 0">✕ ${esc(f)}</div>`).join("") || '<p class="muted">Nenhum: sem falhas não há suspeição.</p>'}
        ${s.expectedMissing && s.expectedMissing.length ? `<p class="badge warn" style="margin-top:8px">esperados e não vistos: ${esc(s.expectedMissing.join(", "))}</p>` : ""}
        ${s.expectedFailing && !(s.expectedMissing || []).length ? '<p><span class="badge ok">todos os testes que o benchmark aponta falharam</span></p>' : ""}
      </div>
    </div>
    ${s.warnings.length ? `<div class="card" style="margin-top:16px"><h3>Avisos</h3>${s.warnings.map((w) => `<div class="muted">⚠ ${esc(w)}</div>`).join("")}</div>` : ""}`;
}

function rankRow(id, r, max) {
  const cls = r["class"];
  const simple = cls.slice(cls.lastIndexOf(".") + 1);
  return `<tr class="click ${r.buggy ? "buggy" : ""}" onclick="location.hash='#/run/${id}/code?file=${encodeURIComponent(r.source)}&line=${r.line}'">
    <td class="num">${rank(r.rank)}</td>
    <td><span class="cls">${esc(cls.slice(0, cls.length - simple.length)).replace(/\./g, ".<wbr>")}<b>${esc(simple)}</b>:${r.line}</span>${r.buggy ? ' <span class="badge bad">bug</span>' : ""}</td>
    <td><div class="row" style="gap:8px;flex-wrap:nowrap"><div class="bar" style="flex:1"><i style="width:${max > 0 && r.susp > 0 ? Math.max(2, (r.susp / max) * 100) : 0}%"></i></div><span class="mono" style="white-space:nowrap">${susp(r.susp)}</span></div></td>
    <td class="num">${r.cef}</td><td class="num">${r.cep}</td></tr>`;
}

async function ranking(id, s, q) {
  let offset = 0;
  const limit = 50;
  $("#tab").innerHTML = `<div class="card">
    <div class="row" style="margin-bottom:12px">
      <label class="f" style="margin:0;width:220px"><span>Heurística</span><select id="h">${S.heuristics.map((h) => `<option ${h === s.heuristic ? "selected" : ""}>${h}</option>`).join("")}</select></label>
      <label class="f" style="margin:0;flex:1"><span>Filtrar por classe</span><input type="text" id="flt" placeholder="ex.: NumberUtils"></label>
      <div class="sp"></div><div class="legend" id="lg"></div>
    </div><div id="rk"></div></div>`;
  const load = async () => {
    const d = await api(`runs/${id}/ranking?h=${$("#h").value}&offset=${offset}&limit=${limit}&filter=${encodeURIComponent($("#flt").value)}`);
    const max = d.rows.length ? Math.max(...d.rows.map((r) => r.susp), 0) : 1;
    $("#lg").innerHTML = d.truth ? '<span><span class="badge bad">bug</span> linha alterada pela correção</span>' : "";
    $("#rk").innerHTML = `
      ${d.truth && d.truth.bestRank != null ? `<p><span class="badge ok">bug real na posição #${rank(d.truth.bestRank)} com ${esc(d.heuristic)}</span></p>` : ""}
      <table class="t"><tr><th class="num">Rank</th><th>Linha</th><th>Suspeição</th><th class="num">cef</th><th class="num">cep</th></tr>
      ${d.rows.map((r) => rankRow(id, r, max)).join("")}</table>
      <div class="row" style="margin-top:12px"><span class="faint">${num(offset + 1)}–${num(Math.min(offset + limit, d.total))} de ${num(d.total)} linhas</span><div class="sp"></div>
      <button class="btn sm" id="prev" ${offset === 0 ? "disabled" : ""}>Anterior</button><button class="btn sm" id="next" ${offset + limit >= d.total ? "disabled" : ""}>Próxima</button></div>`;
    $("#prev").onclick = () => { offset = Math.max(0, offset - limit); load(); };
    $("#next").onclick = () => { offset += limit; load(); };
  };
  $("#h").onchange = () => { offset = 0; load(); };
  let t;
  $("#flt").oninput = () => { clearTimeout(t); t = setTimeout(() => { offset = 0; load(); }, 250); };
  await load();
}

async function code(id, s, q) {
  const fs = await api("runs/" + id + "/files");
  const file = q.file || (fs[0] && fs[0].source);
  const maxAll = Math.max(...fs.map((f) => f.maxSusp), 0);
  $("#tab").innerHTML = `<div class="codewrap">
    <div class="card files" style="padding:10px"><input type="text" id="ff" placeholder="filtrar arquivos" style="margin-bottom:8px">
      <div id="fl">${fs.map((f) => `<a href="#/run/${id}/code?file=${encodeURIComponent(f.source)}" class="${f.source === file ? "on" : ""}" data-n="${esc(f.source.toLowerCase())}">
        <span class="heatdot" style="background:${heat(f.maxSusp, maxAll) === "transparent" ? "var(--line)" : heat(f.maxSusp, maxAll)}"></span>
        <span class="nm">${esc(f.source)}</span>${f.buggy ? '<span class="badge bad">bug</span>' : ""}</a>`).join("")}</div></div>
    <div><div class="row" style="margin-bottom:10px"><b class="mono" id="fname">${esc(file || "")}</b><div class="sp"></div>
      <div class="legend"><span class="scale"></span><span>suspeição</span><span><i style="background:var(--bad)"></i>linha do bug</span><span><i style="background:var(--line)"></i>código não executado por teste falho</span></div></div>
      <div class="code" id="code"><div class="empty"><span class="spinner"></span></div></div></div></div>`;
  $("#ff").oninput = () => $$("#fl a").forEach((a) => a.style.display = a.dataset.n.includes($("#ff").value.toLowerCase()) ? "" : "none");
  if (!file) { $("#code").innerHTML = '<div class="empty">Sem arquivos.</div>'; return; }
  const src = await api(`runs/${id}/source?file=${encodeURIComponent(file)}`);
  const max = Math.max(...src.lines.map((l) => l.susp || 0), 0) || maxAll;
  const focus = Number(q.line || 0);
  $("#code").innerHTML = src.path == null && !src.lines.length ? '<div class="empty">Arquivo-fonte não encontrado no checkout.</div>' :
    `<table>${src.lines.map((l) => {
      const has = l.susp !== undefined;
      const c = has ? heat(l.susp, max) : "transparent";
      const cls = [has && l.susp > 0 ? "hit" : "", l.buggy ? "bug" : "", l.n === focus ? "focus" : "", has && l.susp === 0 ? "nocov" : ""].join(" ");
      const tip = has ? `suspeição ${susp(l.susp)} · rank ${rank(l.rank)} · cef ${l.cef} · cep ${l.cep}` : "";
      return `<tr id="L${l.n}" class="${cls}" style="--heat:${c}" title="${esc(tip)}"><td class="ln">${l.n}</td><td class="sc">${has && l.susp > 0 ? susp(l.susp) : ""}</td><td class="tx" style="background:${c}">${esc(l.text) || " "}</td></tr>`;
    }).join("")}</table>`;
  const target = focus ? $("#L" + focus) : $("#code tr.hit");
  if (target) target.scrollIntoView({ block: "center" });
}

async function tests(id) {
  const ts = await api("runs/" + id + "/tests");
  $("#tab").innerHTML = `<div class="card"><div class="row" style="margin-bottom:10px"><div class="seg" id="tf"><button class="on" data-v="all">Todos</button><button data-v="FAIL">Falharam</button><button data-v="PASS">Passaram</button></div>
    <div class="sp"></div><span class="faint">ordem de execução</span></div>
    <table class="t" id="tt"><tr><th class="num">#</th><th>Resultado</th><th>Teste</th></tr>
    ${ts.map((t) => `<tr data-o="${t.outcome}"><td class="num">${t.row + 1}</td><td>${t.outcome === "FAIL" ? '<span class="badge bad">falhou</span>' : '<span class="badge ok">passou</span>'}</td><td class="mono">${esc(t.name)}</td></tr>`).join("")}</table></div>`;
  $$("#tf button").forEach((b) => b.onclick = () => {
    $$("#tf button").forEach((x) => x.classList.toggle("on", x === b));
    $$("#tt tr[data-o]").forEach((r) => r.style.display = b.dataset.v === "all" || r.dataset.o === b.dataset.v ? "" : "none");
  });
}

async function elements(id, s, q, kind) {
  const d = await api(`runs/${id}/elements?kind=${kind}&limit=300`);
  if (!d.available) { $("#tab").innerHTML = '<div class="card empty">Esta análise não mediu esses elementos.</div>'; return; }
  const max = d.rows.length ? d.rows[0].susp : 1;
  const bar = (v) => `<div class="row" style="gap:8px;flex-wrap:nowrap"><div class="bar" style="flex:1"><i style="width:${max > 0 && v > 0 ? Math.max(2, v / max * 100) : 0}%"></i></div><span class="mono">${susp(v)}</span></div>`;
  $("#tab").innerHTML = `<div class="card"><h3>${kind === "branches" ? "Ramos mais suspeitos" : "Associações definição-uso mais suspeitas"} <small>${num(d.total)} com suspeição &gt; 0</small></h3>
    <table class="t">${kind === "branches"
      ? `<tr><th>Classe</th><th class="num">Linha</th><th>Ramo</th><th>Suspeição</th><th class="num">cef</th><th class="num">cep</th></tr>${d.rows.map((r) => `<tr><td class="mono">${esc(r["class"])}</td><td class="num">${r.line}</td><td><span class="badge">${esc(r.branch)}</span></td><td>${bar(r.susp)}</td><td class="num">${r.cef}</td><td class="num">${r.cep}</td></tr>`).join("")}`
      : `<tr><th>Classe</th><th>Método</th><th>Variável</th><th class="num">Def</th><th class="num">Uso</th><th class="num">Alvo</th><th>Suspeição</th><th class="num">cef</th><th class="num">cep</th></tr>${d.rows.map((r) => `<tr><td class="mono">${esc(r["class"])}</td><td class="mono faint">${esc(r.method.split("(")[0])}</td><td><b>${esc(r.variable)}</b></td><td class="num">${r.def}</td><td class="num">${r.use}</td><td class="num">${r.target == null ? "c-use" : r.target}</td><td>${bar(r.susp)}</td><td class="num">${r.cef}</td><td class="num">${r.cep}</td></tr>`).join("")}`}
    </table></div>`;
}

async function files(id) {
  const fs = await api("runs/" + id + "/artifacts");
  const what = {
    "jaguar2.csv": "formato do CsvExporter do Jaguar2", "jaguar2.xml": "formato do XmlExporter do Jaguar2",
    "coverageMatrix.txt": "matriz do fork do Jaguar2", "coverageMatrix.columns.csv": "classe e linha de cada coluna",
    "coverageMatrix.tests.csv": "teste de cada linha da matriz", "spectra.csv": "todas as linhas de código, suspeição 0 inclusa",
    "branches.csv": "espectro por ramo", "duas.csv": "espectro por associação definição-uso", "jflp-summary.txt": "contagens e avisos",
  };
  $("#tab").innerHTML = `<div class="card"><h3>Arquivos do relatório</h3><table class="t"><tr><th>Arquivo</th><th>Conteúdo</th><th class="num">Tamanho</th><th></th></tr>
    ${fs.map((f) => `<tr><td class="mono">${esc(f.name)}</td><td class="muted">${esc(what[f.name] || "")}</td><td class="num">${(f.bytes / 1024).toFixed(1)} KB</td>
      <td><a class="btn sm" href="/api/runs/${id}/download/${encodeURIComponent(f.name)}">${icon("dl")} baixar</a></td></tr>`).join("")}</table></div>`;
}

async function runLog(id) {
  const l = await api("runs/" + id + "/log?offset=0");
  $("#tab").innerHTML = `<div class="card"><pre class="log" style="height:620px">${colorLog(l.text)}</pre></div>`;
}

// ---------------------------------------------------------------- settings

async function settings() {
  const env = await api("env");
  const st = env.settings || {};
  view().innerHTML = `
    <div class="top"><div><div class="crumbs"><a href="#/">Painel</a> / ambiente</div><h1>Ambiente</h1>
      <div class="sub">O que o JFLP encontrou nesta máquina: ${esc(env.os)}, UI em Java ${esc(env.java)}.</div></div></div>
    <div class="grid g2">
      <div class="card"><h3>JDKs</h3><table class="t"><tr><th>Versão</th><th>Caminho</th></tr>
        ${env.jdks.map((j) => `<tr><td><span class="badge accent">Java ${j.major}</span> <span class="muted">${esc(j.version)}</span></td><td class="mono">${esc(j.path)}</td></tr>`).join("")}</table>
        <p class="faint">O agente roda em qualquer JVM a partir do Java 5; a UI precisa do Java 8+.</p></div>
      <div class="card"><h3>Ferramentas</h3><table class="t"><tr><th>Ferramenta</th><th>Versão</th></tr>
        ${env.tools.map((t) => `<tr><td><b>${esc(t.name)}</b></td><td>${t.version ? `<span class="badge ok">${esc(t.version)}</span>` : `<span class="badge">não encontrado</span> <span class="faint">${esc(t.hint || "")}</span>`}</td></tr>`).join("")}</table></div>
    </div>
    <form class="card" style="margin-top:16px" id="sf"><h3>Configurações</h3>
      <div class="grid g2">
        <label class="f"><span>Defects4J home</span><input type="text" name="defects4jHome" value="${esc(st.defects4jHome || "")}" placeholder="/opt/defects4j">
          <em>A pasta onde o Defects4J foi clonado e inicializado (contém framework/bin/defects4j).</em></label>
        <label class="f"><span>JDK padrão</span><select name="defaultJdk"><option value="">automático (Java 11 se houver)</option>
          ${env.jdks.map((j) => `<option value="${esc(j.path)}" ${st.defaultJdk === j.path ? "selected" : ""}>Java ${j.major} — ${esc(j.path)}</option>`).join("")}</select></label>
      </div>
      <label class="f"><span>Outros JDKs (um caminho por linha)</span><textarea name="jdks">${esc((st.jdks || []).join("\n"))}</textarea></label>
      <button class="btn primary" type="submit">Salvar</button></form>`;
  $("#sf").onsubmit = async (e) => {
    e.preventDefault();
    const f = e.target;
    await api("settings", { method: "PUT", body: { defects4jHome: f.defects4jHome.value.trim(), defaultJdk: f.defaultJdk.value,
      jdks: f.jdks.value.split("\n").map((s) => s.trim()).filter(Boolean) } });
    toast("Configurações salvas.");
    route();
  };
}

// ---------------------------------------------------------------- boot

(async function boot() {
  try {
    const t = localStorage.getItem("jflp-theme");
    if (t) document.documentElement.dataset.theme = t;
  } catch (e) { /* private mode */ }
  const s = await api("session");
  S.token = s.token;
  S.heuristics = s.heuristics;
  S.workspace = s.workspace;
  window.addEventListener("hashchange", route);
  route();
})();
