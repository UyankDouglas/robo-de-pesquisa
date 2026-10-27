# Web Search Bot — Robô de Pesquisas e Extração de Dados

Robô (RPA) em **Python + Playwright** que lê identificadores de uma planilha Excel, pesquisa cada um em
um site (ou API) configurado, extrai os resultados e gera uma planilha de saída. Tem interface gráfica
(CustomTkinter), modo sem interface para o Agendador de Tarefas do Windows, histórico em SQLite e
retomada de execuções interrompidas.

> O site real ainda não foi definido. O projeto vem com um **site local de demonstração** (dados
> fictícios) e com três formas de integrar o site real — veja [Como adicionar um site](#como-adicionar-um-site).

---

## Sumário
1. [Instalação](#instalação)
2. [Uso pela interface gráfica](#uso-pela-interface-gráfica)
3. [Modo sem interface e agendamento](#modo-sem-interface-e-agendamento)
4. [Planilha de entrada e de saída](#planilha-de-entrada-e-de-saída)
5. [Como adicionar um site](#como-adicionar-um-site)
6. [API ou navegador?](#api-ou-navegador)
7. [Login, sessão e CAPTCHA](#login-sessão-e-captcha)
8. [Execução, retentativas e limites](#execução-retentativas-e-limites)
9. [Histórico, auditoria e retomada](#histórico-auditoria-e-retomada)
10. [Testes](#testes)
11. [Gerar o executável (.exe)](#gerar-o-executável-exe)
12. [Estrutura do projeto](#estrutura-do-projeto)
13. [Solução de problemas](#solução-de-problemas)

---

## Instalação

Requisitos: Windows 10/11, **Python 3.12+** e Microsoft Edge (já vem no Windows) ou Google Chrome.

```bat
install.bat
```

O script cria o ambiente virtual `.venv`, instala `requirements.txt` e gera a planilha de exemplo
`input\exemplo_entrada.xlsx`. O robô usa o **Edge instalado**, então não é preciso baixar navegador.
Para usar o Chromium do Playwright: `install.bat --chromium`.

Instalação manual equivalente:

```bat
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python cli.py sample-input
```

## Uso pela interface gráfica

```bat
run.bat
```

1. **Planilha → Selecionar…** — escolha o `.xlsx`. As abas e colunas são detectadas automaticamente.
2. Escolha a **aba** e a **coluna de pesquisa** (a coluna com "documento", "cnpj", "protocolo"… é sugerida).
   O painel mostra quantos registros serão consultados, duplicados e vazios/inválidos;
   **Ver lista de processamento** mostra registro a registro.
3. Opcional: intervalo (**Do registro nº / Até**), **completar zeros** (ex.: 14 para CNPJ que perdeu zeros
   no Excel) e **validação** (dígitos, CNPJ, CPF).
4. Escolha o **site/adaptador**, a **pasta de destino** e o **intervalo entre consultas**.
5. **INICIAR**. Acompanhe os indicadores (total, concluídas, pendentes, não encontrados, erros, %,
   tempo decorrido e tempo restante estimado) e o log em tempo real.
6. **PAUSAR / CONTINUAR / PARAR** a qualquer momento. PARAR termina a consulta atual, salva tudo e
   deixa a execução pronta para ser retomada.
7. **EXPORTAR EXCEL** gera a planilha com o filtro escolhido (todos, concluídos, sem resultado, erros…).
   O Excel também é salvo automaticamente durante o processamento e ao final.

Se você iniciar de novo a mesma planilha/coluna/site com pendências, o programa pergunta se deve
**continuar de onde parou** (sem repetir os concluídos) ou criar uma execução nova.
A aba **Histórico de execuções** lista execuções anteriores e permite retomar, repetir erros e exportar
(duplo clique mostra os detalhes e eventos da execução).

A interface nunca congela: o processamento roda em uma thread de trabalho que se comunica com a
janela por filas.

## Modo sem interface e agendamento

1. Na interface, configure tudo e clique em **Salvar configuração para agendamento…**
   (ou copie `config\example_config.json` e edite).
2. Teste: `run_cli.bat run --config config\minha_consulta.json`
3. Agende no Windows:
   ```bat
   run_cli.bat schedule --config config\minha_consulta.json --name consulta_diaria --time 07:30
   ```
   O comando cria `scheduled\consulta_diaria.bat` e mostra o comando `schtasks`. Acrescente
   `--apply` para registrar a tarefa (pasta "WebSearchBot" no Agendador). Também aceita
   `--schedule WEEKLY --days MON,WED,FRI`, `HOURLY`, `ONCE` etc.

Com `"resume": true` (padrão), cada disparo retoma a execução incompleta da mesma
planilha/coluna/site em vez de recomeçar. `"retry_errors_on_resume": true` recoloca os erros na fila.

**Códigos de saída** (para o Agendador ou scripts):

| Código | Significado |
|---|---|
| 0 | Concluída sem erros |
| 1 | Concluída, mas há registros com erro |
| 2 | Configuração ou parâmetros inválidos |
| 3 | Falha de execução (navegador, site, banco) |
| 4 | Precisa do operador (login ou CAPTCHA) — execute pela interface |
| 5 | A execução já está em andamento em outra instância |
| 6 | Parada antes do fim (ainda há pendentes) |

Outros comandos úteis:

```bat
run_cli.bat runs                                 :: execuções anteriores
run_cli.bat show --run-id 3 --errors             :: auditoria e registros com erro
run_cli.bat resume --run-id 3 [--retry-errors]   :: retomar
run_cli.bat export --run-id 3 --filter errors    :: exportar (all, found, not_found, errors, processed, pending)
run_cli.bat retry-errors --run-id 3 [--all]      :: recolocar erros (ou tudo) na fila
run_cli.bat adapters                             :: sites disponíveis
run_cli.bat inspect --file input\planilha.xlsx   :: abas e colunas
run_cli.bat test-site --adapter demo --query 12345678000190 --visible
run_cli.bat login --adapter demo_login           :: login manual e gravação protegida da sessão
run_cli.bat demo-server [--require-login]        :: sobe só o site de demonstração (http://127.0.0.1:8765)
```

## Planilha de entrada e de saída

**Entrada** — qualquer `.xlsx`; a coluna de pesquisa pode conter CNPJ, CPF, protocolos, códigos, nomes etc.
- Tudo é lido como **texto**: zeros à esquerda são preservados; números longos não viram notação científica;
  células numéricas com formato de zeros (`00000000000000`) têm os zeros restaurados.
- Linhas totalmente vazias são ignoradas; identificador vazio vira **Inválido** (não é consultado).
- **Duplicados** são consultados uma única vez; as repetições recebem o resultado da primeira ocorrência.
- O cabeçalho é a primeira linha preenchida (no modo agendado pode ser definido em `header_row`).

**Saída** — arquivo separado do original, em `output\<planilha>_resultado_exec<N>.xlsx`:
- todas as colunas originais, na ordem original;
- os campos capturados do site (ex.: NOME, SITUAÇÃO CADASTRAL…);
- `RESULTADO`, `SITUAÇÃO` (Concluído, Sem resultado, Erro, Pendente, Duplicado, Inválido),
  `DATA/HORA DA CONSULTA`, `TENTATIVAS`, `MENSAGEM`, `LINHA NA PLANILHA ORIGINAL`;
- aba **Resumo** com totais da execução.

O Excel é sempre gerado a partir do SQLite e gravado de forma atômica (arquivo temporário +
substituição), então uma queda nunca deixa um arquivo corrompido. Se a planilha de saída estiver
aberta no Excel, é salva uma cópia com data/hora no nome. Textos iniciados por `=` são gravados como
texto (nunca como fórmula).

## Como adicionar um site

Antes de tudo, confirme que a consulta automatizada é **permitida** pelos termos do site e quais limites
de uso ele impõe. Depois escolha uma das três formas (nenhuma exige alterar o motor):

### 1. Site configurado por JSON (sem programar)
Para sites com um campo de pesquisa e um bloco de resultado.

1. Copie `config\sites\_modelo_site.json.example` para `config\sites\<id>.json`.
2. Preencha URL, seletores do campo/botão, do bloco de resultado, da mensagem "nenhum resultado",
   do CAPTCHA (apenas para detecção) e a lista de `fields` (chave, título da coluna, seletor).
   Use seletores estáveis: `#id`, `[name=...]`, `[data-testid=...]`, `label=Texto`, `role=button[name="Pesquisar"]`.
   Se o site aceita a pesquisa direto na URL, use `search_url_template` com `{query}`.
3. Teste: `run_cli.bat test-site --adapter <id> --query <valor> --visible`.
4. O site aparece na lista da interface. Exemplo funcional: `config\sites\demo_declarativo.json`.

### 2. Adaptador Python para navegador
Para fluxos com várias etapas, tabelas, abas, regras específicas. Copie
`docs\modelo_adapter_navegador.py` para `adapters\<nome>_adapter.py` (o nome deve terminar em
`_adapter.py`) e preencha. Exemplo completo e testado: `adapters\demo_adapter.py`.

### 3. Adaptador de API
Quando existe API oficial e autorizada. Copie `docs\modelo_adapter_api.py`. Tokens são lidos de
variáveis de ambiente — nunca do código-fonte. Exemplo testado: `DemoApiAdapter` em `adapters\demo_adapter.py`.

Contrato dos adaptadores (`adapters\base_adapter.py`):
- `SearchResult.found_with({...})` quando encontra; `SearchResult.not_found()` quando o site diz que não há
  resultado (**não é erro**);
- `TransientError` (rede, timeout, HTTP 5xx) → o motor repete com espera progressiva;
- `RateLimitedError(retry_after=...)` → o motor espera o tempo pedido;
- `PermanentError` → erro do registro, sem nova tentativa;
- `HumanVerificationRequired` → pausa e pede o operador; `SessionExpiredError` → pede novo login.

## API ou navegador?

Para cada site novo, responda antes de implementar:

1. Existe **API oficial e documentada**? O uso para este fim é **autorizado** (termos, contrato, chave)?
2. A API devolve os campos necessários? Quais são os limites de requisição?
3. Só se a resposta for "não" use a automação do navegador — e respeite os termos e limites do site.

Prefira sempre a API quando disponível e permitida: é mais rápida, estável e previsível.

## Login, sessão e CAPTCHA

- **Login manual assistido**: o robô abre a página de login e pausa; o operador entra no site na janela
  do navegador e clica em **CONTINUAR**. Senhas **nunca** são digitadas, armazenadas ou registradas pelo robô.
- **Lembrar login** (opcional): a sessão (cookies) é salva em `data\sessions\` criptografada com a
  **DPAPI do Windows** — só o mesmo usuário, no mesmo computador, consegue lê-la. Para o modo agendado,
  salve a sessão antes com `run_cli.bat login --adapter <id>`.
- **Sessão expirada** é detectada pelo adaptador; na interface o robô pede novo login e continua; no modo
  agendado termina com código 4.
- **CAPTCHA / verificação humana**: o robô **não** tenta resolver nem contornar. Ele pausa, traz o navegador
  para frente e aguarda o operador resolver e clicar em CONTINUAR (requer "Mostrar navegador" ligado).
- Os logs mascaram valores de campos como senha/token/cookie; a opção `mask_identifiers_in_logs`
  também mascara os identificadores pesquisados.

## Execução, retentativas e limites

| Parâmetro | Onde | Padrão |
|---|---|---|
| Intervalo mínimo entre consultas | interface / `interval_seconds` | 1 s |
| Máximo de consultas por minuto | interface / `max_per_minute` | sem limite extra |
| Timeout de cada operação | interface / `timeout_seconds` | 30 s |
| Novas tentativas para erro temporário | interface / `max_retries` | 3 |
| Espera progressiva | `backoff_base_seconds` / `backoff_max_seconds` | 2, 4, 8… até 60 s |
| Salvamento automático do Excel | `autosave_every` / `autosave_seconds` | 10 registros ou 60 s |

O intervalo efetivo é o maior entre o configurado e o exigido pelo site (`site_min_interval` /
`min_interval_seconds`). HTTP 429 com `Retry-After` é respeitado. Os registros são processados
**um por vez** — não há paralelismo agressivo nem técnicas de evasão de detecção.

## Histórico, auditoria e retomada

Banco SQLite em `data\web_search_bot.db`:
- `runs` — execução, site, planilha/aba/coluna, configuração usada, situação, datas, arquivo de saída;
- `items` — cada registro: identificador, linha original completa, situação, resultado estruturado (JSON),
  mensagem de erro, nº de tentativas, data/hora da consulta, duração;
- `attempts` — cada tentativa (início, fim, resultado, mensagem);
- `run_events` — início, pausa, retomada, login, parada, conclusão.

Uma única instância processa cada execução (trava de arquivo do sistema). Se o computador desligar ou o
programa fechar no meio, os registros "em processamento" voltam para a fila e a execução aparece como
**Interrompida**, pronta para retomar. Registros concluídos não são consultados de novo, salvo pedido
explícito (Repetir erros / `retry-errors --all`). Logs: `logs\web_search_bot.log` (rotativo) e
`logs\run_<N>.log` por execução.

## Testes

```bat
run_tests.bat                                   :: todos
run_tests.bat -m "not browser and not ui"       :: só os rápidos (sem navegador e sem janela)
```

Cobrem: importação, preservação de identificadores, duplicados, processamento, exportação, pesquisas sem
resultado, erros temporários, pausa/retomada, recuperação após interrupção, persistência do histórico,
sessão protegida, modo agendado (códigos de saída), automação real do navegador contra o site de
demonstração (Edge) e a interface gráfica.

## Gerar o executável (.exe)

```bat
build_exe.bat
```

Gera `dist\WebSearchBot\` com:
- `WebSearchBot.exe` — interface gráfica;
- `WebSearchBotCLI.exe` — modo sem interface (use este no Agendador de Tarefas);
- `config\`, `input\`, `output\`, `logs\` — pastas de trabalho ao lado do executável.

Distribua a **pasta inteira** (o executável depende da subpasta `_internal`). O executável usa o Edge
ou Chrome instalado na máquina; para usar o Chromium do Playwright execute
`WebSearchBotCLI.exe install-browser` uma vez. O build precisa ser feito no Windows.

## Estrutura do projeto

```text
main.py / cli.py           pontos de entrada (interface / sem interface)
core/                      motor (engine), navegador, configuração, retentativas, sessão DPAPI,
                           trava de instância, agendamento, operações compartilhadas (jobs)
adapters/                  contrato dos adaptadores, demonstração, adaptador JSON, registro
config/                    example_config.json e sites/*.json
excel/                     importer.py (entrada) e exporter.py (saída)
database/                  conexão/esquema SQLite e repositório
ui/                        janela principal e componentes
utils/                     logs, validadores, planilha de exemplo
demo_site/                 site local de demonstração (servidor + HTML/JS)
docs/                      modelos de adaptador (navegador e API)
tests/                     testes automatizados (pytest)
input/ output/ logs/ data/ planilhas, resultados, logs e banco/sessões
```

## Solução de problemas

| Sintoma | O que fazer |
|---|---|
| "Não foi possível abrir nenhum navegador" | Instale/atualize o Edge ou Chrome, ou rode `run_cli.bat install-browser` e escolha "Chromium do Playwright". |
| Execução parou com "Aguardando operador" | Login ou CAPTCHA: abra pela interface com "Mostrar navegador" ligado e retome. |
| Muitos timeouts | Aumente o timeout e o intervalo entre consultas; verifique a conexão. |
| Excel de saída com outro nome | O arquivo estava aberto no Excel; foi salva uma cópia com data/hora. |
| "já está sendo processada por outra instância" | Outra janela/tarefa está processando a mesma execução. |
| Identificadores sem zeros à esquerda | Já perderam os zeros na planilha: use "Completar zeros até" (ex.: 14). |

## O que falta para o site real

1. Informar o site (URL), confirmar a autorização de uso e verificar se há API oficial.
2. Definir os campos a extrair e o que caracteriza "não encontrado" e "erro" no site.
3. Criar o adaptador (JSON ou Python) com os seletores reais e testar com `test-site`.
4. Ajustar intervalo/limites conforme os termos do site e, se houver login, salvar a sessão.
