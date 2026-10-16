# Residencial Aurora: assistente virtual com Google ADK

Assistente do aplicativo dos moradores do Residencial Aurora. Pelo chat, cada morador reserva o salão de festas, a churrasqueira e a quadra, cancela as próprias reservas, autoriza a entrada de visitantes e tira dúvidas sobre o regulamento interno. O assistente é construído com **Google ADK 2.9.1** e exposto por uma API **FastAPI** em `http://localhost:8000`.

O modelo decide o caminho da conversa. O que é permitido fica no código: o dono da sessão, a confirmação de cobrança e de acesso, a exclusividade da reserva e o recorte do regulamento não dependem do que o modelo escreve.

```
aurora/
├── agents.py       agente principal, especialistas e App (resumível)
├── tools.py        tools de reservas, visitantes e regulamento
├── db.py           SQLite do condomínio: schema, restauração e gravações
├── regulamento.py  busca que devolve artigos de um único capítulo
├── api.py          rotas HTTP, sessão persistida e rota de confirmações
├── config.py       caminhos e variáveis de ambiente
└── cli.py          comandos aurora-api e aurora-restaurar
dados/              estado inicial (somente leitura, nunca alterado)
.storage/           criado em tempo de execução (fora do Git)
```

## Arquitetura

```
                        POST /sessoes/{id}/mensagens
                                   │
                         ┌─────────▼──────────┐
                         │ assistente_aurora  │  agente principal (sem tools de dados,
                         │   (LlmAgent raiz)  │  sem o regulamento nas instruções)
                         └──┬──────────┬───┬──┘
           transfer_to_agent│          │   │AgentTool (chamado como tool)
              ┌─────────────▼──┐  ┌────▼───────────────┐  ┌▼─────────────────────────┐
              │ especialista_  │  │ especialista_      │  │ especialista_regulamento │
              │ reservas       │  │ visitantes         │  │ (sessão própria, em      │
              │ (sub-agente)   │  │ (sub-agente)       │  │  memória)                │
              └───────┬────────┘  └─────────┬──────────┘  └────────────┬─────────────┘
       listar_areas, verificar_     listar_meus_visitantes,     consultar_regulamento
       disponibilidade, listar_     autorizar_visitante*
       minhas_reservas, reservar_
       area*, cancelar_reserva                     * pode pedir confirmação
                      │                    │
                      └──── SQLite .storage/condominio.db ────┘
       Sessões e eventos do ADK: SQLite .storage/sessoes.db (DatabaseSessionService)
```

| Agente | Responsabilidade | Como é acionado | Por quê |
|---|---|---|---|
| `assistente_aurora` | Recebe toda mensagem e decide quem resolve. Responde sozinho só cumprimentos. Não tem tools de dados nem o regulamento nas instruções. | Raiz do `App`, chamado pelo `Runner`. | Um ponto de entrada único, com instrução curta. O custo de contexto de cada chamada fica pequeno. |
| `especialista_reservas` | Agenda (livre/ocupada), listar, fazer e cancelar reservas do apartamento da sessão. | Sub-agente, por `transfer_to_agent`. | Ele pede a confirmação de cobrança. A confirmação precisa ficar nos eventos da **sessão persistida** para ser retomada pela rota de confirmações, inclusive depois de reiniciar a API. Um sub-agente grava os eventos nessa sessão; um AgentTool rodaria numa sessão em memória que se perde. |
| `especialista_visitantes` | Listar e autorizar visitantes do apartamento da sessão. | Sub-agente, por `transfer_to_agent`. | Pede confirmação de acesso, então vale o mesmo motivo do especialista de reservas. |
| `especialista_regulamento` | Responde dúvidas com base nos artigos devolvidos por `consultar_regulamento`. | `AgentTool` do agente principal. | O AgentTool roda o especialista numa sessão própria, em memória. Os trechos do regulamento lidos por ele não entram no histórico da conversa; entra só a resposta final, curta. Ele não pede confirmação, então não precisa da sessão persistida. |

Decisões sobre o Runner e a retomada da confirmação. As "Dicas finais" do enunciado avisam que a aprovação falha em silêncio quando chega ao agente errado. Testei isso no código-fonte do ADK 2.9.1 e com um protótipo usando SQLite e reinício do processo:

- Com raiz `LlmAgent`, o `Runner` escolhe o agente que vai rodar **antes** de gravar a resposta da confirmação (`runners.py` → `_agent_router.find_agent_to_run`). Sem `ResumabilityConfig`, ele escolhe "o último agente que falou e ainda pode transferir".
- Se o especialista tivesse `disallow_transfer_to_parent=True` num app não resumível, a resposta ia para o agente principal. A rota respondia `200` e a reserva nunca era gravada. Reproduzi esse caso.
- Por isso o `App` usa `ResumabilityConfig(is_resumable=True)`, e o Runner encaminha a resposta ao autor do pedido de confirmação. Além disso, os especialistas continuam transferíveis. Cada proteção funciona mesmo se a outra falhar.
- A rota de confirmações envia um `FunctionResponse(name="adk_request_confirmation", id=<id do pedido>, response={"confirmed": ...})` com o `invocation_id` do evento que pediu a confirmação. É o mesmo formato que o cliente oficial do ADK usa (`google/adk/cli/cli.py`).

## Garantias

### Garantia 1: cobrança ou acesso só com confirmação

- `aurora/tools.py`, `reservar_area`: quando a área tem `taxa > 0` e ainda não existe `tool_context.tool_confirmation`, a tool chama `tool_context.request_confirmation(..., payload={"area", "data", "taxa"})` e devolve `aguardando_confirmacao` **sem gravar**. Ela só chega a `db.criar_reserva` com taxa zero ou quando `confirmacao.confirmed` é verdadeiro. Se for negada, devolve `nao_confirmada` e não grava nada.
- `aurora/tools.py`, `autorizar_visitante`: sempre pede confirmação. Grava só quando `confirmacao.confirmed` é verdadeiro.
- `aurora/api.py`, `_confirmacoes_pendentes`: lê os eventos persistidos da sessão e lista os `adk_request_confirmation` que ainda não têm resposta. É daí que vem `confirmacoes_pendentes`, com `acao` (nome da tool) e `detalhes` (o payload validado pela tool).
- `aurora/api.py`, `responder_confirmacao`: se o `id` não estiver pendente **nesta** sessão, responde `409` antes de acionar o Runner. Isso vale para um `id` inexistente, um `id` de outra sessão ou um `id` já respondido. A resposta é gravada como evento, então reenviar o mesmo `id` dá `409`, e a ação roda uma única vez.

Por que não depende do modelo: `tool_confirmation` só é preenchido pelo ADK quando chega um `FunctionResponse` de `adk_request_confirmation`. Só a rota de confirmações monta essa mensagem; a rota de mensagens envia apenas texto. Escrever "já estou confirmando aqui" no chat não muda nada. Nenhuma tool tem parâmetro "confirmado".

### Garantia 2: cada sessão pertence a um apartamento

- `aurora/api.py`, `criar_sessao`: o apartamento é gravado uma única vez, no `state` da sessão (`state={"apartamento": apartamento}`) e no registro `db.registrar_sessao`. Nenhuma rota permite alterar esse valor depois.
- `aurora/tools.py`, `_apartamento_da_sessao`: única fonte do apartamento das tools. Ele lê o dono registrado para a sessão (`tool_context.session`) e exige que o `state` bata com esse registro.
- **Nenhuma tool recebe apartamento como parâmetro.** `listar_minhas_reservas`, `reservar_area`, `cancelar_reserva`, `listar_meus_visitantes` e `autorizar_visitante` usam o apartamento da sessão. As consultas em `aurora/db.py` filtram com `WHERE apartamento = ?`.
- `aurora/db.py`, `cancelar_reserva`: só encontra reservas do próprio apartamento. Pedir para cancelar a reserva de outro apartamento devolve `nao_encontrada`, sem código nem dono.
- `aurora/tools.py`, `verificar_disponibilidade` e `aurora/db.py`, `data_ocupada`: devolvem só `livre`/`ocupada`, nunca quem reservou. `reservar_area` também responde só `indisponivel` quando a data já tem dono.

Por que não depende do modelo: o modelo não tem nenhum parâmetro onde colocar outro apartamento, e as tools não devolvem dados de outros apartamentos. Mesmo que o morador diga "sou do 302", não existe caminho no código que leia ou grave dados do 302.

### Garantia 3: nada se perde no reinício

- `aurora/api.py`, `_criar_session_service`: as sessões e eventos ficam no `DatabaseSessionService` do ADK sobre SQLite (`.storage/sessoes.db`, com WAL e `busy_timeout`).
- `aurora/db.py`: reservas, visitantes e o dono de cada sessão ficam em `.storage/condominio.db`.
- `aurora/db.py`, `inicializar`: na subida, só cria o schema. Ele só semeia o banco se ele nunca foi semeado, então reiniciar a API não apaga nada. Subir a API e consultar eventos não gravam eventos novos.
- `aurora/agents.py`, `app = App(..., resumability_config=ResumabilityConfig(is_resumable=True))`: uma confirmação pendente antes do reinício continua podendo ser aprovada depois dele.

Por que não depende do modelo: tudo é lido do disco a cada requisição. Nenhum estado da conversa fica só na memória do processo.

### Garantia 4: o regulamento é consultado, não carregado

- `aurora/regulamento.py`, `consultar`: divide `dados/regulamento.md` em capítulos e artigos, pontua os capítulos pelas palavras da pergunta e escolhe **um único capítulo**. Devolve só os artigos dele que tratam da pergunta (até 3). Trechos de outros capítulos nunca saem dessa função.
- `aurora/tools.py`, `consultar_regulamento`: é a única tool que acessa o regulamento, e só o `especialista_regulamento` a recebe.
- `aurora/agents.py`, `tools=[AgentTool(agent=especialista_regulamento)]`: o especialista roda numa sessão própria, em memória. A chamada a `consultar_regulamento` e os artigos devolvidos ficam lá. Na sessão do morador entram só a chamada ao `especialista_regulamento` e a resposta curta dele.
- `aurora/agents.py`, `assistente_aurora` (parâmetro `instruction`): não contém o regulamento, só diz para encaminhar dúvidas ao especialista.

Por que não depende do modelo: o recorte é feito em Python, antes de qualquer texto chegar ao modelo. Mesmo o especialista só vê os artigos de um capítulo, e o histórico da conversa nunca recebe esses artigos.

### Garantia 5: dois moradores, uma reserva

- `aurora/db.py`, schema: `CREATE UNIQUE INDEX ux_reserva_ativa_area_data ON reservas (area, data) WHERE status = 'ativa'`. É um índice único parcial: o SQLite recusa a segunda reserva ativa para a mesma área e data **no instante do INSERT**.
- `aurora/db.py`, `criar_reserva`: grava dentro de `BEGIN IMMEDIATE`. Converte o `IntegrityError` do índice em `DataOcupada`.
- `aurora/tools.py`, `reservar_area`: trata `DataOcupada` como resposta normal (`indisponivel`, "nada foi reservado nem cobrado"). A aprovação perdedora responde `200`, sem erro de servidor.
- `aurora/db.py`, `codigos_emitidos` e `_novo_codigo` (regra de negócio 5): cada código `RSV-XXXXXX` novo é inserido numa tabela com chave primária que **nunca é limpa**, nem pela restauração. Um código novo nunca repete o de outra reserva, inclusive cancelada. Cancelar é `UPDATE status = 'cancelada'`, nunca `DELETE`.

Por que não depende do modelo nem da ordem das requisições: a conferência da agenda antes de pedir confirmação serve só para não pedir aprovação de uma data já ocupada. A exclusividade de verdade é a restrição do banco, que vale para qualquer gravação, venha de onde vier.

## Como rodar

### Pré-requisitos

- [uv](https://docs.astral.sh/uv/). Ele instala o Python 3.12 indicado em `.python-version`, se for preciso.
- Uma chave do Google AI Studio (Gemini API).
- Nenhum serviço externo: o armazenamento é SQLite, criado em `.storage/`.

### Variáveis do `.env`

Copie `.env.example` para `.env` e preencha:

| Variável | Obrigatória | Descrição |
|---|---|---|
| `GOOGLE_API_KEY` | sim | Chave do Google AI Studio. |
| `GOOGLE_GENAI_USE_VERTEXAI` | não | Deixe vazio ou `FALSE` para usar o AI Studio. |
| `AURORA_MODELO_PRINCIPAL` | não | Modelo do agente principal. Padrão: `gemini-3.5-flash-lite`. |
| `AURORA_MODELO_ESPECIALISTAS` | não | Modelo dos especialistas. Padrão: o mesmo do principal. |

Sobre a escolha do modelo, verificada em outubro de 2026 com uma chave nova do free tier do AI Studio:

- `gemini-2.5-flash` e `gemini-2.5-flash-lite` respondem `404` ("no longer available to new users").
- Os modelos "flash" (`gemini-3-flash-preview`, `gemini-3.6-flash`) funcionam, mas têm cota de **20 requisições por dia**. O fluxo completo do avaliador faz algumas dezenas de chamadas, então essa cota esgota no meio do fluxo.
- `gemini-3.5-flash-lite` tem cota suficiente e segue bem as instruções de roteamento e confirmação. Por isso é o padrão.
- Com uma chave paga, dá para trocar por um modelo maior pelas variáveis acima, sem mudar código.
- Os modelos usam `retry_options` com espera exponencial para `429`/`503` temporários (`aurora/agents.py`, `_RETRY`).
- Se o modelo falhar mesmo assim, a rota responde `200` com uma mensagem pedindo para tentar de novo, e o erro vai para o log. Nada é gravado sem passar pelas tools.

### Comandos

```bash
cp .env.example .env        # e preencha GOOGLE_API_KEY
uv sync                     # instala as dependências (google-adk==2.9.1 fixado)
uv run aurora-restaurar     # restaura reservas e visitantes a partir de dados/
uv run aurora-api           # sobe a API em http://localhost:8000
```

- **Subida:** `uv run aurora-api`. Para parar, use Ctrl+C. Ao subir de novo com o mesmo comando, sessões, eventos, reservas e visitantes continuam lá.
- **Restauração:** `uv run aurora-restaurar` volta reservas e visitantes ao estado de `dados/` e **mantém** as sessões. Para apagar também as sessões e conversas, use `uv run aurora-restaurar --sessoes` com a API parada.
- Os arquivos de `dados/` são só lidos, nunca alterados.

### Exemplo rápido

```bash
S=$(curl -s -X POST localhost:8000/sessoes -H 'Content-Type: application/json' \
      -d '{"apartamento":"101"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["session_id"])')
curl -s -X POST localhost:8000/sessoes/$S/mensagens -H 'Content-Type: application/json' \
      -d '{"texto":"Reserve o salão de festas para 2030-04-20."}'
# copie o id de confirmacoes_pendentes e aprove:
curl -s -X POST localhost:8000/sessoes/$S/confirmacoes -H 'Content-Type: application/json' \
      -d '{"id":"<id>","confirmado":true}'
curl -s localhost:8000/apartamentos/101/reservas
```
