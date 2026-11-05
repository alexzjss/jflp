# JFLP Studio — sincronização entre computadores (two-way sync pela planilha)

Data: 2026-11-05. Base: `main` (0751ed3). Branch: `feat/sincronia`.

## Intenção

Dois (ou mais) computadores rodam a cobertura do bugs-dot-jar apontando para a **mesma planilha** (mesma URL do app da
web do Apps Script, mesmo token). Hoje o envio é só de ida (`planilha.py`: caixa de saída SQLite → POST → aba "Bugs").
Queremos:

1. **Puxar**: quando o computador A conclui um bug, o computador B recebe a **execução completa** (pasta da execução:
   `run.json`, `outputs/`, etc.) e a importa como execução normal; o job local sai da fila e as telas de ranking/código
   funcionam iguais.
2. **Coordenar (claims)**: antes de começar um bug da cobertura, o worker **reserva** o bug no hub; se outra máquina já
   reservou, pula para o próximo. Sem rede ou sem hub configurado, roda como hoje.

Decisões do usuário: transporte = **Apps Script como hub** (reaproveita URL/token); fidelidade = **execução completa**;
coordenação = **sim, com claims**.

## Identidade da máquina

`config.toml`, seção `[planilha]`, chaves novas (em `home.DEFAULTS`):

| chave            | padrão  | uso                                                                 |
|------------------|---------|---------------------------------------------------------------------|
| `sincronizar`    | `true`  | liga puxar + pacotes + claims (só vale com `url` configurada)       |
| `maquina_id`     | `""`    | gerado uma vez (`secrets.token_hex(8)`) e gravado                   |
| `maquina_nome`   | `""`    | nome legível; vazio = `socket.gethostname()`                        |
| `puxar_cada_s`   | `120`   | intervalo entre puxadas                                             |
| `reserva_ttl_s`  | `1800`  | validade de uma reserva; renovada a cada `ttl/3` enquanto roda      |

## Protocolo do hub (Apps Script, versão 2)

Um único `doPost`; o corpo JSON sempre tem `token`. O campo `acao` escolhe a operação; **sem `acao` = `"linhas"`**
(compatível com o app antigo). Toda resposta é JSON com `ok` (bool) e `versao: 2`; em erro, `erro` (texto curto).
`doGet` responde `{ok: true, versao: 2, mensagem}`. Toda operação que escreve roda sob `LockService.getScriptLock()`.

Um app novo falando com um script **antigo** recebe `{ok:false, erro:"formato"}` (sem `versao`) nas ações novas: o
cliente então desliga a sincronia nesta sessão e mostra "atualize o Apps Script (Implantar > Gerenciar implantações >
Nova versão)". O envio de linhas segue funcionando.

### Armazenamento no Google

- Aba **"Bugs"** (visível): igual a hoje, com uma coluna nova **"Máquina"** no fim de `COLUNAS` (nome da máquina que
  produziu cada etapa, ex. `maven: pc-lab, J1: notebook`).
- Aba oculta **"_sync"**: uma linha por bug: `chave | seq | dados` onde `dados` é JSON
  `{kind: {estado, maquina, maquina_nome, revisao, run_rel, pacote}}` (`pacote` = objeto descrito abaixo ou `null`).
- Propriedade do script `JFLP_SEQ`: contador monotônico. Toda mudança efetiva numa linha de `_sync` faz
  `seq = ++JFLP_SEQ` naquela linha.
- Pasta do Drive **"JFLP pacotes"** (id em `JFLP_PASTA`, criada sob demanda, ao lado da planilha): arquivos
  `<sha256>.parte<i>` com os pedaços (base64 decodificado = bytes do zip).
- Reservas: propriedades do script `r:<chave>` = JSON `{maquina, maquina_nome, expira}` (`expira` em ms, relógio do
  servidor). Reservas vencidas são removidas a cada `reservar`.

### `acao: "linhas"` (existente, estendida)

Corpo: `{token, colunas, linhas, maquina?, maquina_nome?, sync?}`.
- `colunas`/`linhas`: como hoje (upsert na aba "Bugs").
- `sync` (opcional): `{chave: {kind: {estado, revisao, run_rel}}}` — só as etapas **concluídas e produzidas por esta
  máquina** (`estado` ∈ `ok`, `falhou`, `nao_exec`, que são os valores de `cobertura.SIT_*`; `revisao` = ISO do fim da
  execução). Para cada uma: se `_sync[chave][kind]` não existe, ou tem outro `run_rel`, ou outra `revisao`, grava
  `{estado, maquina, maquina_nome, revisao, run_rel, pacote: null}` e incrementa o `seq` da linha. Se for igual, não
  faz nada (sem `seq` novo: evita eco).
Resposta: `{ok, versao, n, cabecalho_mudou?}`.

### `acao: "pacote"` (upload de um pedaço)

Corpo: `{token, maquina, chave, kind, run_rel, sha256, parte, partes, dados_b64}`. Pedaços de no máximo **8 MiB** de
bytes (≈ 11 MiB em base64), numerados `0..partes-1`.
- Grava o pedaço na pasta (se já existe um arquivo com o mesmo nome, reaproveita: upload idempotente).
- Quando todos os `partes` pedaços de `sha256` existem: `_sync[chave][kind].pacote = {sha256, partes, ids:[fileId...],
  bytes}` **se** `_sync[chave][kind].run_rel == run_rel` e `.maquina == maquina` (senão a resposta tem
  `obsoleto: true` e nada muda), e incrementa o `seq` da linha.
Resposta: `{ok, versao, completo: bool, obsoleto?: bool}`.

### `acao: "puxar"`

Corpo: `{token, maquina, cursor, limite?}` (`limite` padrão 200, máx 500).
- Lê `_sync`, filtra `seq > cursor`, ordena por `seq`, devolve até `limite` linhas.
- Também devolve as reservas vivas de **outras** máquinas.
Resposta: `{ok, versao, cursor: <maior seq devolvido, ou o próprio cursor>, mais: bool,
mudancas: [{chave, seq, dados}], reservas: {chave: {maquina, maquina_nome, expira_em_s}}}`
(`expira_em_s` = segundos que faltam, para não depender do relógio do cliente). O cliente ignora as etapas cuja
`maquina` é ele mesmo.

### `acao: "baixar"`

Corpo: `{token, id}` (um `fileId` de `pacote.ids`). Resposta: `{ok, versao, dados_b64}`.

### `acao: "reservar"`

Corpo: `{token, maquina, maquina_nome, chaves: [...], ttl_s}`.
Para cada chave: concede se não há reserva viva ou se a reserva é da própria máquina (renova `expira = agora+ttl`).
Resposta: `{ok, versao, concedidas: [...], negadas: {chave: {maquina, maquina_nome, expira_em_s}}}`.

### `acao: "liberar"`

Corpo: `{token, maquina, chaves}`. Remove as reservas dessas chaves que são da própria máquina. Resposta `{ok, versao, n}`.

## Lado do app (Python)

### Módulo novo `sbfl/sincronia.py`

- `Hub(url, token, post=planilha.post_http)`: métodos `puxar`, `pacote`, `baixar`, `reservar`, `liberar`; levanta
  `HubAntigo` quando a resposta não tem `versao >= 2` e `erro == "formato"`; `RuntimeError` para `ok: false`.
- `maquina(settings) -> (id, nome)`: gera/grava `maquina_id`, nome padrão = hostname.
- **Pacote**: `montar_pacote(home, run_rel) -> bytes` = zip (deflate) da pasta da execução **sem** `outputs/jacoco.exec`
  e sem `.writer.json`; `aplicar_pacote(home, job, dados_zip, origem_info) -> rel` = extrai num temporário (rejeita
  caminhos absolutos ou com `..`), grava com `store.RunWriter(home, bug, kind)` (`copy_into` de cada arquivo) e
  `finish(run)` onde `run` = `run.json` do pacote + `origem: "remoto"` + `remoto_de: {maquina, maquina_nome, run_rel}`.
- Tabelas SQLite novas (no `jflp.db`):
  - `sync_estado(chave TEXT PRIMARY KEY, valor TEXT)`: `cursor:<url>` etc.
  - `sync_pacotes(run_rel TEXT PRIMARY KEY, bug_key TEXT, kind TEXT, sha256 TEXT, partes INT, enviadas INT,
    enviado TEXT, erro TEXT)`: fila de upload de pacotes (uma linha por execução local concluída da cobertura).
- **Aplicar mudanças remotas** (`aplicar_mudancas(home, settings, hub, mudancas)`): para cada `chave`/`kind` com
  `pacote` completo e `maquina != eu`:
  - acha o job do lote de cobertura (`cobertura.batch_id`); sem job local (bug que não existe aqui): ignora.
  - **livre** (`state in ('pending','cancelled')`, sem `run_rel`) → baixa os pedaços, confere o sha256, aplica e marca
    `UPDATE jobs SET state='done', run_rel=?, error=NULL, seconds=?, origem='remoto' WHERE id=? AND state IN
    ('pending','cancelled')` (+ `queue._close_if_finished`); se o UPDATE não pegou, apaga a execução recém-gravada
    (mesmo padrão de `importar._publicar`).
  - **já concluído localmente**: só substitui se a etapa local é `falhou` e a remota é `ok` (o melhor resultado vence).
    Em qualquer outro caso mantém o local.
  - `running` localmente: não mexe (o local termina e decide).
  - Depois de aplicar, chama `planilha.enfileirar(home, [chave])` para a linha da planilha refletir o estado (as etapas
    remotas não entram no `sync` enviado — ver abaixo — então não há eco).
- **Reservas**: `Reservas(hub, settings)` com `reservar(chave) -> bool` (rede falhou ⇒ `True`, degrada), memória das
  chaves reservadas e `renovar_e_liberar(home)`: renova as chaves com job `running` local; libera as que não têm mais
  job `running` nem `pending` local.
- **Thread `Sincronizador`** (como o `Enviador`: daemon, `acordar()`, `stop()`, `status()`, backoff exponencial em
  falha, nunca morre). A cada ciclo, com URL configurada e `sincronizar = true`:
  1. sobe até N pedaços pendentes de `sync_pacotes` (um POST por pedaço);
  2. a cada `puxar_cada_s`: `puxar` em laço enquanto `mais`, aplica as mudanças, grava o cursor; atualiza a memória de
     reservas remotas (`jobs.remoto_ate`, ver abaixo);
  3. renova/libera reservas a cada `reserva_ttl_s/3`.
  `status()` = `{ultimo_puxar, aplicadas, pacotes_pendentes, reservas_remotas, erro, hub_antigo}`.

### `planilha.py`

- `COLUNAS` ganha `"Máquina"` no fim (o hub já sinaliza `cabecalho_mudou` → reenvio de tudo).
- O `Enviador.ciclo` passa a mandar `maquina`, `maquina_nome` e `sync` no corpo: para cada linha enviada, as etapas
  concluídas do bug cujo job **não** tem `origem` começando com `remoto` (as `importado` são produção desta máquina e
  **entram**). Isso exige guardar o `sync` por linha na caixa de saída (coluna nova
  `planilha_outbox.sync TEXT`, JSON, montado junto com a linha em `_gravar`).
- Toda execução local concluída da cobertura que entra no `sync` também entra em `sync_pacotes` (se ainda não está),
  para o `Sincronizador` subir o pacote.

### `queue.py` (claims no worker)

- Coluna nova `jobs.remoto_ate REAL` (epoch s): até quando o bug está reservado por outra máquina.
- `_claim_rules` ganha `AND (alias.remoto_ate IS NULL OR alias.remoto_ate < :agora)` — passar o agora como literal
  numérico na string (as regras são concatenadas no SQL; manter o padrão atual).
- Gancho `Worker.reservar_remoto` (o pool repassa, como `on_bug_change`): depois da reivindicação atômica de um job **de
  lote de cobertura**, fora da conexão, chama `reservar_remoto(bug_key)`. Se `False`: `UPDATE jobs SET state='pending'
  WHERE id=? AND state='running'`, e `UPDATE jobs SET remoto_ate=? WHERE bug_key=? AND batch_id=?` (agora + ttl) e tenta
  o próximo job (até 5 vezes por chamada de `_next_job`). Exceção no gancho ⇒ trata como `True`.
- O `Sincronizador`, ao puxar, grava `remoto_ate` para as reservas remotas vivas (e zera as que sumiram), assim o worker
  nem tenta os bugs ocupados.

### UI / CLI

- Ambiente › Planilha: nome da máquina (editável), estado da sincronia (último puxar, execuções recebidas, pacotes a
  enviar, bugs reservados por outras máquinas, erro, aviso "atualize o Apps Script"), botão "Sincronizar agora".
- Painel da cobertura: "N bugs rodando em outra máquina" quando houver.
- CLI: `jflp cobertura sincronizar` (um ciclo completo de puxar/subir, imprime o resumo); o `cobertura rodar` da CLI
  liga o `Sincronizador` e as reservas como o app.

## Testes

- `tests/fake_hub.py`: implementação em Python, em memória, do protocolo v2 (inclui `linhas` mínima), usada como `post`.
- `tests/test_sincronia.py`: duas `Home` com o mesmo lote de cobertura (fixture `bugsjar_fixture`) ligadas ao mesmo
  `FakeHub`: A conclui um bug → A envia linhas + pacote → B puxa → o job de B fica `done` com `origem='remoto'`, a
  execução de B tem os mesmos `outputs/` e `compare.run_scores` igual. Casos: eco não incrementa `seq`; conflito
  (local ok fica; local falhou + remoto ok troca); pacote com `..` recusado; sha errado recusado; hub antigo; reservas
  (A reserva, B é negado e o job volta a `pending` com `remoto_ate`; rede caída ⇒ roda).
- `tools/planilha/teste_code_gs.js` (node, sem dependências): stubs de `SpreadsheetApp`, `PropertiesService`,
  `LockService`, `DriveApp`, `Utilities`, `ContentService` e o mesmo roteiro de casos sobre o `Code.gs` real.

## Fora de escopo

- Sincronizar ações manuais ("marcar não executável", "refazer", prioridade).
- Apagar pacotes antigos do Drive.
- Mais de um lote de cobertura por máquina.
