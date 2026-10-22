# JFLP Studio — uso facilitado (v0.3)

Data: 2026-10-21. Base: `feat/jflp-studio` (v0.2, GUI web completa: Painel, Ambiente, Bugs, Lotes,
Comparar, Receitas, Exportar). Objetivo: rodar Maven, Jaguar 2 e Jaguar 1 sobre um projeto com o
mínimo de passos, abrindo como um app de desktop e vendo o resultado direto no código.

Restrições da v0.2 continuam valendo (`.superpowers/sdd/2026-10-20-jflp-studio/global-constraints.md`):
textos em português, subprocessos em lista, servidor só em 127.0.0.1, nada alterado fora da pasta do
JFLP, testes `unittest` sem rede.

## 1. Executar (execução rápida)

Nova tela `/executar`, primeiro item da barra lateral depois do Painel.

- Campo **Projeto**: pasta local ou `URL@commit` (o mesmo `local_bug(spec)` da tela Bugs). Botão
  **Procurar…** abre o seletor de pastas nativo (tkinter `askdirectory`, janela no topo) no próprio
  computador, pelo servidor (`POST /executar/escolher`); se o tkinter faltar, o botão some.
- **O que rodar**: caixas Maven, Jaguar 2 (marcada por padrão), Jaguar 1. Pelo menos uma.
- **Testes**: "todos" (padrão aqui, pois pasta local não tem testes relevantes conhecidos) ou "só estes"
  com uma lista `Classe` / `Classe#metodo` separada por vírgula/linha → `opts.tests` (ver §1.1).
- **Busca automática de correções**: ligada por padrão.
- **Executar** cria um lote (`queue.create_batch`) com o bug e os tipos marcados e vai para
  `/executar/{batch_id}`.
- **Recentes**: os últimos 10 projetos usados (`<home>/recentes.json`: spec, nome, data, tipos), cada um
  com "Executar de novo" (mesmas opções) e "Usar" (preenche o formulário). Se a pasta sumiu, mostra aviso.

### 1.1 Seleção de testes
Se o executor já aceita uma lista explícita de testes (relevant_tests / `-Dtest=`), reutilize; senão
"só estes" fica de fora e é registrado como não feito. Não inventar caminho novo no execute.

### 1.2 Tela do resultado `/executar/{batch_id}`
Um cartão por tipo, atualizado por htmx a cada 2 s enquanto houver job pendente/rodando:
- na fila / rodando (link "ver log ao vivo" → `/job/{id}/ao-vivo`);
- terminado: status com o rótulo e a cor já usados no app, diagnóstico em português e sugestões,
  resumo dos testes (total/falhas), e para Jaguar 1/2 as **10 linhas mais suspeitas** (classe, linha,
  susp) cada uma com link para o código (§3); botões "Detalhes da execução" (`/run/...`) e
  "Ver no código". Quando Maven e Jaguar 2 terminaram, link para a comparação já existente.

## 2. Assistente de primeiro uso

Nova tela `/bem-vindo`.
- Checklist com o estado de cada item: Git; perfil **Jaguar 2** (JDK 11 + Maven ≥3.9.9); dependências do
  Jaguar 2 no `m2/` próprio (pre-fetch); perfil **Maven puro**; perfil **Jaguar 1** (JDK 8 + Maven 3.6.3 + `lib/`).
  Usa `toolchain.profiles.resolve` e a detecção já existente.
- Botão **Preparar tudo**: mostra antes a lista do que vai ser baixado (URL, tamanho, destino — a mesma
  confirmação da tela Ambiente) e, ao confirmar, roda numa única tarefa em segundo plano, em sequência:
  JDK 11, Maven 3.9.9, JDK 8, Maven 3.6.3, lib do Jaguar 1, pre-fetch do Jaguar 2, pulando o que já existe.
  Progresso ao vivo (reaproveitar `_progresso.html`/`app.state.tasks`). Uma falha não para os outros
  itens; o resumo final diz o que falhou e por quê. Opção "só o essencial (Jaguar 2)".
- Git ausente: só instrução (não baixamos Git).
- O Painel (`/`) redireciona para `/bem-vindo` quando o perfil do Jaguar 2 não resolve **e** o usuário
  não dispensou o assistente (`<home>/estado-ui.json` → `assistente_dispensado: true`, gravado por
  "Pular por agora" ou ao concluir). Link "Assistente" fica na tela Ambiente.

## 3. Resultado no código

Nova tela `/run/{rel}/codigo?classe=<FQCN>` para execuções Jaguar 1/2 com linhas.
- Fonte lida do **arquivo do checkout da própria execução** (`checkout.zip` ou `checkout.tar.zst`;
  sem `zstandard` e só `.tar.zst` → mensagem clara). Nada de depender de `work/`.
- Mapeamento classe → arquivo: classe externa (`a.b.C$X` → `a/b/C.java`); procura membros do arquivo
  terminados em `/a/b/C.java`, preferindo o módulo do bug e `src/main/java`, excluindo diretórios de teste
  quando houver mais de um. Não achou → mensagem e a tabela de linhas daquela classe.
- Layout: à esquerda a lista das classes com linhas suspeitas, ordenadas pela maior susp (com a susp
  máxima e a contagem); à direita o arquivo inteiro com número da linha, fundo colorido por susp
  (escala contínua, legível no tema claro e escuro, sem cor nas linhas sem score), na margem a susp e
  `cef/cep`, âncora `#L<n>` e rolagem até a linha mais suspeita. Tooltip com `cef cnf cep cnp`.
- Arquivos grandes: limite de 20 000 linhas; texto lido como UTF-8 com `errors="replace"`, escapado.
- Links: na tabela de linhas da tela da execução, cada linha abre o código; botão "Ver no código" no topo.
- Leitura do arquivo compactado com cache simples (LRU de poucos índices por execução).

## 4. App de desktop

- `jflp ui --app`: abre a interface numa **janela de app** do Edge/Chrome/Chromium
  (`--app=<url> --user-data-dir=<home>/navegador --no-first-run`), sem barra de endereço. O servidor
  encerra quando essa janela fecha (espera o processo do navegador). Ordem: Edge/Chrome/Chromium →
  pywebview (`--window`, se instalado) → navegador padrão. No Linux procura `chromium`,
  `google-chrome`, `microsoft-edge` no PATH.
- `jflp atalho` (`--remover` desfaz): cria o atalho **JFLP Studio** na Área de Trabalho e no Menu Iniciar
  (Windows, `.lnk` via PowerShell `WScript.Shell`, alvo `pythonw.exe -m sbfl ui --app` do `.venv`, pasta
  de trabalho = repositório, ícone `.ico` do pacote) ou `~/.local/share/applications/jflp.desktop` (Linux).
  Nada fora desses arquivos de atalho é alterado.
- Sem console (`pythonw`): `sys.stdout/stderr` são `None` → a saída vai para `<home>/ui.log`. Se o app já
  estiver aberto (trava do worker ou porta), a segunda abertura só traz a janela existente/abre a URL
  (gravar `<home>/ui.json` com url+pid; se o pid vive, abrir a URL num novo `--app` e sair).
- Ícone: `sbfl/web/static/jflp.ico` (+ `favicon`) gerado por código Python puro e versionado.
- Lançadores: `jflp.ps1`/`jflp.cmd` sem argumentos continuam `jflp ui`; README explica o atalho.

## 5. Fora do escopo
Empacotar `.exe` (PyInstaller), auto-atualização, editar código na GUI, cálculo de fórmulas SBFL.

## 6. Aceitação
1. Suíte completa verde.
2. Ao vivo no navegador: assistente mostra o estado real; Executar com a pasta do selftest (demo) rodando
   Maven + Jaguar 2 → cartões terminam com `ok_testes_falhando`, top linhas com `demo.Calc:6`, código com a
   linha 6 destacada.
3. Executar sobre um bug Bugs.jar já usado na aceitação anterior (pasta local do checkout) com Jaguar 2.
4. `jflp atalho` cria o `.lnk`; `jflp ui --app` abre a janela do Edge e o servidor encerra ao fechar.
