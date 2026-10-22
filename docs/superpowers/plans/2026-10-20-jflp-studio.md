# JFLP Studio — Plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transformar o JFLP numa ferramenta com UI local (Windows e Arch) que prepara bugs (Defects4J, Bugs.jar, local), roda Maven puro, Jaguar 2 (principal) e Jaguar 1 com ambiente gerenciado, guarda cada execução numa pasta imutável e exibe e compara os resultados.

**Architecture:** O núcleo Python (`sbfl/`) vira uma biblioteca sem UI, organizada em toolchain → preparação → execução (com diagnóstico, receitas e busca automática) → armazenamento → comparação/lote/exportação. Todo trabalho passa por uma fila persistida em SQLite e é consumido por um worker em thread. Uma UI FastAPI + Jinja2 + htmx vendorizado (só em `127.0.0.1`, com token) e a CLI `jflp` são duas fachadas finas sobre o mesmo núcleo.

**Tech Stack:** Python ≥3.11 (stdlib `tomllib`, `sqlite3`, `xml.etree`, `urllib`, `zipfile`, `tarfile`), FastAPI, Uvicorn, Jinja2, python-multipart, htmx 2.0.4 (vendorizado); opcionais `zstandard` e `pywebview`. Testes com `unittest`.

**Spec:** `docs/superpowers/specs/2026-10-20-jflp-ui-jaguar2-design.md`. Leia junto com este plano; as seções citadas (§) são de lá.

## Global Constraints

- Python ≥3.11. Dependências obrigatórias: `fastapi`, `uvicorn`, `jinja2`, `python-multipart`. Opcionais: `zstandard`, `pywebview`. **Sem** pandas/matplotlib.
- Todas as mensagens ao usuário (UI, CLI, diagnósticos) em **português**.
- Jaguar 2: `br.usp.each.saeg:jaguar2-{junit4,jacoco-provider,core,xml-exporter,csv-exporter}` versão **0.0.2**, escopo `test`. Listener: `br.usp.each.saeg.jaguar2.junit.JaguarJUnitRunListener` (propriedade `listener` do Surefire). `jacoco-maven-plugin` **0.8.7** (goal `prepare-agent`). `maven-surefire-plugin` **3.5.3**. `maven-compiler-plugin` **3.8.0**. JUnit **4.12** quando ausente.
- Saídas do Jaguar 2 (verificadas no código-fonte): `target/jaguar2.csv` com colunas `classe,linha,cef,cnf,cep,cnp,susp` (classe em formato JVM com `/`) e `target/jaguar2.xml` (`<report><tests fail= pass=/><package name=><sourcefile name=><line nr= cef= cep= susp=/>`). **Só linhas com susp > 0** são gravadas; a heurística é sempre Ochiai. O bytecode do Jaguar 2 é Java 6, então ele roda em JDK 8+.
- Perfis de ambiente: `jaguar2` = JDK 11 + Maven ≥3.9.9; `jaguar1` = JDK 8 + Maven 3.6.3 (aceita 3.6.x–3.8.0); `maven` = JDK escolhido pelo nível do `pom.xml` (`sbfl/jdk.py`) + Maven 3.6.3 se disponível, senão qualquer ≥3.6.
- Nunca alterar o `pom.xml` original, o `~/.m2` do usuário, PATH/variáveis globais. Todo Maven roda com `-Dmaven.repo.local=<home>/m2`.
- Testes falhando são **dado**: todo `mvn ... test` leva `-Dmaven.test.failure.ignore=true -DfailIfNoTests=false -Dsurefire.failIfNoSpecifiedTests=false`.
- Subprocessos sempre como lista de argumentos, nunca `shell=True`.
- O servidor web escuta só em `127.0.0.1`.
- Escopo de testes padrão = `relevant` (§8.4a). `all` é opção.
- Status (strings exatas): `ok`, `ok_testes_falhando`, `checkout_falhou`, `jdk_incompativel`, `dependencia_nao_resolvida`, `plugin_incompativel`, `compilacao_falhou`, `classes_duplicadas`, `testes_nao_encontrados`, `jaguar_sem_saida`, `jaguar_crash`, `timeout`, `interrompido`, `desconhecido`.
- Testes: `python -m unittest discover -s tests -v`, um arquivo `tests/test_<módulo>.py` por tarefa. Testes unitários **sem rede** (mock de `urllib.request.urlopen`). Testes que precisam de `java`/`mvn` reais usam `@unittest.skipUnless(shutil.which("mvn") and shutil.which("java"), ...)`.
- Cada tarefa indica o **Modelo** do subagente implementador (Sonnet ou Haiku).

## Review Focus

1. **Bug cujo teste não falha** (o defeito não se reproduziu): o Jaguar 2 grava `jaguar2.csv` **vazio**. O resultado esperado é status `ok`, com o aviso "nenhum teste falhou: relatório vazio" e **sem** busca automática. Não pode virar `jaguar_sem_saida`. Teste na Tarefa 12.
2. **Caminhos com espaço e acento** (ex.: `C:\Users\Nome Sobrenome\Documentos`, home com `ç`): `mvn.cmd`, `-Dmaven.repo.local`, `-f` e `JAVA_HOME` precisam funcionar. Testes nas Tarefas 5 e 9.
3. **App fechado ou morto no meio de uma execução**: a pasta `.partial` e o job `running` precisam ser recuperados ao reabrir. A pasta vira execução com status `interrompido` e o job volta para `pending`. Nunca aparece meia execução como válida. Testes nas Tarefas 6 e 15.
4. **POM que já tem** `argLine` no Surefire, `<properties>` de listener, plugin do JaCoCo ou Surefire só em `pluginManagement`/no pai: o POM gerado precisa manter `@{argLine}` e a configuração existente, sem elementos duplicados. Teste na Tarefa 10.
5. **WSL ausente, Defects4J ausente ou saída do `wsl.exe` em UTF-16LE** (nesta máquina o `wsl -l -q` devolve UTF-16 com "não está instalado"): mensagem clara em português, sem exceção. Teste na Tarefa 3.

---

## Mapa de arquivos

```
pyproject.toml                    # novo: pacote, entry point `jflp`, deps
jflp.ps1 / jflp.cmd / jflp.sh     # novos: lançadores (cria venv na 1ª vez)
sbfl/
  home.py                         # novo: Home (pastas) + Settings (config.toml da home)
  status.py                       # novo: constantes de status
  toolchain/__init__.py
  toolchain/detect.py             # novo: JDKs, Mavens, Git, WSL, Defects4J
  toolchain/download.py           # novo: planos de download, fetch, safe_extract
  toolchain/profiles.py           # novo: resolve(perfil) -> ToolEnv
  outputs.py                      # novo: parsers surefire / jaguar2 csv,xml / jaguar1 xml
  prepare.py                      # novo: checkout pristine + working copy, por benchmark
  store.py                        # novo: RunWriter, índice SQLite, reindex, recuperação
  pomgen/__init__.py
  pomgen/pom.py                   # novo: PomDoc (ET com comentários)
  pomgen/fixes.py                 # novo: base + correções nomeadas
  pomgen/generate.py              # novo: generate()/write()
  run_maven.py / run_jaguar2.py / run_jaguar1.py   # novos: uma tentativa de cada tipo
  diagnose.py                     # novo: classificação + regras PT
  recipes.py                      # novo: receitas TOML
  autosearch.py                   # novo: candidatos de tentativa
  execute.py                      # novo: orquestra prepare → tentativas → store
  compare.py                      # reescrito: testes, espectros, posições, lote
  queue.py                        # novo: fila SQLite + Worker
  dataset.py                      # novo: exportar dataset
  cli.py                          # reescrito: comandos jflp
  web/app.py, web/routes_*.py, web/templates/*.html, web/static/{htmx.min.js,app.css}
removidos: report.py (parser migra p/ outputs.py), aggregate.py, campaign.py, pipeline.py,
           adapters/{bears,gitbugjava,manifest}.py, manifests/, config.example.toml (Task 20)
```

Mantidos e reaproveitados: `proc.py`, `jdk.py`, `build.py` (`resolve_tool`), `jaguar.py`, `classfile.py`, `gitutil.py`, `envcheck.py`, `envinfo.py`, `triage.py` (padrões viram regras), `power.py`, `selftest.py`, `diffgt.py`, `truth.py`, `adapters/{base,bugsjar,gitpair,defects4j}.py`.

---

## Marco 0 — Base

### Task 1: Poda do código legado, empacotamento e testes verdes

**Modelo:** Sonnet

**Files:**
- Delete: `sbfl/aggregate.py`, `sbfl/campaign.py`, `sbfl/pipeline.py`, `sbfl/adapters/bears.py`, `sbfl/adapters/gitbugjava.py`, `sbfl/adapters/manifest.py`, `manifests/exemplo.jsonl`
- Create: `sbfl/outputs.py` (só `Req` + `parse_xml_jaguar1` movidos de `report.py` nesta tarefa), `sbfl/status.py`, `pyproject.toml`
- Delete: `sbfl/report.py` (depois de mover o parser)
- Modify: `sbfl/adapters/__init__.py` (NAMES = `["d4j", "bugsjar"]`), `sbfl/cli.py` (remover comandos `run/analyze/aggregate/status/inspect` e o que importa módulos removidos; o resto é reescrito na Task 16), `sbfl/compare.py`/`sbfl/selftest.py`/`sbfl/triage.py` (imports), `tests/test_core.py`

**Interfaces:**
- Produces: `sbfl.status` com as constantes `OK="ok"`, `OK_TEST_FAIL="ok_testes_falhando"`, `INFRA = (...)` (todas as outras strings das Global Constraints) e `def is_success(status: str) -> bool`. `sbfl.outputs.parse_xml_jaguar1(path) -> list[Req]` (mesmo comportamento do antigo `report.parse_xml`), `outer(cls: str) -> str`.
- `pyproject.toml`: projeto `jflp`, `requires-python = ">=3.11"`, entry point `jflp = "sbfl.cli:main"`, dependências das Global Constraints, extras `full = ["zstandard", "pywebview"]`, package data `sbfl/web/templates/*`, `sbfl/web/static/*`, `sbfl/selftest_data/**`.

- [ ] **Step 1:** Rodar `python -m unittest discover -s tests` e confirmar a base: 29 testes, 9 erros `TOMLDecodeError: Invalid hex value` (caminhos do Windows com `\` em TOML entre aspas duplas, gerados pelos próprios testes).
- [ ] **Step 2:** Nos helpers de `tests/test_core.py` que escrevem `config.toml`, gravar caminhos com `Path.as_posix()`. Rodar de novo: os 9 erros somem (ou sobram só falhas não ligadas a TOML; registre quais).
- [ ] **Step 3:** Remover os testes dos módulos apagados (`test_campaign_*`, `test_aggregate_smoke`, `test_end_to_end_with_fakes`, `test_needs_run_retry`, `test_bears_layout`, testes de `evaluate`/fórmulas). Manter os de `diffgt`, `truth`, `jdk`, `build`, `classfile`, `junit_flavor`, `jaguar.run_jaguar`, `compare`, `triage`, `selftest` e o `parse_xml` (agora `outputs.parse_xml_jaguar1`).
- [ ] **Step 4:** Mover `Req`, `parse_xml` (→ `parse_xml_jaguar1`) e `outer` para `sbfl/outputs.py`, apagar os módulos listados, corrigir os imports e criar `sbfl/status.py` e `pyproject.toml`.
- [ ] **Step 5:** Rodar `python -m unittest discover -s tests -v`. Esperado: 0 erros e 0 falhas. Rodar também `pip install -e .` num venv de rascunho e `jflp --help` (o CLI provisório precisa carregar).
- [ ] **Step 6:** Commit `refactor: poda do pipeline SBFL e empacotamento jflp`.

### Task 2: Home e Settings

**Modelo:** Haiku

**Files:**
- Create: `sbfl/home.py`, `tests/test_home.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Home: root: Path` com as propriedades `tools`, `m2`, `work`, `runs`, `recipes`, `datasets` (todas `root / nome`), `db_path = root / "jflp.db"` e `config_path = root / "config.toml"`; `@classmethod default() -> Home` (variável `JFLP_HOME` se definida; senão `%LOCALAPPDATA%\jflp` no Windows e `~/.jflp` no resto); `ensure() -> Home` (cria as pastas e escreve um `config.toml` comentado se faltar).
  - `class Settings` com `get(section, key)`, `section(name) -> dict`, `set(section, key, value)` e `save()` (TOML escrito à mão, sem dependência nova; só os tipos str/int/bool/list/dict simples), e `load_settings(home: Home) -> Settings`, que mescla com `DEFAULTS`.
  - `DEFAULTS` (valores exatos):
    ```
    tools:    {jdks: {}, mavens: {}}            # overrides manuais: {"11": "C:/..."}
    jaguar2:  {version: "0.0.2", jacoco: "0.8.7", surefire: "3.5.3", compiler: "3.8.0", junit: "4.12"}
    jaguar1:  {lib: "", timeout_s: 3600, max_retries: 6, jvm_args: []}
    run:      {test_scope: "relevant", timeout_s: 7200, archive_checkout: true, max_attempts: 6,
               maven_opts: "", skip_flags: <lista atual de config.DEFAULTS["maven"]["skip_flags"]>}
    bugsjar:  {root: "", projects: [], url_template: "https://github.com/bugs-dot-jar/{project}.git"}
    defects4j:{home: "", wsl_distro: "", projects: ["Lang","Math","Time","Chart","Closure","Mockito","Jsoup"]}
    ui:       {port: 0}
    ```
- Consumes: nada.

- [ ] **Step 1: Testes que falham** — `test_default_uses_env` (com `JFLP_HOME=<tmp>`, `Home.default().root == tmp`), `test_ensure_creates_dirs_and_config`, `test_settings_roundtrip` (`set("run","test_scope","all")` + `save()` + `load_settings` devolvem `"all"`; um caminho do Windows com `\` sobrevive ao ciclo), `test_defaults_merged` (`get("jaguar2","surefire") == "3.5.3"`).
- [ ] **Step 2:** Rodar `python -m unittest tests.test_home -v` → FAIL (módulo não existe).
- [ ] **Step 3:** Implementar `sbfl/home.py`. Para o TOML com caminhos, escrever strings em aspas simples (literais) quando contiverem `\`.
- [ ] **Step 4:** Rodar → PASS.
- [ ] **Step 5:** Commit `feat: home e settings do jflp`.

---

## Marco 1 — Toolchain

### Task 3: Detecção de JDK, Maven, Git, WSL e Defects4J

**Modelo:** Sonnet

**Files:**
- Create: `sbfl/toolchain/__init__.py`, `sbfl/toolchain/detect.py`, `tests/test_detect.py`

**Interfaces:**
- Consumes: `Home`, `Settings` (Task 2); `envcheck.java_major`, `envcheck.maven_version`.
- Produces:
  - `@dataclass class Jdk: home: Path; major: int; version: str; source: str` (`"config" | "managed" | "system"`).
  - `@dataclass class Maven: home: Path; version: tuple[int, int, int]; source: str`.
  - `@dataclass class WslStatus: installed: bool; distros: list[str]; d4j: str | None; message: str`.
  - `find_jdks(home, settings) -> list[Jdk]`: deduplicado por caminho real; ordem `config`, `managed` (`home.tools/jdk-*`), `system`. O sistema inclui `JAVA_HOME`, `java` no PATH e as pastas `/usr/lib/jvm/*` (Linux), `C:\Program Files\{Java,Eclipse Adoptium,Zulu,Microsoft,Amazon Corretto}\*` e `HKLM\SOFTWARE\JavaSoft\*` via `winreg` (Windows).
  - `find_mavens(home, settings) -> list[Maven]`: `config`, `managed` (`home.tools/apache-maven-*`), `mvn` no PATH, `MAVEN_HOME`/`M2_HOME`, `/opt/maven`, `/usr/share/maven`.
  - `java_info(java_home: Path) -> tuple[int, str] | None` (roda `bin/java -version`).
  - `maven_info(maven_home: Path) -> tuple[int, int, int] | None` (roda `bin/mvn(.cmd) -v` com `JAVA_HOME` de qualquer JDK encontrado).
  - `decode_console(raw: bytes) -> str`: detecta UTF-16LE (bytes nulos intercalados ou BOM) e decodifica; senão UTF-8 com `errors="replace"`.
  - `wsl_status(settings) -> WslStatus`: no Linux devolve `installed=False, message="não se aplica"`; no Windows roda `wsl.exe -l -q` e decodifica com `decode_console`. Se houver distro, testa `wsl -d <distro> -- bash -lc "command -v defects4j || ls <home>/framework/bin/defects4j"`.
  - `defects4j_cmd(settings, wsl: WslStatus) -> list[str] | None`: prefixo de comando para chamar o Defects4J (`["wsl","-d",distro,"--","bash","-lc"]` no Windows; caminho direto no Linux).

- [ ] **Step 1: Testes que falham:**
  - `test_decode_console_utf16`: `decode_console("O Subsistema do Windows para Linux não está instalado".encode("utf-16-le"))` contém `"não está instalado"`.
  - `test_wsl_absent_message` (mock de `subprocess.run` devolvendo esse texto em UTF-16 com rc 1): `installed is False`, `distros == []`, mensagem em PT, sem exceção.
  - `test_find_jdks_managed_and_config` (pastas falsas `tools/jdk-11.0.24+8/bin/java(.exe)` + override em settings; mock de `java_info`): fontes e majors corretos, sem duplicatas.
  - `test_maven_version_parse`: `"Apache Maven 3.9.16 (2bdd...)"` → `(3,9,16)`.
  - `test_detect_real_machine` (skipUnless java): `find_jdks` encontra pelo menos um JDK com major > 0.
- [ ] **Step 2:** Rodar → FAIL.
- [ ] **Step 3:** Implementar. Toda chamada de subprocesso com `timeout=20` e `capture_output=True` em bytes, decodificando com `decode_console`.
- [ ] **Step 4:** Rodar → PASS.
- [ ] **Step 5:** Commit `feat: detecção de JDK, Maven, WSL e Defects4J`.

### Task 4: Downloads seguros (JDK Temurin, Maven, Jaguar 1)

**Modelo:** Sonnet

**Files:**
- Create: `sbfl/toolchain/download.py`, `tests/test_download.py`

**Interfaces:**
- Consumes: `Home`.
- Produces:
  - `@dataclass class Download: name: str; url: str; size: int | None; checksum: str | None; algo: str | None; archive: bool; dest: Path`.
  - `plan_jdk(home, major: int, os_name: str | None = None, arch: str | None = None) -> Download`: consulta `https://api.adoptium.net/v3/assets/latest/{major}/hotspot?os={windows|linux}&architecture={x64|aarch64}&image_type=jdk&vendor=eclipse` e usa `binary.package.{link,size,checksum}` (sha256) do primeiro item. `dest = home.tools / f"jdk-{release_name}"`.
  - `plan_maven(home, version: str) -> Download`: `https://archive.apache.org/dist/maven/maven-3/{v}/binaries/apache-maven-{v}-bin.zip`, checksum do `.sha512` irmão (algo `sha512`), `dest = home.tools / f"apache-maven-{v}"`.
  - `plan_jaguar1(home) -> list[Download]`: `https://github.com/saeg/jaguar/releases/download/jaguar-0.0.6-4f9d6f0/br.usp.each.saeg.jaguar.core-0.0.6-SNAPSHOT-jar-with-dependencies.jar` e `https://github.com/saeg/jaguar/releases/download/jaguar-cli-0.0.1/jacocoagent.jar`, ambos com `archive=False` e `dest = home.tools / "jaguar1/lib"`. Sem checksum publicado → `checksum=None`, e a UI mostra isso.
  - `fetch(d: Download, progress: Callable[[int, int | None], None] | None = None) -> Path`: baixa em `home.tools/_dl/<arquivo>.part`, verifica o checksum (falha → apaga e levanta `DownloadError` em PT), extrai com `safe_extract` num diretório temporário irmão e renomeia para `dest` (a raiz única do arquivo vira `dest`). Devolve `dest`.
  - `safe_extract(archive: Path, dest: Path) -> None`: zip e tar.gz; recusa membros com caminho absoluto, `..` ou links simbólicos/hard que apontem para fora de `dest`.
  - `class DownloadError(Exception)`.
- Notas: `urllib.request` com `User-Agent: jflp`, timeout de 60 s e leitura em blocos. Nunca altera PATH.

- [ ] **Step 1: Testes que falham** (todos com `urlopen` mockado):
  - `test_plan_jdk_parses_adoptium` (JSON fixture mínimo) → url, size e sha256 corretos.
  - `test_plan_maven_urls` (`"3.9.9"` → url exata acima; sha512 lido do mock).
  - `test_fetch_checksum_mismatch` → `DownloadError` e nenhum `dest` criado.
  - `test_safe_extract_rejects_traversal` (zip com `../evil.txt`) → exceção, nada escrito fora.
  - `test_fetch_zip_single_root` (zip `apache-maven-3.9.9/bin/mvn`) → `dest/bin/mvn` existe.
- [ ] **Step 2:** Rodar → FAIL.
- [ ] **Step 3:** Implementar.
- [ ] **Step 4:** Rodar → PASS. Depois, uma vez e manualmente (rede real), `python -c "from sbfl.toolchain.download import *; from sbfl.home import Home; print(plan_jdk(Home.default(), 11))"` deve mostrar uma URL do Adoptium com tamanho.
- [ ] **Step 5:** Commit `feat: downloads verificados de JDK, Maven e Jaguar 1`.

### Task 5: Perfis de ambiente (ToolEnv)

**Modelo:** Sonnet

**Files:**
- Create: `sbfl/toolchain/profiles.py`, `tests/test_profiles.py`

**Interfaces:**
- Consumes: `find_jdks`, `find_mavens` (Task 3); `jdk.declared_level`; `build.resolve_tool`.
- Produces:
  - `@dataclass class ToolEnv: jdk: Jdk; maven: Maven; env: dict[str, str]; m2: Path` com `mvn(*args) -> list[str]` (`[<maven>/bin/mvn(.cmd)], "-B", f"-Dmaven.repo.local={m2}", *args]`), `java() -> str` e `describe() -> dict` (versões, para `env.json`).
  - `class MissingTool(Exception): kind: str ("jdk"|"maven"); wanted: str; message: str` (PT, ex.: "JDK 11 não encontrado — baixe na tela Ambiente").
  - `PROFILES = {"jaguar2": {"jdk": 11, "maven_min": (3,9,9)}, "jaguar1": {"jdk": 8, "maven": (3,6,3), "maven_max": (3,8,0)}, "maven": {"jdk": None, "maven_pref": (3,6,3), "maven_min": (3,6,0)}}`.
  - `resolve(profile: str, home, settings, pom_dir: Path | None = None, jdk: int | None = None, maven: str | None = None) -> ToolEnv`. Override explícito vence. JDK: major exato para os perfis com `jdk`. No perfil `maven`, usa `pick_home` (menor ≥ `max(nível_do_pom, 8)`, senão o maior). Maven: o preferido exato se existir, senão o mais novo dentro de `[min, max]`.
  - O `env` copia `os.environ`, define `JAVA_HOME`, põe `jdk/bin` e `maven/bin` na frente do `PATH`, remove `M2_HOME`/`MAVEN_HOME`/`JAVA_TOOL_OPTIONS`/`_JAVA_OPTIONS`/`MAVEN_ARGS` e define `MAVEN_OPTS` a partir de `run.maven_opts`.

- [ ] **Step 1: Testes que falham:**
  - `test_resolve_jaguar2_picks_11_and_399` (listas falsas de JDK 8/11/17 e Maven 3.6.3/3.9.9) → 11 e (3,9,9).
  - `test_resolve_missing_raises_pt` (sem JDK 11) → `MissingTool` com `kind == "jdk"` e "11" na mensagem.
  - `test_env_is_sanitized` (`JAVA_TOOL_OPTIONS` no ambiente) → ausente em `env`; `PATH` começa com `jdk/bin`.
  - `test_paths_with_space_and_accent` (home em `<tmp>/Pasta Ção/`) → `mvn("test")` contém `-Dmaven.repo.local=<tmp>/Pasta Ção/m2` como **um único** item da lista.
  - `test_real_mvn_version_with_spaces` (skipUnless mvn; home com espaço) → `subprocess.run(toolenv.mvn("-v"), env=toolenv.env)` com rc 0.
- [ ] **Step 2:** Rodar → FAIL.
- [ ] **Step 3:** Implementar.
- [ ] **Step 4:** Rodar → PASS.
- [ ] **Step 5:** Commit `feat: perfis de ambiente isolados`.

---

## Marco 2 — Preparação, saídas e armazenamento

### Task 6: Armazenamento imutável de execuções

**Modelo:** Sonnet

**Files:**
- Create: `sbfl/store.py`, `tests/test_store.py`

**Interfaces:**
- Consumes: `Home`, `sbfl.status`, `adapters.base.Bug`.
- Produces:
  - `class RunWriter`: `__init__(self, home, bug: Bug, kind: str, parent: str | None = None)` cria `runs/<bench>/<proj>/<bug>/.<AAAAMMDD-HHMMSS>-<kind>.partial/` (com sufixo `-2`, `-3`… se colidir). Atributos `dir`, `rel` (caminho final relativo a `home.runs`, com `/`) e `started`. Métodos:
    - `attempt_dir(n: int) -> Path` (`attempts/<n>/`)
    - `write_json(rel: str, obj)` e `write_text(rel: str, text: str)`
    - `copy_into(src: Path, rel: str)` (arquivo ou árvore)
    - `gzip_into(src: Path, rel: str)`
    - `archive_checkout(src_dir: Path) -> str` (`checkout.tar.zst` se `zstandard` importar, senão `checkout.zip`; devolve o nome)
    - `finish(run: dict) -> Path`: grava `run.json` com os campos `kind`, `bug`, `status`, `started`, `finished`, `seconds`, `attempts` e `parent`, mais o que vier em `run`; renomeia para o nome final, marca os arquivos como somente leitura (`chmod 0o444` / `stat.S_IREAD`) e indexa.
    - É context manager: uma exceção dentro do `with` chama `finish({"status": "desconhecido", "error": repr(e)[-1500:]})` e propaga.
  - Índice SQLite em `home.db_path`, tabela `runs(path TEXT PRIMARY KEY, benchmark, project, bug, kind, status, started, seconds REAL, n_tests INT, n_failed INT, parent TEXT)`.
  - `list_runs(home, benchmark=None, project=None, bug=None, kind=None) -> list[dict]` (mais recente primeiro), `load_run(home, rel: str) -> dict`, `run_dir(home, rel) -> Path`.
  - `reindex(home) -> int`: reconstrói a tabela varrendo `runs/**/run.json`.
  - `recover_partials(home) -> list[str]`: toda `.partial` vira execução final com status `interrompido` (grava `run.json` mínimo) e entra no índice.

- [ ] **Step 1: Testes que falham:**
  - `test_finish_atomic_and_readonly` → depois de `finish`, a pasta final existe, `.partial` não, e `run.json` não é gravável.
  - `test_never_overwrites` (dois writers no mesmo segundo) → duas pastas distintas.
  - `test_exception_marks_desconhecido`.
  - `test_reindex_matches` (apagar `jflp.db` e chamar `reindex`) → mesmas linhas.
  - `test_recover_partials_marks_interrompido` (Review Focus 3): `.partial` órfã vira `interrompido` e aparece em `list_runs`.
  - `test_archive_checkout_zip_fallback` (com `zstandard` bloqueado via `sys.modules`) → `checkout.zip` com os arquivos do checkout.
- [ ] **Step 2:** Rodar → FAIL. **Step 3:** Implementar. **Step 4:** Rodar → PASS.
- [ ] **Step 5:** Commit `feat: armazenamento imutável de execuções`.

### Task 7: Parsers das saídas puras

**Modelo:** Haiku

**Files:**
- Modify: `sbfl/outputs.py`, `sbfl/compare.py` (remover `parse_surefire`/`parse_jaguar_log`, importar de `outputs`)
- Create: `tests/test_outputs.py`

**Interfaces:**
- Produces (em `sbfl/outputs.py`):
  - `@dataclass class LineScore: cls: str; line: int; cef: int; cep: int; cnf: int | None; cnp: int | None; susp: float`. `cls` é sempre FQCN com `.`, e classes internas ficam com `$`.
  - `parse_surefire(reports_dir: Path) -> dict[str, str]` (movido de `compare.py`, mesma semântica).
  - `parse_jaguar1_log(path: Path) -> dict[str, str]` (movido de `compare.parse_jaguar_log`).
  - `parse_jaguar2_csv(path: Path) -> list[LineScore]` (colunas `classe,linha,cef,cnf,cep,cnp,susp`; `/`→`.`; arquivo vazio → `[]`).
  - `parse_jaguar2_xml(path: Path) -> tuple[dict, list[LineScore]]` (`{"fail": int, "pass": int}`; `cls = package.replace("/", ".") + "." + sourcefile sem ".java"`; `cnf`/`cnp` = `None`).
  - `jaguar1_scores(path: Path) -> list[LineScore]` (a partir de `parse_xml_jaguar1`).
  - `test_counts(results: dict[str, str]) -> dict[str, int]` (`passed/failed/error/skipped/total`).

- [ ] **Step 1: Testes que falham:** fixtures pequenas inline. `test_csv_parsing` (`org/jsoup/helper/StringUtil,42,1,0,3,10,0.125988` → `LineScore("org.jsoup.helper.StringUtil",42,1,3,0,10,0.125988)` — atenção à ordem cef,cnf,cep,cnp), `test_csv_empty`, `test_xml_parsing`, `test_surefire_statuses` (passed/failure/error/skipped), `test_counts`.
- [ ] **Step 2:** Rodar → FAIL. **Step 3:** Implementar. **Step 4:** Rodar `python -m unittest tests.test_outputs tests.test_core -v` → PASS.
- [ ] **Step 5:** Commit `feat: parsers das saídas de Maven e Jaguar`.

### Task 8: Preparação de bugs (pristine + cópia de trabalho)

**Modelo:** Sonnet

**Files:**
- Create: `sbfl/prepare.py`, `tests/test_prepare.py`
- Modify: `sbfl/adapters/bugsjar.py` (adicionar `checkout`), `sbfl/adapters/defects4j.py` (adicionar `checkout` via `defects4j_cmd`), `sbfl/adapters/base.py` (método abstrato `checkout`)

**Interfaces:**
- Consumes: `Home`, `Settings`, `defects4j_cmd`/`wsl_status` (Task 3), `gitutil`, `truth.git_truth`, `diffgt`.
- Produces:
  - `@dataclass class Prepared: bug: Bug; pristine: Path; module: str; relevant_tests: list[str] | None; truth: list[FaultLine]; info: dict`.
  - `Adapter.checkout(self, bug: Bug, dest: Path, log: Path) -> Prepared` (contrato novo: **não compila**, só checkout, módulo, testes relevantes e verdade).
  - `prepare(home, settings, bug: Bug, log: Path, refresh: bool = False) -> Prepared`: o pristine fica em `home.work / "p" / <sha1(bug.key)[:10]>` (caminho curto). Reaproveita se existir o marcador `prepared.json`, que serializa `Prepared` sem o `bug`.
  - `working_copy(prep: Prepared, home) -> Path`: copia o pristine para `home.work / "w" / <8 hex aleatórios>`, sem `target/`.
  - `list_bugs(settings, benchmark: str, project: str | None = None) -> list[Bug]`.
  - `local_bug(spec: str) -> Bug`: `spec` é uma pasta existente (`Bug("local", <nome da pasta>, "dir-<sha1(caminho)[:8]>", {"path": ...})`) ou `URL@commit` (`Bug("local", <nome do repo>, <commit[:12]>, {"repo": url, "commit": commit})`).
  - `to_wsl_path(p: Path) -> str`: `E:\a\b` → `/mnt/e/a/b`.
  - `bug_from_key(settings, key: str) -> Bug`: inverso de `Bug.key` para `d4j` e `bugsjar` (usado pela fila e pela CLI).
- Regras:
  - Bugs.jar: `git worktree add` no pristine. Verdade via `git_truth(wt, "HEAD", fixed)`. Testes relevantes = FQCNs dos arquivos de teste alterados no commit do fix (mesma lógica do escopo `changed` de `gitpair.py`). `module` vem de `git_truth`.
  - Local pasta: `copytree` ignorando `target`, `.idea` e `*.class`; sem verdade, `relevant_tests=None`.
  - Local URL: `git clone` + `checkout <commit>`.
  - Defects4J: `checkout -p P -v Nb -w <pristine>` e `-v Nf` num temporário (via WSL com `to_wsl_path`). Verdade como em `defects4j.py`; `relevant_tests` = `export -p tests.relevant`. `module="."`. Sem `pom.xml` → `info["jaguar2_suportado"] = False` (§3).

- [ ] **Step 1: Testes que falham:**
  - `test_local_dir_copy_does_not_touch_source` (pasta com `pom.xml` e `target/x`) → pristine sem `target`, e a origem fica com o mtime intacto.
  - `test_bugsjar_checkout_with_fake_repo` (repo git criado no teste com uma branch `bugs-dot-jar_X-1_<hash>` e um commit de fix que altera `src/main/java/a/B.java` e `src/test/java/a/BTest.java`) → `relevant_tests == ["a.BTest"]` e verdade não vazia.
  - `test_prepare_reuses_marker`, `test_working_copy_is_independent`.
  - `test_to_wsl_path`.
  - `test_d4j_unavailable_message` (WSL ausente) → `StageError("checkout_falhou", <PT>)`.
- [ ] **Step 2:** Rodar → FAIL. **Step 3:** Implementar. **Step 4:** Rodar → PASS.
- [ ] **Step 5:** Commit `feat: preparação de bugs com checkout pristine`.

---

## Marco 3 — Execução

### Task 9: Execução Maven pura

**Modelo:** Sonnet

**Files:**
- Create: `sbfl/run_maven.py`, `tests/test_run_maven.py`

**Interfaces:**
- Consumes: `ToolEnv` (Task 5), `Prepared` (Task 8), `proc.run`, `outputs.parse_surefire/test_counts`.
- Produces:
  - `@dataclass class AttemptConfig: fixes_on: frozenset[str] = frozenset(); fixes_off: frozenset[str] = frozenset(); params: dict = field(default_factory=dict); jdk: int | None = None; maven: str | None = None; test_scope: str = "relevant"` com `key() -> str` (estável, para deduplicar) e `to_dict()`.
  - `@dataclass class AttemptResult: rc: int | None; timed_out: bool; seconds: float; log: Path; outputs: dict[str, Path]; tests: dict[str, str]; command: list[str]; toolenv: dict; extra: dict` (`outputs` mapeia nomes lógicos `surefire`, `jaguar2_csv`, `jaguar2_xml`, `jacoco_exec`, `jaguar1_xml`, `pom` para caminhos existentes).
  - `test_selection_args(prep: Prepared, scope: str) -> list[str]`: `[]` para `all` ou `relevant_tests is None`. Senão, `-Dtest=<lista>`. Se a linha resultante passar de 6000 caracteres, grava `jflp-tests.txt` no módulo e usa `-Dsurefire.includesFile=jflp-tests.txt` (uma linha `pacote/Classe.java` por classe).
  - `run_maven(home, settings, prep, workdir: Path, cfg: AttemptConfig, log: Path) -> AttemptResult`: `toolenv.mvn("clean","test", *skip_flags, "-Dmaven.test.failure.ignore=true","-DfailIfNoTests=false","-Dsurefire.failIfNoSpecifiedTests=false", *test_selection_args)` no diretório do módulo, com timeout `run.timeout_s`.

- [ ] **Step 1: Testes que falham:**
  - `test_selection_args_relevant` / `_all` / `_long_list_uses_includes_file`.
  - `test_run_maven_with_fake_mvn` (mesmo estilo dos fakes de `tests/test_core.py`: um `mvn` falso em Python que cria `target/surefire-reports/TEST-a.BTest.xml` com 1 failure) → `tests` com 1 `failed` e rc 0.
  - `test_run_maven_real_selftest_project` (skipUnless mvn+java; usa `sbfl/selftest_data/fixed` copiado para uma pasta com espaço no nome) → surefire presente e `tests` não vazio.
- [ ] **Step 2:** Rodar → FAIL. **Step 3:** Implementar. **Step 4:** Rodar → PASS.
- [ ] **Step 5:** Commit `feat: execução Maven pura`.

### Task 10: Gerador do POM do Jaguar 2

**Modelo:** Sonnet

**Files:**
- Create: `sbfl/pomgen/__init__.py`, `sbfl/pomgen/pom.py`, `sbfl/pomgen/fixes.py`, `sbfl/pomgen/generate.py`, `tests/test_pomgen.py`, `tests/fixtures/poms/*.xml`

**Interfaces:**
- Consumes: `Settings` (versões do `[jaguar2]`), `jdk.declared_level`.
- Produces:
  - `pom.PomDoc`: `load(path) -> PomDoc` (ElementTree com `TreeBuilder(insert_comments=True)`, namespace POM registrado como padrão), `serialize() -> str` (com `ET.indent`, declaração XML e `\n`), helpers `find_plugin(group, artifact, in_management=False)`, `ensure_plugin(group, artifact, version) -> Element`, `ensure_dependency(group, artifact, version, scope)`, `set_build_value(tag, value)`, `property(name) -> str | None`.
  - `pom.chain(module_dir: Path) -> list[Path]`: o `pom.xml` do módulo e os pais alcançáveis no disco via `<parent><relativePath>` (padrão `../pom.xml`), parando quando o groupId/artifactId não bate.
  - `@dataclass class PomContext: module_dir: Path; chain_texts: list[str]; jdk: int; versions: dict; test_dir: str | None; main_dir: str | None; has_junit4: bool; has_junit5_only: bool; surefire_argline: str | None`. Construído por `build_context(module_dir, settings, jdk) -> PomContext`.
  - `fixes.Fix(id: str, title: str, reason: str, detect: Callable[[PomContext], bool], apply: Callable[[PomDoc, PomContext, dict], None], default_params: dict)` e `FIXES: dict[str, Fix]` com os ids exatos:
    - `skip-animal-sniffer` — detecta `animal-sniffer` em qualquer POM da cadeia; aplica o plugin `org.codehaus.mojo:animal-sniffer-maven-plugin` com `<configuration><skip>true</skip>` no filho (versão: a da cadeia, ou `1.16`).
    - `compiler-java` — detecta nível declarado < 8 ou ausente; aplica `maven-compiler-plugin` na versão `params.version` (padrão do settings `3.8.0`) com `source`/`target` = `ctx.jdk` e `encoding` UTF-8.
    - `source-dirs` — detecta testes fora de `src/test/java` (ou main fora de `src/main/java`) inferindo a raiz pelo `package` declarado num `*.java`; aplica `<testSourceDirectory>`/`<sourceDirectory>` relativos. **Nunca move arquivos.**
    - `junit4` — detecta ausência de `junit:junit` na cadeia; adiciona `junit:junit:{junit}` com escopo test.
    - `override-version` — sem detecção; `params = {"deps": {"g:a": "v"}, "plugins": {"g:a": "v"}}` redeclara no filho.
    - `exclude-duplicates` — sem detecção (só sugerido pelo diagnóstico); `params = {"files": [...]}` vira `<excludes>` no compiler.
  - `fixes.apply_base(doc, ctx)`, sempre aplicado: as 5 dependências do Jaguar 2 (escopo `test`), o plugin JaCoCo com execução `prepare-agent`, e o Surefire `{surefire}` com `<properties combine.children="append"><property><name>listener</name><value>br.usp.each.saeg.jaguar2.junit.JaguarJUnitRunListener</value></property></properties>`. Se `ctx.surefire_argline` existir e não contiver `@{argLine}`, define `<argLine>@{argLine} <original></argLine>`. Não duplica nada que já exista: atualiza no lugar.
  - `@dataclass class PomPlan: text: str; diff: str; detected: list[str]; applied: list[str]; params: dict; warnings: list[str]`.
  - `generate.generate(module_dir, settings, jdk: int, fixes_on: set[str] | None = None, fixes_off: set[str] = set(), params: dict = {}) -> PomPlan`. `fixes_on=None` significa "usar as detectadas". O diff é `difflib.unified_diff` entre o **original normalizado** pelo mesmo serializador e o gerado, para mostrar só mudanças semânticas. Avisos em PT: "projeto só tem JUnit 5: o Jaguar 2 só roda testes JUnit 4"; "sem pom.xml: Jaguar 2 não suportado".
  - `generate.write(plan, module_dir) -> Path` grava `module_dir / "jaguar2-pom.xml"`.

- [ ] **Step 1: Testes que falham** (fixtures em `tests/fixtures/poms/`):
  - `test_base_injection_minimal_pom` → o XML gerado tem as 5 dependências 0.0.2, o JaCoCo 0.8.7, o Surefire 3.5.3 e o listener exato.
  - `test_original_untouched` → os bytes do `pom.xml` não mudam.
  - `test_existing_argline_preserved` (Review Focus 4: Surefire com `<argLine>-Xmx1g</argLine>` e `<properties>` com outro listener) → `@{argLine} -Xmx1g`, os dois listeners presentes, um único elemento de plugin Surefire.
  - `test_surefire_only_in_pluginmanagement` → um plugin em `build/plugins` com a versão 3.5.3, sem duplicar.
  - `test_detect_animal_sniffer_in_parent` (pai em `../pom.xml`) → `skip-animal-sniffer` em `detected` e aplicado.
  - `test_source_dirs_detects_src_test` (`src/test/org/x/FooTest.java` com `package org.x;`) → `<testSourceDirectory>src/test</testSourceDirectory>`.
  - `test_fix_toggle_off` (`fixes_off={"compiler-java"}`) → não aplicado.
  - `test_diff_is_semantic` (POM com comentários e indentação estranha) → o diff só tem linhas das mudanças.
  - `test_junit5_only_warning`.
- [ ] **Step 2:** Rodar → FAIL. **Step 3:** Implementar. **Step 4:** Rodar → PASS.
- [ ] **Step 5:** Commit `feat: gerador de jaguar2-pom.xml com correções`.

### Task 11: Execução Jaguar 2 (com multi-módulo) e Jaguar 1

**Modelo:** Sonnet

**Files:**
- Create: `sbfl/run_jaguar2.py`, `sbfl/run_jaguar1.py`, `tests/test_run_jaguar.py`
- Modify: `sbfl/jaguar.py` (aceitar `jar_dir`, `java_exe` e `env` explícitos em vez de `cfg`, mantendo o laço de recuperação de crash)

**Interfaces:**
- Consumes: `AttemptConfig`, `AttemptResult`, `test_selection_args` (Task 9); `generate`/`write` (Task 10); `resolve` (Task 5).
- Produces:
  - `run_jaguar2(home, settings, prep, workdir, cfg: AttemptConfig, log) -> AttemptResult`. Passos: (1) se `prep.module != "."`, roda `toolenv.mvn("install","-DskipTests",*skip_flags)` na raiz com os POMs originais. Se falhar, devolve o resultado com `extra["fase"] = "reactor"`. (2) `generate` + `write` no módulo. (3) `toolenv.mvn("-f","jaguar2-pom.xml","clean","test", <flags de falha ignorada>, *skip_flags, *test_selection_args)`. (4) `outputs` com `jaguar2_csv`, `jaguar2_xml`, `jacoco_exec` (`target/jacoco.exec`), `surefire` e `pom`; `extra["pom_plan"] = PomPlan` como dict.
  - `prefetch_jaguar2(home, settings) -> bool`: com o perfil `jaguar2`, roda `mvn dependency:get -Dartifact=<g>:<a>:<v>` para os 5 artefatos do Jaguar 2, `org.jacoco:jacoco-maven-plugin:0.8.7`, `org.apache.maven.plugins:maven-surefire-plugin:3.5.3` e `junit:junit:4.12` no `m2/` próprio. Usado pelo botão "Preparar Jaguar 2 para uso offline" na tela Ambiente (Task 17).
  - `run_jaguar1(home, settings, prep, workdir, cfg, log) -> AttemptResult`: compila com o `toolenv.mvn("test-compile", ...)` do perfil `jaguar1` + `dependency:copy-dependencies`, monta a lista de classes (relevantes ou `scan_tests`) e chama `jaguar.run_jaguar` com `lib = settings jaguar1.lib or home.tools/"jaguar1/lib"`. `outputs["jaguar1_xml"]` e `extra["excluded_classes"]`.

- [ ] **Step 1: Testes que falham:**
  - `test_jaguar2_multimodule_installs_reactor_first` (`mvn` falso que registra as chamadas; projeto com `pom.xml` raiz + `core/pom.xml`; `module="core"`) → a 1ª chamada é `install -DskipTests` na raiz, a 2ª é `-f jaguar2-pom.xml ... test` em `core/`, e `core/jaguar2-pom.xml` existe enquanto a raiz não ganhou um.
  - `test_jaguar2_collects_outputs` (o fake cria `target/jaguar2.csv`) → `outputs["jaguar2_csv"]` existe.
  - `test_jaguar1_uses_existing_loop` (adaptado do `test_crash_recovery_loop` atual).
  - `test_jaguar2_real_selftest` (skipUnless mvn+java **e** JDK 11 encontrado; rede para o Maven Central) → `jaguar2.csv` existe para o projeto do selftest (`selftest_data/fixed` com o `Calc.java` buggy por cima).
- [ ] **Step 2:** Rodar → FAIL. **Step 3:** Implementar. **Step 4:** Rodar → PASS.
- [ ] **Step 5:** Commit `feat: execução Jaguar 2 e Jaguar 1`.

### Task 12: Diagnóstico e classificação de status

**Modelo:** Sonnet

**Files:**
- Create: `sbfl/diagnose.py`, `tests/test_diagnose.py`
- Modify: `sbfl/triage.py` (passa a importar as regras de `diagnose.py`, ou é removido se ficar sem uso; a decisão fica registrada no commit)

**Interfaces:**
- Consumes: `AttemptResult`, `sbfl.status`, `outputs.*`.
- Produces:
  - `@dataclass class Suggestion: fix: str | None; params: dict; jdk: int | None; maven: str | None; label: str` (PT).
  - `@dataclass class Diagnosis: status: str; cause: str; evidence: list[str]; suggestions: list[Suggestion]; warnings: list[str]`.
  - `RULES: list[Rule]`, com `Rule(status, pattern: str (regex), cause: str, suggestions: list[Suggestion])`. Mínimo, com estas causas → sugestões:
    - `Unsupported class file major version|invalid target release|release version \d+ not supported|UnsupportedClassVersionError` → `jdk_incompativel` → JDK 8 / JDK 17 / `compiler-java`.
    - `animal-sniffer|Undefined reference` → `plugin_incompativel` → `skip-animal-sniffer`.
    - `maven-surefire-plugin:2\.|SurefireBooterForkException|The forked VM terminated` → `plugin_incompativel` → `override-version` Surefire 3.2.5 / 3.0.0-M5.
    - `maven-compiler-plugin.*(Fatal error compiling|NoSuchMethodError)` → `plugin_incompativel` → compiler 3.1 / 3.11.0.
    - `Could not resolve dependencies|Could not transfer artifact|Blocked mirror for repositories` → `dependencia_nao_resolvida` → Maven 3.6.3 (para http bloqueado).
    - `duplicate class` → `classes_duplicadas` → `exclude-duplicates` com os arquivos extraídos da mensagem (sempre exige confirmação na UI).
    - `COMPILATION ERROR|cannot find symbol` → `compilacao_falhou` → `source-dirs`, `junit4`.
    - `No tests (were )?(executed|to run)|Tests run: 0` → `testes_nao_encontrados` → `source-dirs`, `junit4`, escopo `all`.
  - `classify(kind: str, res: AttemptResult) -> Diagnosis`. Ordem: `timed_out` → `timeout`. Depois, sucesso por tipo: `maven` = surefire não vazio; `jaguar2` = `jaguar2.csv` **existe** (mesmo vazio); `jaguar1` = `jaguar1_xml`. No sucesso, `ok_testes_falhando` se houver failed/error, senão `ok`. Se for `jaguar2`, o CSV estiver vazio e não houver falhas: aviso "nenhum teste falhou: relatório vazio (defeito não reproduzido)". Sem sucesso: primeira regra que casar no log (lido do fim, até 400 KB); Jaguar 2 com build ok e sem CSV → `jaguar_sem_saida`; Jaguar 1 com `meta.status == "jaguar_crash"` → `jaguar_crash`; senão `desconhecido`. `evidence` = até 8 linhas do log em volta do match.

- [ ] **Step 1: Testes que falham:** um teste por regra (com um trecho de log real ou plausível); `test_test_failures_are_success` (surefire com failures → `ok_testes_falhando` e `suggestions == []`); `test_empty_jaguar2_csv_no_failures_is_ok_with_warning` (Review Focus 1); `test_timeout_wins`.
- [ ] **Step 2:** Rodar → FAIL. **Step 3:** Implementar. **Step 4:** Rodar → PASS.
- [ ] **Step 5:** Commit `feat: diagnóstico em português das falhas de infraestrutura`.

### Task 13: Receitas, busca automática e orquestrador `execute`

**Modelo:** Sonnet

**Files:**
- Create: `sbfl/recipes.py`, `sbfl/autosearch.py`, `sbfl/execute.py`, `tests/test_execute.py`

**Interfaces:**
- Consumes: tudo do Marco 3, `RunWriter`, `prepare`/`working_copy`.
- Produces:
  - `@dataclass class Recipe: match: str | list[str]; kind: str; fixes: list[str]; params: dict; jdk: int | None; maven: str | None; note: str; source: Path`.
  - `recipes.find(home, bug, kind) -> Recipe | None`. Prioridade: lista/ID exato > glob (`fnmatch` no `bug_id`) > `"*"`. Arquivo: `home.recipes/<benchmark>/<projeto>.toml` com tabelas `[[recipe]]` e chaves `match, kind, fixes, params, jdk, maven, note`.
  - `recipes.save(home, bug, kind, scope: "bug" | "glob" | "project", cfg: AttemptConfig, glob: str | None = None, note: str = "") -> Path` (acrescenta; não reescreve as receitas existentes).
  - `autosearch.candidates(first: AttemptConfig, diag: Diagnosis, kind: str, settings) -> list[AttemptConfig]`: primeiro as sugestões do diagnóstico (exceto `exclude-duplicates`), depois Surefire `3.5.3→3.2.5→3.0.0-M5` (`override-version`), compiler `3.8.0→3.1→3.11.0` (param de `compiler-java`) e JDK `11→17→8`. Deduplicado por `key()` e cortado em `run.max_attempts - 1`.
  - `@dataclass class RunOptions: test_scope: str | None = None; fixes_on: frozenset | None = None; fixes_off: frozenset = frozenset(); params: dict = {}; jdk: int | None = None; maven: str | None = None; auto_search: bool = True; archive_checkout: bool | None = None; parent: str | None = None; log_cb: Callable[[str], None] | None = None`.
  - `execute(home, settings, bug: Bug, kind: str, opts: RunOptions) -> str` (devolve o `rel` da execução). Fluxo: `RunWriter` → `prepare` (log em `attempts/0-prepare.log`; falha → `checkout_falhou`) → 1ª `AttemptConfig` = receita (se houver) + overrides de `opts` → para cada tentativa: `working_copy` → `run_*` → `classify` → grava `attempts/<n>/{log.txt.gz, attempt.json (config, diagnosis, command, toolenv)}`. Se for sucesso, ou falha com `auto_search=False`, para. Senão, segue `candidates` → copia as saídas puras da **última** tentativa para `outputs/` (`surefire-reports/`, `jaguar2.csv`, `jaguar2.xml`, `jacoco.exec`, `jaguar1.xml`, `jaguar1.log.gz`), o POM e o diff para `pom/`, grava `env.json` (`envinfo.snapshot` + `toolenv.describe()` + commit) e `command.txt`, arquiva o checkout se ligado, apaga a cópia de trabalho e chama `finish` com `status`, `diagnosis`, `tests` (counts), `test_scope`, `relevant_tests`, `recipe` e `attempt_configs`.
  - O `log_cb` recebe cada linha do log ao vivo (para SSE). Isso exige que `proc.run` aceite `on_line: Callable[[str], None] | None` (modificar `sbfl/proc.py`).

- [ ] **Step 1: Testes que falham** (com `mvn` falso controlado por variável de ambiente para falhar N vezes com "animal-sniffer"):
  - `test_execute_success_first_try` → uma tentativa e status `ok_testes_falhando`.
  - `test_autosearch_stops_at_first_success` → falha 1x e passa na 2ª com `skip-animal-sniffer`; o `run.json` tem `attempts == 2`.
  - `test_autosearch_disabled_stops` → uma tentativa e status `plugin_incompativel`.
  - `test_attempt_cap` → nunca passa de `max_attempts`.
  - `test_recipe_applied_and_priority` (receitas `*` e de ID exato) → a de ID exato vence e os fixes aparecem na 1ª tentativa.
  - `test_recipe_save_appends`.
  - `test_test_failures_never_trigger_search`.
- [ ] **Step 2:** Rodar → FAIL. **Step 3:** Implementar. **Step 4:** Rodar → PASS.
- [ ] **Step 5:** Commit `feat: orquestrador com receitas e busca automática`.

---

## Marco 4 — Comparação, fila, exportação e CLI

### Task 14: Comparações

**Modelo:** Sonnet

**Files:**
- Modify: `sbfl/compare.py` (reescrito), `tests/test_core.py` (testes antigos de compare migram para `tests/test_compare.py`)
- Create: `tests/test_compare.py`

**Interfaces:**
- Consumes: `outputs.*`, `store.load_run/run_dir/list_runs`, `diffgt.select_lines`, `outputs.outer`.
- Produces:
  - `compare_tests(a: dict, b: dict) -> dict` (semântica atual, com chaves genéricas `only_a`, `only_b`, `outcome_differs`, `a_failing`, `b_failing`, `a_counts`, `b_counts`).
  - `compare_spectra(a: list[LineScore], b: list[LineScore]) -> dict`: chave `(outer(cls), line)`; devolve `only_a`, `only_b`, `both: int` e `diffs` (lista de `{cls, line, a: {cef,cep,susp}, b: {...}}` onde algum difere; susp com tolerância 1e-6) e `note` (PT: "Jaguar 2 só grava linhas com susp > 0").
  - `fault_positions(scores: list[LineScore], faults: list[FaultLine]) -> dict` (mesma saída do `jaguar_rank` atual: `total`, `lines[{cls,line,susp,rank_best,rank_worst,tied,cef,cep,cnf,cnp}]`, `not_covered`).
  - `run_tests(home, rel) -> dict[str,str]`: Maven e Jaguar 2 → surefire de `outputs/`; Jaguar 1 → `parse_jaguar1_log`.
  - `run_scores(home, rel) -> list[LineScore] | None`.
  - `compare_runs(home, rel_a, rel_b) -> dict` com `{"tests": compare_tests, "spectra": compare_spectra | None, "faults_a", "faults_b"}`. A verdade vem do `Prepared` salvo em `run.json` (`truth`), quando existir.
  - `batch_summary(home, runs: list[dict]) -> list[dict]` por `(benchmark, project, kind)`: `bugs`, `ok`, `ok_testes_falhando`, `falhas: {status: n}` e `taxa_sucesso` (= bugs cuja **última** execução daquele tipo teve sucesso / bugs tentados).
  - `summary_csv(rows) -> str`.

- [ ] **Step 1: Testes que falham:** `test_compare_tests_generic`, `test_compare_spectra_only_and_diffs`, `test_fault_positions_ties` (mesmos números do teste antigo de `jaguar_rank`), `test_batch_summary_uses_last_run_per_bug`, `test_compare_runs_maven_vs_jaguar2` (pastas de execução sintéticas).
- [ ] **Step 2:** Rodar → FAIL. **Step 3:** Implementar. **Step 4:** Rodar → PASS.
- [ ] **Step 5:** Commit `feat: comparações Maven×Jaguar, Jaguar1×Jaguar2 e resumo de lote`.

### Task 15: Fila persistente, worker e exportação de dataset

**Modelo:** Sonnet

**Files:**
- Create: `sbfl/queue.py`, `sbfl/dataset.py`, `tests/test_queue.py`, `tests/test_dataset.py`

**Interfaces:**
- Consumes: `execute`, `RunOptions`, `bug_from_key`, `store.*`, `power.keep_awake`, `compare.batch_summary`.
- Produces:
  - Tabelas `batches(id INTEGER PK, name, created, kinds TEXT(json), options TEXT(json), state)` e `jobs(id INTEGER PK, batch_id, idx, bug_key, bug_extra TEXT(json), kind, state, run_rel, error)`. Estados de job: `pending | running | done | cancelled`. Estados de lote: `active | paused | cancelled | done`.
  - `create_batch(home, name: str, bugs: list[Bug], kinds: list[str], opts: dict) -> int`. Uma execução avulsa da UI é um lote de 1 job.
  - `class Worker(threading.Thread)`: `__init__(home, settings)`, `pause(batch_id)`, `resume(batch_id)`, `cancel(batch_id)`, `stop()`, `current() -> dict | None` (job atual + últimas 500 linhas do log) e `subscribe() -> queue.Queue[str]` (linhas ao vivo). Ao iniciar, chama `store.recover_partials` e devolve jobs `running` para `pending` (Review Focus 3). Processa um job por vez com `power.keep_awake()` enquanto houver fila.
  - `batch_status(home, batch_id) -> dict` (contagens por estado, ETA pela média de duração dos jobs feitos, `summary = compare.batch_summary`).
  - `dataset.export(home, name: str, bugs: list[Bug] | None = None, batch_id: int | None = None) -> Path`: cria `home.datasets/<name>/` com o layout de §8.11 (`README.md`, `manifest.csv`, `manifest.json` com `schema_version: 1`) e copia, para cada bug, as saídas puras da última execução Jaguar 2 bem-sucedida. Bugs sem sucesso entram no manifest com o status da última tentativa.

- [ ] **Step 1: Testes que falham** (com `execute` mockado):
  - `test_worker_processes_in_order`.
  - `test_resume_after_crash` (job `running` no banco + `.partial` órfã → worker novo: o job roda de novo e a parcial vira `interrompido`).
  - `test_pause_and_cancel`.
  - `test_dataset_manifest_columns` (colunas exatas: `benchmark,project,bug,status,test_scope,n_tests,n_failed,jdk,maven,jaguar2_version,recipe,path`).
  - `test_dataset_includes_failed_bugs_status`.
- [ ] **Step 2:** Rodar → FAIL. **Step 3:** Implementar. **Step 4:** Rodar → PASS.
- [ ] **Step 5:** Commit `feat: fila retomável e exportação de dataset`.

### Task 16: CLI `jflp`

**Modelo:** Haiku

**Files:**
- Modify: `sbfl/cli.py` (reescrito), `sbfl/__main__.py`
- Create: `tests/test_cli.py`

**Interfaces:**
- Consumes: todo o núcleo.
- Produces: `main(argv: list[str] | None = None) -> int` com os subcomandos:
  - `ui [--port N] [--no-browser] [--window]` (implementado na Task 17; aqui só repassa).
  - `doctor` (lista detecções e o perfil resolvido de cada tipo, marcando `[OK]`/`[!!]`/`[XX]`).
  - `tools list | tools get jdk <major> | tools get maven <versão> | tools get jaguar1` (mostra URL e tamanho e pede `s/N`; `--yes` pula a pergunta).
  - `bugs <d4j|bugsjar> [projeto]`.
  - `prepare <bug>`.
  - `run <maven|jaguar2|jaguar1> <bug> [--all-tests] [--fix ID]... [--no-fix ID]... [--jdk N] [--maven V] [--no-search] [--no-archive]`.
  - `runs [bug]`.
  - `compare <rel_a> <rel_b>`.
  - `batch create <nome> <kind,...> <bug>... | batch run | batch status <id>`.
  - `export <nome> (--batch ID | <bug>...)`.
  - `recipes [benchmark/projeto]`.
  - `reindex`.
  - `selftest`.
  - `<bug>` é `Bug.key` (`bugsjar/maven/bugs-dot-jar_MNG-1_abc`, `d4j/Jsoup/93`) ou `local:<pasta>` / `local:<url>@<commit>`. Saída em PT; código de saída 0 em sucesso (inclui `ok_testes_falhando`).
  - O `selftest` vira: preparar `selftest_data` como bug local e rodar `maven` + `jaguar2` (+ `jaguar1` se houver lib), verificando que a linha defeituosa aparece no CSV do Jaguar 2 (`selftest.bug_line()`).

- [ ] **Step 1: Testes que falham:** `test_bug_spec_parsing` (as 4 formas), `test_run_invokes_execute_with_options` (mock), `test_doctor_runs_without_tools` (sem JDK → rc 1 e mensagens PT, sem exceção).
- [ ] **Step 2:** Rodar → FAIL. **Step 3:** Implementar. **Step 4:** Rodar → PASS, e `jflp --help` lista os comandos.
- [ ] **Step 5:** Commit `feat: CLI jflp`.

---

## Marco 5 — Interface

Todas as páginas: HTML server-side com Jinja2 e htmx 2.0.4 vendorizado em `sbfl/web/static/htmx.min.js` (baixado uma vez na implementação de `https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js` e commitado). CSS próprio em `app.css`, com tokens de cor em `:root` e tema escuro via `prefers-color-scheme`. Layout sóbrio: barra lateral com Painel, Ambiente, Bugs, Lotes, Comparar, Receitas e Exportar. Textos em PT.

### Task 17: Esqueleto da app, segurança e tela Ambiente

**Modelo:** Sonnet

**Files:**
- Create: `sbfl/web/__init__.py`, `sbfl/web/app.py`, `sbfl/web/routes_env.py`, `sbfl/web/templates/{base,painel,ambiente}.html`, `sbfl/web/static/{htmx.min.js,app.css}`, `tests/test_web_base.py`
- Modify: `sbfl/cli.py` (comando `ui`)

**Interfaces:**
- Produces:
  - `create_app(home, settings, worker: Worker, token: str) -> FastAPI`. Middleware: rejeita (403) requisição sem o cookie `jflp_token == token`, exceto `GET /?t=<token>`, que grava o cookie e redireciona para `/`. Rejeita `Host` diferente de `127.0.0.1:<porta>`/`localhost:<porta>` (contra DNS rebinding). Templates em `app.state.templates`.
  - `serve(home, port: int = 0, open_browser: bool = True, window: bool = False) -> None`: escolhe uma porta livre, sobe o Uvicorn em `127.0.0.1`, inicia o `Worker`, imprime e abre `http://127.0.0.1:<p>/?t=<token>` (`webbrowser` ou `pywebview` se `window` e disponível).
  - Painel `/`: job atual, fila e últimas 10 execuções.
  - `/ambiente`: tabela de JDKs, Mavens, Git, WSL, Defects4J e Jaguar 1 (lib), com o perfil resolvido para cada tipo de execução ou o `MissingTool` em PT. Botão "Baixar" → `GET /ambiente/plano?tipo=jdk&v=11` mostra URL, tamanho e checksum num diálogo de confirmação → `POST /ambiente/baixar` roda `fetch` em thread com progresso via polling htmx (`/ambiente/progresso/<id>`). Formulário para apontar pastas (grava em `settings.tools`).

- [ ] **Step 1: Testes que falham** (`fastapi.testclient.TestClient`): `test_requires_token` (403 sem cookie), `test_token_sets_cookie`, `test_bad_host_rejected`, `test_ambiente_renders_without_tools` (detecção mockada vazia → 200 com "não encontrado").
- [ ] **Step 2:** Rodar → FAIL. **Step 3:** Implementar. **Step 4:** Rodar → PASS. Depois, `jflp ui` abre no navegador e o Painel e o Ambiente aparecem (verificar no browser pane com screenshot).
- [ ] **Step 5:** Commit `feat: app web local com tela Ambiente`.

### Task 18: Bugs, execução, revisão do POM e log ao vivo

**Modelo:** Sonnet

**Files:**
- Create: `sbfl/web/routes_bugs.py`, `sbfl/web/templates/{bugs,bug,revisao_pom,execucao_viva}.html`, `tests/test_web_bugs.py`

**Interfaces:**
- Consumes: `list_bugs`, `local_bug`, `prepare`, `generate`, `recipes.find`, `create_batch`, `Worker.subscribe/current`, `store.list_runs`.
- Produces as rotas:
  - `GET /bugs?bench=&proj=`: seletor benchmark → projeto → lista de bugs com checkbox, status da última execução por tipo e filtro "sem execução ok". Ações: "Rodar selecionados…" (vai para Lotes) e o formulário "Projeto local" (pasta ou URL + commit).
  - `GET /bug/{key:path}`: linha do tempo das execuções e botões "Rodar Maven", "Revisar e rodar Jaguar 2" e "Rodar Jaguar 1", mais escopo de testes (relevantes/todos) e "busca automática" (ligada).
  - `GET /bug/{key}/jaguar2`: prepara (se preciso) e mostra o `PomPlan` (diff colorido, lista de correções com checkbox e motivo, detectadas pré-marcadas, receita aplicada e avisos). `POST` enfileira com `fixes_on`/`fixes_off`/`params`.
  - `GET /job/{id}/ao-vivo`: página com o log via SSE (`/job/{id}/stream`, `text/event-stream` alimentado por `Worker.subscribe`). Ao terminar, redireciona para a execução.

- [ ] **Step 1: Testes que falham:** `test_bug_page_lists_runs`, `test_pom_review_shows_detected_fixes` (projeto fixture com animal-sniffer), `test_post_enqueues_job_with_fixes`, `test_sse_stream_yields_lines` (worker falso).
- [ ] **Step 2:** Rodar → FAIL. **Step 3:** Implementar. **Step 4:** Rodar → PASS. Depois, no browser pane: rodar o selftest local pela UI e ver o log ao vivo.
- [ ] **Step 5:** Commit `feat: telas de bugs, revisão do POM e log ao vivo`.

### Task 19: Execução, comparação, lotes, receitas e exportação

**Modelo:** Sonnet

**Files:**
- Create: `sbfl/web/routes_runs.py`, `sbfl/web/templates/{execucao,comparar,lotes,lote,receitas,exportar}.html`, `tests/test_web_runs.py`

**Interfaces:**
- Consumes: `store`, `compare.*`, `outputs.*`, `diagnose`, `recipes.save`, `queue.*`, `dataset.export`.
- Produces as rotas:
  - `GET /run/{rel:path}`: status, diagnóstico (causa, evidência, sugestões com botão "Tentar de novo com esta correção", que enfileira um novo run com `parent=rel`; `exclude-duplicates` pede confirmação extra), tentativas, contagem de testes, lista de falhas, tabela de linhas (classe, linha, cef/cep/cnf/cnp, susp; ordenável e filtrável no servidor via htmx, paginada de 200 em 200; linhas do defeito destacadas), navegador de `outputs/` (download de cada arquivo), log com busca (`/run/{rel}/log?q=`), botões "Abrir pasta" (`os.startfile` / `xdg-open`), "Exportar zip" e "Salvar como receita" (escopo bug/glob/projeto) quando o sucesso veio após correções.
  - `GET /comparar?a=&b=`: seletores de execução (pré-preenchidos a partir da página do bug: "Maven × Jaguar 2", "Jaguar 1 × Jaguar 2"), resultado de `compare_runs` em tabelas.
  - `GET/POST /lotes`: criar lote a partir da seleção de `/bugs` (tipos, escopo, busca automática, arquivar checkout), lista de lotes; `GET /lote/{id}`: progresso, ETA, pausar/retomar/cancelar, `batch_summary` em tabela e "Baixar CSV".
  - `GET/POST /receitas`: lista por benchmark/projeto e edição do TOML num textarea (validado com `tomllib` antes de salvar).
  - `GET/POST /exportar`: escolher lote ou seleção e nome → `dataset.export` → link da pasta.

- [ ] **Step 1: Testes que falham:** `test_run_page_renders_synthetic_run`, `test_retry_enqueues_with_parent_and_fix`, `test_save_recipe_writes_toml`, `test_compare_page`, `test_batch_csv_download`, `test_invalid_recipe_toml_rejected_pt`.
- [ ] **Step 2:** Rodar → FAIL. **Step 3:** Implementar. **Step 4:** Rodar → PASS.
- [ ] **Step 5:** Commit `feat: telas de execução, comparação, lotes, receitas e exportação`.

### Task 20: Lançadores, README e aceitação

**Modelo:** Haiku (scripts e README); a verificação de aceitação é feita pelo coordenador.

**Files:**
- Create: `jflp.ps1`, `jflp.cmd`, `jflp.sh`
- Modify: `README.md` (reescrito em PT, focado no Studio: instalar, primeira execução, telas, CLI, receitas, layout de `runs/` e do dataset, solução de problemas), `.gitignore`
- Delete: `config.example.toml`, `docs/COMPARACAO_COM_E_SEM_JAGUAR.md` (conteúdo útil vai para o README)

**Interfaces:**
- `jflp.ps1`/`jflp.sh`: na 1ª execução criam `.venv` ao lado do script com o Python ≥3.11 encontrado (`py -3.11+` no Windows, `python3` no Arch) e rodam `pip install -e .[full]` (se o extra falhar, instalam sem ele). Depois executam `jflp <args>`; sem argumentos, `jflp ui`. `jflp.cmd` chama `powershell -ExecutionPolicy Bypass -File jflp.ps1 %*`.

- [ ] **Step 1:** Escrever os scripts e o README.
- [ ] **Step 2:** Verificar `./jflp.ps1 --help` num clone limpo (pasta temporária) e, se houver Arch/WSL disponível, `./jflp.sh --help`. Senão, registrar como não verificado.
- [ ] **Step 3: Aceitação (spec §12)**, pela UI no browser pane, registrando o resultado de cada item:
  1. Ambiente: com `JFLP_HOME` vazio, baixar o Maven 3.9.9 pela UI (JDK 11 já existe nesta máquina; testar o download de JDK 17 para cobrir o fluxo).
  2. Jsoup 93b: como o WSL não está instalado aqui, usar `local:https://github.com/jhy/jsoup.git@<commit buggy do D4J Jsoup-93>` (obter o commit em `defects4j/framework/projects/Jsoup/active-bugs.csv` no GitHub). Rodar Jaguar 2 e conferir `jaguar2.csv` não vazio sem editar POM à mão.
  3. Bugs.jar: `fetch` de um projeto pequeno (ex.: `commons-math`) e um bug com Maven, Jaguar 1 e Jaguar 2, mais as duas comparações.
  4. Bug com testes falhando → `ok_testes_falhando`, sem retry.
  5. Animal-sniffer: o fixture da Task 10 como projeto local com a correção desligada → diagnóstico em PT, busca resolve, salvar receita, reaplicada no próximo.
  6. Lote de ≥10 bugs Bugs.jar; fechar e reabrir a UI no meio; CSV do resumo.
  7. Pastas imutáveis conferidas. 8. Módulo de projeto multi-módulo do Bugs.jar com Jaguar 2. 9. Exportar dataset do lote.
  - Itens que dependem de WSL/Defects4J nativo ficam marcados como "não verificados nesta máquina".
- [ ] **Step 4:** Commit `docs: README do JFLP Studio e lançadores`.
