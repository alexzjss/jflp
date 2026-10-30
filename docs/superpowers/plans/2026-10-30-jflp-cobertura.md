# JFLP Cobertura bugs-dot-jar — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn JFLP Studio into a self-driving coverage of the whole bugs-dot-jar folder (1.158 bugs × Maven/Jaguar 1/Jaguar 2), with per-bug state in the app, pause/stop/resume/redo, N parallel workers, import of the old PowerShell results and automatic upsert into a Google Sheet.

**Architecture:** The coverage is **one special batch** in the existing SQLite queue (`batches.options` carries `{"cobertura": true, ...}`), one job per bug×kind, ordered round-robin by project with the three kinds of a bug consecutive. The single `Worker` becomes a `WorkerPool` of N `Worker` threads that claim jobs atomically, never two jobs of the same bug at once. Everything per-bug (cells, verdicts, detail) keeps coming from `sbfl/bugview.py`; a new `sbfl/cobertura.py` derives the bug *situação* and project summaries; `sbfl/planilha.py` keeps a persistent outbox and posts to an Apps Script web app.

**Tech Stack:** Python ≥ 3.11, FastAPI + Jinja2 + htmx (already used), SQLite, `unittest`, Google Apps Script (`tools/planilha/Code.gs`).

**Spec:** `docs/superpowers/specs/2026-10-30-jflp-cobertura-design.md` (read it first; §4a holds the settled answers).

**Mockups:** https://claude.ai/artifact/PQjS4fyrUdwJsY5RdWggV4 — boards "A · Painel" (`/cobertura`) and "C · Tabela" (`/cobertura/bugs`). Board B is not built.

## Global Constraints
- All UI and messages in Portuguese (pt-BR), same tone as existing templates.
- Subprocesses always as argument lists; server only on 127.0.0.1; nothing written outside `JFLP_HOME`, the configured work dir and the bugs-dot-jar worktrees `git worktree` creates.
- Never compare paths with `Path.resolve()` (MSIX AppData redirection). Use `os.path.normcase(str(p))`.
- Never `git reset --hard`, `git clean -fdx` or `checkout -f` in the user's bugs-dot-jar clones: only `git worktree add/remove` (as `sbfl/gitutil.add_worktree` already does).
- Tests: `unittest`, no network, no real Maven/Java. Run all with: `.venv\Scripts\python -m unittest discover -s tests -q` (expected: `OK`, possibly with skips).
- Coverage runs the **whole test suite**: batch opts `{"test_scope": "all", "auto_search": true, "archive_checkout": false}`.
- Kinds order per bug: `["maven", "jaguar1", "jaguar2"]`.
- Default workers: **4** (`cobertura.workers`).
- Do **not** disable Maven's http blocking for coverage; use an HTTPS mirror settings file (Task 8). The existing `unblock-http` fix is left as is.
- Commit after every task with a `feat:`/`fix:`/`docs:` message in Portuguese, ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Branch: `feat/jflp-studio`.

## Review Focus
1. Two workers preparing two bugs of the **same repo** at the same time → `git worktree add` is serialized per repo; no `index.lock`/`worktrees` error (test in Task 3).
2. App closed (or "Parar agora") mid-bug → on reopen the job is `pending` again and reruns; the table shows one row per bug and the newest run wins (test in Task 3).
3. Sheet URL wrong / no internet / Apps Script returns HTML error page → outbox keeps every row, panel shows the error and the minutes since last success, worker unaffected (test in Task 5).
4. A project folder in bugs-dot-jar not cloned or with broken git → `sincronizar` skips it with a warning in the result and still adds the other projects (test in Task 2).
5. Configured work dir on a drive that does not exist (e.g. `E:` absent) → falls back to `home.root/work` and the Ambiente page shows a warning (test in Task 1).

---

### Task 1: Configuração da cobertura e pasta de trabalho  *(Haiku)*

**Files:**
- Modify: `sbfl/home.py` (DEFAULTS, `_NOTES`, `Home`)
- Modify: `sbfl/web/app.py:110-140` (`serve`: apply work dir before creating the worker)
- Test: `tests/test_home.py`

**Interfaces:**
- Produces: `DEFAULTS["cobertura"] = {"workers": 4, "work_dir": "", "legado": True, "legado_offline": True}`, `DEFAULTS["planilha"] = {"url": "", "token": ""}`, `DEFAULTS["jaguar1"]["port_aware"] = False`.
- Produces: `Home.work_root: Path | None = None` (dataclass field, default None); `Home.work` returns `work_root` when set, else `root / "work"`.
- Produces: `apply_work_dir(home: Home, settings: Settings) -> tuple[Home, str | None]` in `sbfl/home.py`: returns `(dataclasses.replace(home, work_root=Path(wd)), None)` when `cobertura.work_dir` is set and its drive/anchor exists; `(home, "<aviso em português>")` when set but the drive is missing; `(home, None)` when empty.

- [ ] **Step 1: Failing tests** in `tests/test_home.py`: `test_work_root_override` (`Home(tmp, work_root=tmp/"x").work == tmp/"x"`), `test_apply_work_dir_vazio` (returns same home, None), `test_apply_work_dir_ok` (tmp subdir → work_root set), `test_apply_work_dir_drive_ausente` (path `"Q:\\nao\\existe"` on Windows / `"/nao-existe-xyz/w"` elsewhere, mocked `os.path.exists` false for the anchor → home unchanged, warning contains `"pasta de trabalho"`), `test_defaults_cobertura` (`load_settings(home).get("cobertura","workers") == 4`, `get("planilha","url") == ""`).
- [ ] **Step 2:** Run `.venv\Scripts\python -m unittest tests.test_home -q` → FAIL.
- [ ] **Step 3:** Implement. `_NOTES["cobertura"] = "# Cobertura do bugs-dot-jar: workers paralelos, pasta de trabalho (ex.: E:/JFLP/cobertura), perfis legados"`, `_NOTES["planilha"] = "# Planilha do Google (Apps Script): URL do app da web e token"`. In `serve`, call `apply_work_dir` right after loading settings and keep the warning in `app.state.aviso_work_dir` for Task 7.
- [ ] **Step 4:** Run the full suite → `OK`.
- [ ] **Step 5:** Commit `feat: configuração da cobertura e pasta de trabalho configurável`.

---

### Task 2: Núcleo da cobertura (`sbfl/cobertura.py`)  *(Sonnet)*

**Files:**
- Create: `sbfl/cobertura.py`
- Modify: `sbfl/queue.py` (schema migration: `jobs.origem TEXT`, `jobs.forcado TEXT`; add via `ALTER TABLE ... ADD COLUMN` when `PRAGMA table_info(jobs)` lacks them, inside `_connect`)
- Test: `tests/test_cobertura.py` (use `tests/bugsjar_fixture.py` to build a tiny bugs-dot-jar collection with 2 projects)

**Interfaces:**
- Consumes: `queue._connect`, `queue.create_batch` logic, `prepare.bugsjar_repo_bugs(repo) -> list[Bug]`, `prepare.is_bugsjar_repo`, `store.load_run(home, rel) -> dict`, `status.is_success`, `status.PROJETO_INVALIDO`.
- Produces (all in `sbfl/cobertura.py`):
  - `KINDS = ("maven", "jaguar1", "jaguar2")`
  - `SIT_OK, SIT_PARCIAL, SIT_FALHOU, SIT_NAO_EXEC, SIT_RODANDO, SIT_PENDENTE = "ok", "parcial", "falhou", "nao_executavel", "rodando", "pendente"`
  - `batch_id(home) -> int | None` — the batch whose `options` has `"cobertura": true` (newest if several).
  - `sincronizar(home, settings, root: Path) -> dict` — creates the coverage batch if missing (name `"Cobertura bugs-dot-jar"`, kinds `KINDS`, opts per Global Constraints plus `"cobertura": True, "root": str(root)`); for every repo dir under `root` that `is_bugsjar_repo`, enumerates bugs and inserts the missing (bug_key, kind) jobs. New jobs get `idx` so that projects alternate (bug *i* of project *p* gets rank `i * n_projects + p`; kinds consecutive inside a bug: `idx = rank*3 + k`), appended after the current max idx. Returns `{"batch_id", "adicionados", "total_bugs", "avisos": [str]}`; a repo whose enumeration fails/returns nothing gets an aviso and is skipped. Idempotent.
  - `etapa(job: dict, run: dict | None) -> str` — per job: `"rodando"`/`"pendente"` from job state; `forcado == "nao_executavel"` → `"nao_executavel"`; done + `is_success(status)` (or `sem_teste_falhando` for maven) → `"ok"`; done + status `projeto_invalido` → `"nao_executavel"`; done + failed and the run's `attempt_configs` contain a config with `jdk == 7` (legacy already tried) → `"nao_executavel"`; else `"falhou"`.
  - `situacao(etapas: dict[str, str]) -> str` — any rodando → rodando; all ok → ok; all pendente → pendente; any ok → parcial; all in {nao_executavel} → nao_executavel; any pendente (none ok) → pendente; else falhou.
  - `bugs(home, bid, projeto=None, situacoes=None, busca=None) -> list[dict]` — one dict per bug: `{"key","projeto","bug","etapas":{kind: str},"jobs":{kind: job},"situacao","origem"}`; `busca` matches bug id or the run diagnosis cause (case-insensitive).
  - `resumo(home, bid) -> dict` — `{"total", "concluidos", "por_situacao": {sit: n}, "por_projeto": [{"projeto","total","concluidos","ok","parcial","falhou","nao_executavel","pendente"}], "ritmo_h": float|None, "restante_s": int|None}`; `concluidos` = ok+parcial+falhou+nao_executavel; ritmo from bugs whose last job finished in the last 6 h (moving window).
  - `refazer(home, bid, bug_keys: list[str] | None = None, so_falhas: bool = True) -> int` — sets matching done jobs back to `pending` (`run_rel`, `error`, `seconds` NULL, `forcado` NULL, `origem` NULL); `so_falhas` keeps `ok` and `nao_executavel` etapas; `bug_keys=None` = all bugs in situação falhou/parcial. Never touches running jobs. Returns jobs requeued.
  - `marcar_nao_executavel(home, bid, bug_keys: list[str]) -> int` — sets `forcado='nao_executavel'` and pending jobs of those bugs to `done`.
  - `priorizar(home, bid, bug_keys: list[str]) -> int` — moves their pending jobs to the front (`idx = min_idx - N + i`, keeping kind order).

- [ ] **Step 1: Failing tests** — `test_sincronizar_cria_lote_e_jobs` (2 projects × 2 bugs → 12 jobs, opts test_scope `all`); `test_sincronizar_idempotente` (second call `adicionados == 0`); `test_ordem_alterna_projetos` (jobs ordered by idx: bug keys go p1b1×3, p2b1×3, p1b2×3…); `test_sincronizar_repo_quebrado` (an extra folder with `.bugs-dot-jar` but no git → `avisos` non-empty, the other projects still added); `test_etapa_regras` (table of job/run dicts → expected etapa, covering every rule above); `test_situacao_regras` (≥7 combinations); `test_refazer_so_falhas` (ok jobs stay done, failed go pending, run_rel NULL); `test_marcar_e_priorizar`; `test_resumo_contagens`.
- [ ] **Step 2:** Run `.venv\Scripts\python -m unittest tests.test_cobertura -q` → FAIL.
- [ ] **Step 3:** Implement. Reads of runs go through `store.load_run`; batch `bugs()` must not call git (performance: 1.158 bugs, must render in < 1 s on the 3.474 jobs — load all jobs with one query and runs with one `SELECT ... WHERE path IN` chunked by 500).
- [ ] **Step 4:** Full suite → `OK`.
- [ ] **Step 5:** Commit `feat: núcleo da cobertura (sincronizar, situação, refazer)`.

---

### Task 3: Vários workers em paralelo (`WorkerPool`)  *(Sonnet)*

**Files:**
- Modify: `sbfl/queue.py` (`Worker`, new `WorkerPool`)
- Modify: `sbfl/gitutil.py` (`add_worktree`: per-repo `threading.Lock`, dict keyed by `os.path.normcase(str(repo))`)
- Modify: `sbfl/jaguar.py:79-96` (`build_cmd`: port from a thread-local, default 6300)
- Modify: `sbfl/web/app.py` (`serve` creates `WorkerPool(home, settings, n)`), `sbfl/cli.py` (`batch run` uses the pool with `n=1` unless `--workers`)
- Test: `tests/test_queue.py`, `tests/test_robustez.py` (existing ones must still pass), new tests in `tests/test_queue.py`

**Interfaces:**
- Consumes: Task 2 schema.
- Produces:
  - Claim rule in `Worker._next_job`: pending job of an active batch, lowest `(batch_id, idx)`, **and** `NOT EXISTS (running job with the same bug_key)`, **and**, when `settings.get("jaguar1","port_aware")` is false and the candidate is `jaguar1`, `NOT EXISTS (running jaguar1 job)`. Keep the atomic `UPDATE ... WHERE state='pending'` claim.
  - `Worker.__init__(home, settings, lock=None, *, index: int = 0, owns_lock: bool = True)`; a pool worker never acquires/releases the instance lock nor runs `_recover` (the pool does, once).
  - `Worker._discard_job` deletes only its own run (no longer wipes `home.work/"w"`; `execute` already removes its workdirs).
  - `jaguar.set_port(port: int | None) -> None` / thread-local read in `build_cmd`; pool worker *i* sets `6300 + 1 + i` when `port_aware`, also passed as `-Djaguar.port=<p>` before `-javaagent`.
  - `class WorkerPool` with the same public surface the web routes use on `Worker`: `start()`, `stop(kill=True)`, `is_alive()`, `status() -> dict` (adds `"workers": n`), `current() -> dict | None` (first running), `currents() -> list[dict]` (one per busy worker, each with `index`), `progress_of(job_id)`, `cancel_job(job_id)`, `cancel(batch_id)`, `pause(batch_id)`, `resume(batch_id)`, `cancel_pending()`, `cancel_everything()`, `remove_pending(job_id)`, `subscribe(job_id: int | None = None)` / `unsubscribe(q)` (subscribe to the worker running `job_id`, else to all), plus `interromper() -> int`: kills the running jobs of every worker and returns them to `pending` (the "Parar agora" button), counting jobs interrupted.
  - `WorkerPool(home, settings, n: int | None = None, lock=None)`; `n` defaults to `settings.get("cobertura","workers")`.

- [ ] **Step 1: Failing tests** (fake `execute` patched in `sbfl.queue`, like the existing tests do): `test_pool_roda_em_paralelo` (2 workers, 2 bugs, execute blocks on an Event → both running at once); `test_pool_nunca_dois_jobs_do_mesmo_bug` (1 bug, 3 kinds, 3 workers → max 1 running); `test_jaguar1_serializado_sem_port_aware`; `test_jaguar1_paralelo_com_port_aware` (and each thread saw a different port via `jaguar.build_cmd`); `test_interromper_devolve_a_fila` (running → pending, run deleted); `test_reabrir_recupera_running` (job left `running` in DB → pool start → `pending` → runs once); `test_add_worktree_serializado` (two threads calling `add_worktree` on the same repo path with `git` patched to record overlap → never overlaps).
- [ ] **Step 2:** Run `.venv\Scripts\python -m unittest tests.test_queue -q` → FAIL.
- [ ] **Step 3:** Implement. Keep `Worker` usable alone (CLI and existing tests).
- [ ] **Step 4:** Full suite → `OK`.
- [ ] **Step 5:** Commit `feat: vários workers em paralelo, sem dois jobs do mesmo bug`.

---

### Task 4: Importação dos resultados do pipeline PowerShell  *(Sonnet)*

**Files:**
- Create: `sbfl/importar.py`
- Test: `tests/test_importar.py` + fixtures under `tests/fixtures/importar/` (two tiny `summary.json` — one per format — a small `control-flow.xml` copied/trimmed from a real one, a `maven.log` tail)

**Interfaces:**
- Consumes: `cobertura.batch_id`, `store.RunWriter(home, bug, kind, parent)` (`.write_json`, `.copy_into`, `.finish(run)`, `.rel`), `prepare.bug_from_key`.
- Source layout (read-only): `<raiz>/<projeto>/<bug-dir>/summary.json`, `control-flow*.xml`, `maven.log`. Skip any `<projeto>/origin/` subtree. Read both summary formats (look at `E:\Documentos\GitHub\bugs-dot-jar\relatorios_cobertura\_tools\make_xlsx.py` `backfill_rank`/`tests_tuple` for the field names, and real samples under `relatorios_cobertura\commons-math` and `relatorios_cobertura\maven`).
- Produces: `importar_pipeline(home, settings, raiz: Path) -> dict` returning `{"maven": n, "jaguar1": n, "ignorados": n, "erros": [str]}`. For each bug dir whose bug_key (`bugsjar/<projeto>/<bug-dir name>`) is in the coverage batch: writes a Maven run (status `ok_testes_falhando`/`sem_teste_falhando`/`compilacao_falhou`… mapped from the summary; `tests` counts; `failures` with test names; `origem: "importado"`, `test_scope: "all"`) and, if a Jaguar 1 XML exists, a Jaguar 1 run with `outputs/jaguar1.xml` (the first/largest `control-flow*.xml`; multiple module XMLs: copy the one with most `<line>` elements, list the others in `warnings`). Marks the coverage jobs (bug, kind) `done` with `run_rel` and `origem='importado'`. Jaguar 2 job stays pending. Idempotent: a job already `origem='importado'` or `done` with a run is skipped (counted in `ignorados`).

- [ ] **Step 1: Failing tests** — `test_importa_formato_antigo`, `test_importa_formato_run_bug2`, `test_ignora_origin`, `test_idempotente`, `test_jaguar2_fica_pendente`, `test_run_importado_tem_scores` (`compare.run_scores(home, rel)` returns a non-empty list for the imported Jaguar 1 run).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement; also expose CLI `jflp cobertura importar <pasta>` in `sbfl/cli.py`.
- [ ] **Step 4:** Full suite → `OK`.
- [ ] **Step 5:** Commit `feat: importa resultados do pipeline PowerShell para a cobertura`.

---

### Task 5: Planilha do Google (outbox + envio + Apps Script)  *(Sonnet)*

**Files:**
- Create: `sbfl/planilha.py`, `tools/planilha/Code.gs`
- Modify: `sbfl/queue.py` (`Worker._finish_job`/`_discard_job` call an optional `on_bug_change(bug_key)` hook given to the pool), `sbfl/web/app.py` (start `Enviador` with the pool)
- Test: `tests/test_planilha.py`

**Interfaces:**
- Consumes: `cobertura.bugs`, `bugview` helpers for positions/verdicts (`veredito_jaguar`, `posicoes_defeito`, `_concordancia` logic via `bug_detail` or a lighter helper you add to `bugview.py`), `bugview.resumo_bugsjar(repo, ramo)` for the JIRA summary (cache it in the outbox row; it reads git).
- Produces:
  - `COLUNAS: list[str]` — exactly, in order: `Projeto, Bug, Resumo, Link JIRA, Situação, Última execução, Tentativas, Origem, Maven testes, Maven falhas, Maven erros, Testes que falharam, Bateu com o catálogo?, J1 status, J1 posição do patch, J1 topo, J2 status, J2 posição do patch, J2 topo, Veredito J1, Veredito J2, Concordância J1×J2, Motivo, Perfil`.
  - `linha(home, bug: dict) -> list` — one row in `COLUNAS` order; "Bateu com o catálogo?" uses `bugview.esperado_maven`, "não reproduziu" when Maven ran with 0 failures; Perfil = `moderno`/`legado`/`legado offline` from the chosen attempt config (`jdk == 7` → legado; `params.offline` → legado offline).
  - SQLite table `planilha_outbox(bug_key TEXT PRIMARY KEY, linha TEXT, atualizado TEXT, enviado TEXT)`.
  - `enfileirar(home, bug_keys: list[str]) -> None` (upsert, `enviado` NULL) and `enfileirar_todos(home) -> int`.
  - `class Enviador(threading.Thread)`: `__init__(home, settings, post=None)`; `post(url: str, body: dict) -> dict` defaults to a `urllib.request` JSON POST (timeout 60 s, follows the 302 to `googleusercontent.com`, raises on non-JSON); sends up to 100 rows per call `{"token", "colunas": COLUNAS, "linhas": [[key, *row]]}`; expects `{"ok": true}`; on error waits 30 s doubling to 15 min; `status() -> {"ultimo_ok": iso|None, "pendentes": int, "erro": str|None, "configurada": bool}`; does nothing (and `configurada` false) while `planilha.url` is empty; `acordar()` to send now.
  - `Code.gs`: `doPost(e)` → parse JSON, compare `token` with `PropertiesService.getScriptProperties().getProperty('JFLP_TOKEN')` (mismatch → `{ok:false, erro:'token'}`), `LockService.getScriptLock().waitLock(30000)`, create sheet `Bugs` with header row `["Chave", ...colunas]` if missing, upsert by column A (`Chave` = bug_key) using one `getRange().setValues` per contiguous block, then (re)write sheet `Resumo` with `COUNTIFS` formulas per project (rodados, OK, parcial, falhou, não executável, ✓/~/✗ per Jaguar). Returns `ContentService.createTextOutput(JSON.stringify({ok:true, n}))` with JSON mime type. Header comment in Portuguese with the 6 deployment steps from spec §4.

- [ ] **Step 1: Failing tests** (fake `post`): `test_linha_tem_todas_as_colunas`; `test_enfileirar_upsert` (same key twice → one row); `test_enviador_envia_e_marca`; `test_enviador_erro_mantem_fila_e_mostra_status` (post raises `URLError`, then returns HTML → `pendentes` unchanged, `erro` filled, no exception escapes); `test_enviador_sem_url_nao_envia`; `test_hook_do_worker_enfileira` (finishing a coverage job enqueues its bug).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement. Generate `planilha.token` (`secrets.token_urlsafe(24)`) on first save of a URL if empty.
- [ ] **Step 4:** Full suite → `OK`.
- [ ] **Step 5:** Commit `feat: envio automático para a planilha do Google`.

---

### Task 6: Tela Cobertura — Painel (opção A)  *(Sonnet)*

**Files:**
- Create: `sbfl/web/routes_cobertura.py`, `sbfl/web/templates/cobertura.html`, `sbfl/web/templates/_cobertura_painel.html`
- Modify: `sbfl/web/app.py` (include router; `/` → `/cobertura` when a coverage batch exists or `bugsjar.root` is set, else the current behavior), `sbfl/web/templates/base.html` (sidebar: Cobertura, Resultados, Ambiente; remove Rodar link — `/rodar` routes stay reachable), `sbfl/web/static/app.css` (segment bar, state colors: ok `#1d7a46`, parcial `#c77700`, falhou `#b3261e`, não executável `#5b4e8c`, rodando `#2f5fd0`, pendente `#e7eaf0`; dark-mode equivalents in the existing `@media` block)
- Test: `tests/test_web_cobertura.py` (use `tests/web_helpers.WebFixture`; worker mock gains `currents`, `interromper`)

**Interfaces:**
- Consumes: Task 2 (`batch_id`, `sincronizar`, `resumo`, `refazer`), Task 3 (`pool.currents()`, `pool.pause/resume/interromper`), Task 5 (`enviador.status()` via `app.state.enviador`, may be absent → section hidden).
- Routes: `GET /cobertura` (no batch yet → form with the bugs-dot-jar folder prefilled from `bugsjar.root` and button "Começar cobertura" → `POST /cobertura/iniciar` which runs `sincronizar` and redirects); `GET /cobertura/painel` (partial, polled by htmx every 5 s); `POST /cobertura/pausar`, `/cobertura/continuar`, `/cobertura/parar`, `/cobertura/refazer-falhados`, `/cobertura/sincronizar` — all POST, token-protected like the existing forms, redirect back (303).
- Copy (exact): selo `Rodando` / `Pausando…` (paused with jobs still running) / `Pausado` / `Parado` (no workers alive); buttons `Pausar após o bug atual`, `Continuar`, `Parar agora`, `Refazer falhados (N)`; progress `"{concluidos} de {total} bugs concluídos ({pct}%)"`; `"ritmo {x} bugs/h · faltam ≈ {h} h · {n} workers"`; sheet line `"Planilha Google sincronizada há {t} · {n} linhas"` or (red) `"Planilha sem enviar há {t} · {n} pendentes · {erro}"` or `"Planilha não configurada · configurar"` (link to `/ambiente#planilha`).
- Layout per board A: header, progress section with segmented bar + legend, then two columns: "Por projeto" table (link "ver" → `/cobertura/bugs?projeto=X`) and "Rodando agora" (bug, elapsed, Maven/Jaguar 1/Jaguar 2 chips: done/running/waiting) + "Últimos concluídos" (5).

- [ ] **Step 1: Failing tests** — `test_sem_cobertura_mostra_inicio`; `test_iniciar_sincroniza_e_redireciona` (fixture collection); `test_painel_mostra_contagens_e_projetos`; `test_pausar_continuar_parar_chamam_pool`; `test_refazer_falhados`; `test_sidebar_sem_rodar`; `test_painel_planilha_erro_em_vermelho`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement; reuse `comum.py` helpers and the existing token/CSRF pattern from `routes_rodar.py`.
- [ ] **Step 4:** Full suite → `OK` (update existing web tests that asserted the Rodar sidebar link).
- [ ] **Step 5:** Commit `feat: tela Cobertura (painel)`.

---

### Task 7: Tabela dos bugs (opção C) e Ambiente  *(Haiku)*

**Files:**
- Create: `sbfl/web/templates/cobertura_bugs.html`, `sbfl/web/templates/_cobertura_linhas.html`
- Modify: `sbfl/web/routes_cobertura.py`, `sbfl/web/templates/ambiente.html`, `sbfl/web/routes_env.py`
- Test: `tests/test_web_cobertura.py`, `tests/test_web_base.py` (Ambiente)

**Interfaces:**
- Consumes: Task 2 `bugs()`, `refazer`, `marcar_nao_executavel`, `priorizar`; Task 5 `enfileirar_todos`, `Enviador.acordar`; existing `bugview.bug_detail(home, bid, key)` + `_lote_detalhe.html` for the row detail.
- Routes: `GET /cobertura/bugs?situacao=&projeto=&q=&pagina=` (50 per page; filters kept in the URL); `GET /cobertura/bug?key=...` (detail partial, reuses `_lote_detalhe.html`); `POST /cobertura/bugs/acao` with `acao in {refazer, nao_executavel, priorizar}` and `keys` (multi) → redirect preserving filters.
- Columns per board C: checkbox, Bug (+ projeto), Situação, Maven (`"{total} · {falhas} falhas"` or status label), Jaguar 1 / Jaguar 2 (`"pos. N"` or status label or `"na fila"`/`"rodando…"`), J1×J2 (`"k/10"`), Motivo / perfil (`importado` shown here), link `refazer`.
- Filter chips with counts: `Todos, Pendentes, Rodando, OK, Parcial, Falharam, Não executáveis`.
- Ambiente gets a section `id="planilha"`: URL field, token (read-only, "copiar"), `Reenviar tudo` (calls `enfileirar_todos` + `acordar`), status line; and a section "Cobertura": workers (1–12), pasta de trabalho, legado on/off, legado offline on/off, plus the Task 1 work-dir warning when present. Saving writes `config.toml`; workers/pasta take effect on restart (say so on screen).

- [ ] **Step 1: Failing tests** — `test_tabela_filtra_por_situacao_e_projeto`; `test_tabela_busca`; `test_tabela_pagina`; `test_acao_refazer_selecionados`; `test_acao_nao_executavel`; `test_acao_priorizar`; `test_detalhe_do_bug`; `test_ambiente_salva_planilha_e_gera_token`; `test_reenviar_tudo`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Full suite → `OK`.
- [ ] **Step 5:** Commit `feat: tabela da cobertura e configuração da planilha`.

---

### Task 8: Perfis legados e mirror HTTPS  *(Sonnet)*

**Files:**
- Modify: `sbfl/autosearch.py` (`candidates`), `sbfl/run_maven.py` (mirror settings, offline prefetch), `sbfl/toolchain/download.py` (Zulu 7, Maven 3.0.5 plans), `sbfl/web/routes_env.py` + `ambiente.html` (download buttons in "Cobertura")
- Test: `tests/test_run_maven.py`, `tests/test_execute.py`, `tests/test_download.py`

**Interfaces:**
- Produces:
  - `run_maven.mirror_settings_args(home) -> list[str]`: writes `home.tools/"jflp-settings-mirror.xml"` with one `<mirror>` `id=jflp-https`, `mirrorOf=external:http:*`, `url=https://repo.maven.apache.org/maven2` and returns `["-s", path]`. Used for every coverage attempt (`params.get("cobertura")`) unless `unblock-http` is on (that fix keeps precedence). Coverage batch opts gain `params: {"cobertura": true}` (update Task 2's `sincronizar` opts).
  - `candidates(...)`: when `settings.get("cobertura","legado")` and kind == `"maven"` and the tools resolve (`tools.jdks["7"]` or a detected JDK 7, and Maven `3.0.5`), append `replace(first, jdk=7, maven="3.0.5")`; when also `legado_offline`, append a second one with `params={**first.params, "offline": True}`. These come **after** the existing candidates and still respect `run.max_attempts` (raise the coverage default by passing `max_attempts` 8 in the coverage `params`, read in `execute`).
  - `run_maven`: with `cfg.params.get("offline")`, first runs `dependency:go-offline` with the JDK 8 profile (Maven 3.9, mirror settings, same `m2`), then the build with `-o` on JDK 7 + Maven 3.0.5. JDK 7 Maven must not touch the network.
  - `download.plan_jdk(home, 7)` uses the Azul Zulu 7 URL (`zulu7.56.0.11-ca-jdk7.0.352-win_x64.zip` on Windows; sha256 pinned — compute it once when implementing and hard-code it); `plan_maven(home, "3.0.5")` uses `https://archive.apache.org/dist/maven/binaries/apache-maven-3.0.5-bin.zip` with its published sha1/md5 check.
- Note: Jaguar 1/2 keep their own profiles (JDK 8 / JDK 11); the legacy chain is for the Maven stage. If J1/J2 fail after Maven only ran in legacy, Task 2's `etapa` rule marks them `nao_executavel` only when their own attempts included JDK 7; otherwise `falhou` (redo-able).

- [ ] **Step 1: Failing tests** — `test_mirror_settings_conteudo`; `test_candidates_incluem_legado_quando_ferramentas_existem`; `test_candidates_sem_legado_sem_jdk7`; `test_offline_roda_go_offline_antes` (proc.run patched; order of commands and `-o`); `test_plan_jdk7_e_maven305`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Full suite → `OK`.
- [ ] **Step 5:** Commit `feat: perfis legados (JDK 7 + Maven 3.0.5, offline) e mirror HTTPS`.

---

### Task 9: CLI e README  *(Haiku)*

**Files:**
- Modify: `sbfl/cli.py`, `README.md`
- Test: `tests/test_cli.py`

**Interfaces:**
- CLI: `jflp cobertura sincronizar [pasta]`, `jflp cobertura status` (prints `resumo` as text), `jflp cobertura importar <pasta>` (Task 4), `jflp cobertura refazer [--todos]`, `jflp cobertura planilha --reenviar`.
- README: new section "Cobertura do bugs-dot-jar" (what it does, Painel/Tabela, pausar/parar/continuar/refazer, workers, pasta de trabalho em `E:`, perfis legados) and "Planilha do Google" with the exact steps: create blank sheet → Extensões → Apps Script → paste `tools/planilha/Code.gs` → Configurações do projeto → Propriedades do script → `JFLP_TOKEN` = token shown in Ambiente → Implantar → Nova implantação → App da web (executar como: eu; quem pode acessar: qualquer pessoa) → autorizar → copiar URL → colar em Ambiente → Planilha → Reenviar tudo. Update the sidebar description (Cobertura instead of Rodar).

- [ ] **Step 1: Failing tests** — `test_cli_cobertura_status`, `test_cli_cobertura_sincronizar`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement + README.
- [ ] **Step 4:** Full suite → `OK`.
- [ ] **Step 5:** Commit `docs: cobertura e planilha no README; comandos da CLI`.

---

### Task 10: Piloto real (controller, not a subagent)

- [ ] Configure `cobertura.work_dir = E:\JFLP\cobertura`, `workers = 4`; import `E:\Documentos\GitHub\bugs-dot-jar\relatorios_cobertura`; user deploys `Code.gs` and pastes URL.
- [ ] Run ~1 h; check: sheet receives rows without duplicates; Pausar/Continuar/Parar agora/fechar-e-reabrir keep state; legacy bugs: how many reproduce (decide `legado_offline` per spec §4a); ritmo vs. estimate.
- [ ] Report numbers to the user; fix what the pilot shows (new tasks if needed).
