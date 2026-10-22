// Vista do lote: abre/fecha o detalhe de cada bug (carregado sob demanda) e o "ver mais" das linhas do Jaguar.
// Os trechos abertos sobrevivem à atualização da tabela (ela é trocada quando um job muda de estado).
(function () {
  var abertos = {};
  function corpo(btn) {
    var d = document.getElementById(btn.getAttribute("aria-controls"));
    return d ? { linha: d, alvo: d.querySelector(".lv-det-corpo") } : null;
  }
  function abre(btn) {
    var c = corpo(btn);
    if (!c) return;
    c.linha.hidden = false;
    btn.setAttribute("aria-expanded", "true");
    btn.closest("tbody").classList.add("aberto");
    abertos[btn.dataset.key] = true;
    htmx.ajax("GET", btn.dataset.url, { target: c.alvo, swap: "innerHTML" });
  }
  function fecha(btn) {
    var c = corpo(btn);
    if (!c) return;
    c.linha.hidden = true;
    btn.setAttribute("aria-expanded", "false");
    btn.closest("tbody").classList.remove("aberto");
    delete abertos[btn.dataset.key];
  }
  document.addEventListener("click", function (e) {
    var t = e.target;
    if (!t.closest) return;
    var mais = t.closest("[data-mais]");
    if (mais) {
      var aberto = mais.getAttribute("aria-expanded") === "true";
      var tabela = mais.parentNode.querySelector("table[data-top]");
      if (tabela) tabela.querySelectorAll("tr.lv-extra").forEach(function (tr) { tr.hidden = aberto; });
      mais.setAttribute("aria-expanded", String(!aberto));
      mais.textContent = aberto ? mais.dataset.rotulo : "ver menos";
      return;
    }
    var btn = t.closest(".lv-toggle");
    if (!btn) {
      var linha = t.closest("tr.lv-linha");
      if (!linha || t.closest("a, button, input, select, summary")) return;
      btn = linha.querySelector(".lv-toggle");
    }
    if (!btn) return;
    if (btn.getAttribute("aria-expanded") === "true") fecha(btn); else abre(btn);
  });
  // a tabela foi trocada (o elemento novo ainda não foi marcado): reabre o que estava aberto
  document.body.addEventListener("htmx:afterSettle", function () {
    var el = document.getElementById("lote-vista");
    if (!el || el.dataset.restaurado) return;
    el.dataset.restaurado = "1";
    el.querySelectorAll(".lv-toggle").forEach(function (b) { if (abertos[b.dataset.key]) abre(b); });
  });
})();
