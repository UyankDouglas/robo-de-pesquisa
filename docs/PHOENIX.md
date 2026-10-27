# PHOENIX BOT — Consulta de Portabilidade TIM

Robô que lê a planilha da esteira, consulta cada **CNPJ distinto** no Phoenix2 Business
(Portabilidade → Acompanhamento → Status Solicitações), abre todas as solicitações, lê a tabela
**Bilhetes** e separa as linhas da planilha em três arquivos pelo **Status do Bilhete**.

> **Situação da integração real: NÃO validada.** Todo o fluxo foi implementado e testado contra um
> simulador local com dados fictícios. A navegação no Phoenix2 real ainda precisa ser feita com o
> operador logado (seção [Primeira integração](#primeira-integração-com-o-phoenix2-real)). Nenhum
> seletor CSS do site foi inventado: o robô usa apenas os textos visíveis informados (menus, rótulos,
> nomes de colunas), configuráveis em `config/phoenix/selectors.json`.

## Como operar

```bat
run_phoenix.bat            :: ou dist\WebSearchBot\PhoenixBot.exe
```

1. **Selecionar…** a planilha. As abas aparecem com nº de linhas; as **mapeadas** e visíveis vêm
   marcadas; as **ocultas** vêm desmarcadas (marque se quiser processá-las).
2. **Ver/editar mapeamento** confere CNPJ / Acesso / Fila Atual / Nº temporário por aba.
   **Validar** mostra linhas, CNPJs distintos, CNPJs inválidos, linhas sem número e alertas.
3. **Abrir navegador (login)** → faça o login no Phoenix2 na janela aberta (inclusive autenticação
   adicional). **Testar conexão** confirma o login e abre a tela pelo menu.
4. **Consulta de teste** pesquisa um CNPJ e mostra quantas solicitações/bilhetes e os status.
5. **EXECUTAR LOTE**. Acompanhe os indicadores; **PAUSAR / CONTINUAR / CANCELAR** a qualquer momento.
   Se a sessão expirar ou surgir verificação humana, o robô pausa, mostra o aviso laranja e espera
   você agir no navegador e clicar em **CONTINUAR**.
6. **EXPORTAR** (ou automático durante o lote) gera os arquivos em `output\phoenix\`.
   **RETOMAR** continua uma execução interrompida (escolha no Histórico).

Linha de comando (`phoenix_cli.py` ou `PhoenixBotCLI.exe`):

```bat
phoenix_cli.py abas --file "ESTEIRA ATIVAÇÃO para robo de pesquisa.xlsx"
phoenix_cli.py validar --file "...xlsx" --abas "AGOSTO TLVS;OUTUBRO TLV"
phoenix_cli.py inspecionar          :: 1ª integração guiada
phoenix_cli.py login                :: salva a sessão protegida (DPAPI) para o modo sem interface
phoenix_cli.py testar --cnpj 12345678000190
phoenix_cli.py executar --file "...xlsx" [--abas "..."] [--novo]
phoenix_cli.py exportar --run-id 3
phoenix_cli.py execucoes
```

## Primeira integração com o Phoenix2 real

**Já observado no servidor real (27/10/2026, sem login, só o cabeçalho da resposta):** o endereço
responde **HTTP 401** com `WWW-Authenticate: Bearer` e uma página de SSO intitulada
**"Click Provider Link"**. Ou seja: o login é feito num provedor de identidade corporativo (clicar no
link do provedor e autenticar, possivelmente com MFA) — o operador faz isso na janela do navegador do
robô. O robô trata essa página como "sem sessão / sessão expirada" (`login.titulos_pagina_login`).
Como a autenticação é por token, a sessão salva (DPAPI) pode não sobreviver ao fechamento do
navegador; na interface o navegador fica aberto durante todo o trabalho, então isso não atrapalha.

O que precisa ser confirmado no HTML real (o robô foi escrito para tolerar variações, mas só o site
real comprova):

| Etapa | Como o robô procura hoje | Confirmar |
|---|---|---|
| Logado | texto/link "Portabilidade" visível; página de login = campo de senha visível | texto do menu |
| Menu | hover em "Portabilidade" → hover em "Acompanhamento" → clique em "Status Solicitações" (testa cada elemento com o texto até um revelar o submenu) | se o menu abre por hover, frames |
| Tela | texto "Status das Solicitações" + botão "Pesquisar" | título real |
| Tipo da Consulta | `select` ligado ao rótulo "Tipo da Consulta" (ou o único com a opção "CNPJ") | texto da opção |
| Valor / Períodos | primeiro campo após o rótulo "Valor" / "Período inicial" / "Período final" | rótulos |
| Sem resultado | texto "Nenhuma Solicitação foi encontrada." | texto exato |
| Resultados | tabela cujo cabeçalho contém "Razão Social" e "Status Solicitação"; só aceita se a coluna CNPJ = CNPJ pesquisado | nomes das colunas |
| Paginação | link/botão "Próxima", "Próximo", "Próxima página", "»" ou ">" | controle real |
| Detalhar | rádio da linha + botão "Detalhar"; aceita detalhe na mesma aba ou em nova janela | comportamento |
| Bilhetes | tabela cujo cabeçalho contém "Número a portar" e "Status do Bilhete"; colunas lidas pelo nome | nomes das colunas |
| Voltar | botão "Voltar"; senão "voltar" do navegador; senão refaz a pesquisa e vai à página certa | botão real |

Procedimento:

1. `phoenix_cli.py inspecionar` (ou o botão **Inspecionar página** em cada tela).
2. Faça o login e siga as instruções; o robô tenta abrir a tela pelo menu e pede que você faça uma
   pesquisa com resultado, vá à página 2, abra um detalhe e pesquise um CNPJ sem solicitações.
3. As capturas ficam em `logs\inspecao\<data>\` (resumo `.json` + HTML de cada frame com sequências de
   6+ dígitos mascaradas). Ficam só nesta máquina.
4. Ajuste `config/phoenix/selectors.json` se algum texto for diferente e registre aqui os locators
   confirmados (seção abaixo). Rode **Consulta de teste** com CNPJs conhecidos antes do primeiro lote.

### Locators confirmados no ambiente real

_Pendente — preencher após a primeira inspeção (data, versão da tela, o que mudou)._

## Regras de classificação (somente pelo "Status do Bilhete")

| Status do Bilhete | Arquivo | "Fila Atual" na cópia |
|---|---|---|
| Em andamento | `EM_ANDAMENTO_<data_hora>.xlsx` | "Em andamento" |
| Falha parcial / portada (*) | `FALHA_PARCIAL_PORTADA_<data_hora>.xlsx` | texto real do status |
| Em conflito | `EM_CONFLITO_<data_hora>.xlsx` | conteúdo integral de "Motivo Recusa" do **mesmo** bilhete |
| Outros | não exportado | — (contado no relatório) |

- Comparação sem diferença de maiúsculas, acentos e espaços — nunca por semelhança.
- (*) Configurado só com o texto literal "Falha parcial / portada". **Confirme no Phoenix2** se é um
  status único ou dois (ex.: "Falha parcial" e "Portada") e ajuste em *Regras* na interface ou em
  `config/phoenix/settings.json` (`status.falha_parcial_portada`). "Portado" **não** é incluído
  automaticamente.
- Em conflito com Motivo Recusa vazio, "-" ou indisponível: vai para EM_CONFLITO com Fila Atual =
  "Em conflito" e a coluna **[ROBÔ] Revisão** = "motivo pendente, revisar" (também na aba *Revisar*
  do relatório). O motivo nunca é inventado.
- O "Status Solicitação" da listagem é ignorado na classificação.

## Correspondência CNPJ + Número a portar

- CNPJ: 14 dígitos com zeros à esquerda restaurados e dígitos verificadores conferidos (aceita também o
  CNPJ alfanumérico). CPF ou CNPJ inválido não é consultado (vai para o relatório).
- Telefone: só dígitos. DDI 55 removido apenas se configurado e com 12/13 dígitos (DDD 55 com 11
  dígitos é preservado). 10 e 11 dígitos são números diferentes — nada é "corrigido".
- Uma linha só recebe um bilhete do **mesmo CNPJ** e do **mesmo número** (não mistura solicitações).
- Número temporário: só se ativado, e só quando exatamente um bilhete corresponde.
- Vários bilhetes para o mesmo número: se todos têm o mesmo status/motivo, usa-o; senão, com a regra
  "mais recente", usa o da solicitação com a **data mais recente única**; empate, data ilegível ou regra
  "revisar" → aba *Revisar*, não exportado.
- Mesmo CNPJ + número repetido na mesma aba: todas as linhas recebem o mesmo bilhete e são sinalizadas.
- Cada CNPJ distinto é pesquisado **uma vez**, mesmo que apareça em várias linhas/abas.

## Planilha analisada (ESTEIRA ATIVAÇÃO para robo de pesquisa.xlsx)

29 abas (13 ocultas). Mapeamento em `config/phoenix/columns_mapping.json`; o cabeçalho esperado de
cada coluna é conferido a cada importação (se mudar, a aba é recusada).

| Aba | Linhas | CNPJ | Acesso | Fila Atual | Observação |
|---|---|---|---|---|---|
| AGOSTO TLVS | 790 | D | G | AA | 98 linhas sem número válido |
| SETEMBRO TLV | 1743 | D | G | AA | 142 linhas sem número válido |
| OUTUBRO TLV | 1198 | D | G | **Z** | Z e AA = "fila atual"; AA é numérica e não é alterada |
| REENVIO BILHETE 2310 | 179 | D | G | **Z** | linhas 68–180: Z vazia e status em AA (colunas deslocadas) → sinalizadas para revisão |
| Ação AGOSTO | 115 | D | G | Z | |
| OUTUBRO / SETEMBRO / AGOSTO CSTV | 468 / 904 / 1069 | P | I | — | Fila Atual criada só na exportação; Temporário em J |
| JULHO CSTV, JUNHO CSTV | 1032 / 121 | P / Q | I / J | — | detectadas automaticamente (fora da tabela inicial) |
| Bate E Volta TLV | 68 | C | **nenhuma** | P | sem coluna de número: não há como relacionar CNPJ + número |
| SETEMBRO TLVS (oculta) | 810 | C | F (Acesso 1) | U | só se marcada |

Nas abas TLV a coluna "Número Temporário" contém texto (não números). Somadas, as 12 abas mapeadas têm
5.157 CNPJs distintos.

## Saídas

- `EM_ANDAMENTO_*`, `FALHA_PARCIAL_PORTADA_*`, `EM_CONFLITO_*`: uma aba por aba de origem, todas as
  colunas originais (inclusive cabeçalhos repetidos), Fila Atual alterada (destacada em amarelo) e
  colunas de auditoria **[ROBÔ]**: aba e linha originais, CNPJ pesquisado, número a portar, bilhete,
  Status do Bilhete, Motivo Recusa, solicitação (página/posição + data/tipo/status), data/hora da
  pesquisa, revisão. Sem registros → aba "Sem registros".
- `RELATORIO_TECNICO_*`: Resumo, Revisar, Sem correspondência (com os números que o Phoenix tem para o
  CNPJ), Sem número ou CNPJ inválido, CNPJ sem solicitações, Erros de consulta, Bilhetes sem linha,
  Contagens (por status), Consultas (por CNPJ) e Mapeamento.
- Valores exportados são os **calculados** (fórmulas não são copiadas, para não quebrar referências a
  outras abas). O arquivo de origem nunca é aberto para escrita; seu SHA-256 fica no relatório.
- Mesmo carimbo de data/hora durante uma execução (os arquivos são regravados de forma atômica);
  nova execução = novos arquivos.

## Robustez e segurança

- SQLite (`data/phoenix_bot.db`): cada CNPJ consultado (solicitações e bilhetes em JSON), tentativas,
  eventos e as linhas importadas. Retomada após queda sem repetir CNPJs concluídos; a classificação é
  recalculada a partir do banco a cada exportação (mudar uma regra não exige nova consulta).
- Uma instância por execução (trava do sistema), intervalo configurável entre consultas, retentativas
  com espera progressiva, timeout, detecção de "sessão expirada", página de login e CAPTCHA — o robô
  **nunca** tenta contornar: pausa e pede o operador.
- Sessão do navegador opcionalmente salva com DPAPI (`data/sessions/phoenix2.session.bin`); senhas
  nunca são digitadas nem armazenadas pelo robô.
- Logs com CNPJ e telefones mascarados (`123********190`).

## Testes executados (simulador + dados fictícios)

| Cenário pedido | Teste |
|---|---|
| 1 uma solicitação · 5 em andamento | `test_matching::test_single_request_in_progress`, `test_client::test_single_request_and_menu_navigation` |
| 2 várias solicitações · 4 status diferentes | `test_matching::test_multiple_requests_use_ticket_status_not_request_status`, `test_client::test_multiple_requests_and_tickets` |
| 3 vários bilhetes · 6 falha parcial · 7/8 conflito com/sem motivo · 9 fora do filtro | `test_matching::test_one_request_many_tickets` |
| 10 CNPJ sem resultado · 11 número diferente | `test_matching::test_not_found_and_mismatched_number`, `test_client::test_not_found_and_leading_zeros` |
| 12 CNPJ repetido | `test_matching::test_repeated_cnpj_and_cross_sheet_rows` |
| 13 zeros à esquerda | `test_matching::test_cnpj_normalization_keeps_leading_zeros`, `test_leading_zeros_ddi_pagination_and_history` |
| 14 "fila atual" duplicada · 15 aba sem Fila Atual | `test_matching::test_sheets_listing_hidden_and_mapping_detection`, `test_job::test_full_job_exports_three_files_and_keeps_source` |
| 16 pausa e retomada | `test_job::test_pause_cancel_and_resume_after_interruption` |
| 17 paginação | `test_client::test_pagination_walks_every_page` |
| 18 três arquivos | `test_job::test_full_job_exports_three_files_and_keeps_source`, `test_only_classified_rows_go_to_each_file` |
| 19 sessão expirada | `test_client::test_session_expired_is_detected`, `test_job::test_end_to_end_with_browser_and_session_expiry` |

Os testes de navegador rodam com Edge (Chromium) contra o simulador, com e sem iframe.

## Pendências

1. Inspeção e validação no Phoenix2 real (login do operador) e registro dos locators confirmados.
2. Confirmar o(s) texto(s) exato(s) de "Falha parcial / portada".
3. Decidir o tratamento da aba **Bate E Volta TLV** (não tem coluna de número).
4. Revisar as linhas 68–180 de **REENVIO BILHETE 2310** (Fila Atual deslocada para AA).
5. Confirmar com o responsável pelo Phoenix2 o intervalo aceitável entre consultas (padrão: 3 s).
