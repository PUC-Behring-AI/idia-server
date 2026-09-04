# ADR.md — Architecture Decision Records

Este documento registra as principais decisões arquiteturais tomadas durante
o desenvolvimento do IDIA Server. Cada entrada descreve o problema, a decisão
tomada, as alternativas consideradas e as consequências.

A manutenção deste documento segue as regras estabelecidas no
[Document Evolution Contract](ARCHITECTURE.md#16-document-evolution-contract)
da arquitetura.

---

## ADR-001: Três camadas (LiteLLM → Ray Serve → vLLM)
**Data:** 2026-06-28 | **Fase:** 1 | **Status:** Accepted

**Contexto:** O servidor precisa servir múltiplos usuários e aplicações com
controle individual de budget, rate-limit e chaves de API. Ray Serve LLM
não oferece autenticação por chave virtual — seu ingress (`OpenAiIngress`)
não valida per-request API keys.

**Decisão:** Arquitetura de três camadas:
- **LiteLLM** (gateway, porta 4000): autenticação via master key + virtual keys,
  budgets, rate-limits, spend tracking, health-check routing.
- **Ray Serve LLM** (orquestração, porta 8000 interna): replica autoscaling
  (scale-to-zero), GPU placement, multi-model routing, LoRA multiplexing.
- **vLLM** (engine, in-process): inference, KV cache management, token generation.

LiteLLM é a única camada exposta ao cliente. Ray Serve e vLLM operam na rede
interna e nunca são acessados diretamente.

**Alternativa descartada:** Duas camadas (Ray Serve → vLLM) — sem o gateway,
cada cliente precisaria de acesso direto ao Ray Serve, sem isolamento de budget
ou rate-limit. Adicionar autenticação ao Ray exigiria um proxy customizado.

**Consequências:** [+ isolamento de segurança claro (3 fronteiras de confiança),
+ reuso de ferramentas OpenAI SDK (LiteLLM expõe API compatível), + separação
de responsabilidades (gateway vs orquestração vs engine); - latência adicional
de um hop HTTP entre LiteLLM e Ray]

---

## ADR-002: Python entrypoint (render_config.py) para templating de config
**Data:** 2026-06-28 | **Fase:** 2 | **Status:** Accepted

**Contexto:** `serve_config.yaml` contém placeholders `${MODEL_ID}`,
`${MODEL_SOURCE}`, `${MAX_MODEL_LEN}`, `${GPU_MEMORY_UTILIZATION}`. Esses
precisam ser substituídos por variáveis de ambiente no momento da execução
do container. As opções eram: (A) `envsubst` (shell), (B) entrypoint Python.

**Decisão:** Entrypoint Python (`scripts/render_config.py`). Ele valida
variáveis obrigatórias (exit 1 se `MODEL_ID` ou `MODEL_SOURCE` ausentes),
injeta defaults para variáveis opcionais (`GPU_MEMORY_UTILIZATION=0.9`,
`MAX_MODEL_LEN=8192`), renderiza o YAML com `re.sub`, valida a estrutura
(resultado é YAML válido com `applications` não-vazia), e executa
`serve run` via `os.execve` (sem fork, sem subprocesso).

**Alternativa descartada:** `envsubst` (A) — não valida estrutura YAML, não
injeta defaults com tipagem, não valida variáveis obrigatórias com mensagens
claras. Exigiria um script shell separado para cada validação, aumentando
a superfície de manutenção.

**Consequências:** [+ validação inline (erro early em env vars faltando),
+ default injection com tipagem (str para MODEL_ID, float para
GPU_MEMORY_UTILIZATION), + dry-run mode para depuração; - dependência de
Python no entrypoint (já presente na imagem base `ray-ml`)]

---

## ADR-003: Pre-render workflow para cluster.yaml
**Data:** 2026-06-28 | **Fase:** 3 | **Status:** Superseded (cluster.yaml removed in local-only simplification)

**Contexto:** `serve_config.yaml` usa placeholders `${VAR}` (ADR-002). O
Ray Cluster Launcher monta arquivos via `file_mounts` como cópia estática
— não substitui env vars. Era preciso um mecanismo para que o config
renderizado chegasse ao cluster.

**Decisão:** Pre-render local antes de `ray up`:
```bash
python3 scripts/render_config.py --dry-run > rendered_config.yaml
ray up -y cluster.yaml
ray exec cluster.yaml "serve run /app/rendered_config.yaml"
```
O `cluster.yaml` monta `./rendered_config.yaml` em `/app/rendered_config.yaml`
via `file_mounts`. O script `scripts/deploy_cluster.sh` automatiza todo o
fluxo: carrega `.env`, valida vars, pre-renderiza, executa `ray up`, executa
`ray exec`.

**Alternativa descartada:** Duas opções foram avaliadas:
- (A) `head_setup_commands` com `export` para injetar env vars: hardcoda
  secrets (HF_TOKEN, LITELLM_MASTER_KEY) no `cluster.yaml`, violando o
  isolamento `.env`.
- (B) Upload do template + render remoto no head node: mais complexo, mais
  pontos de falha (SCP + execução remota), sem ganho sobre pre-render local.

**Consequências:** [+ fluxo simples e verificável (dry-run separa validação
de deploy), + reuso do entrypoint Phase 2; - passo extra antes de `ray up`
(automatizado pelo script)]

---

## ADR-004: Instância GPU g5.xlarge (1× A10G 24 GB) como worker padrão
**Data:** 2026-06-28 | **Fase:** 3 | **Status:** Superseded (cluster.yaml removed in local-only simplification)

**Contexto:** O cluster.yaml precisa de um tipo de instância GPU para os nós
worker. Múltiplas famílias atendem: g5 (A10G), g6 (L4), p4d (A100), p5e (H200).

**Decisão:** `g5.xlarge` (1× A10G 24 GB) como worker padrão. É o melhor
custo-benefício para modelos 7-8B (LLaMA 3.1 8B, Mistral 7B, etc.). O A10G
é a GPU mais comum em oferta spot e on-demand na AWS us-east-1.

**Alternativa descartada:** Duas opções foram avaliadas:
- g6.xlarge (L4): mais recente, ligeiramente mais rápida, mas menos
  disponível e mais cara em muitas regiões.
- p4d.24xlarge (8× A100): desnecessário para 7-8B — só se justifica para
  70B+ com tensor parallelism.
- g5.24xlarge (4× A10G): para cenários de alta demanda, documentado como
  upgrade no `cluster.yaml`.

**Consequências:** [+ menor custo por inferência para modelos 7-8B,
+ ampla disponibilidade spot; - requer service quota increase na conta AWS,
- não adequado para modelos 70B+ sem upgrade explícito]

---

## ADR-005: Prometheus na rede interna, Grafana localhost-only
**Data:** 2026-06-28 | **Fase:** 4 | **Status:** Accepted

**Contexto:** O §9.3 determina que apenas a porta 4000 (LiteLLM) seja
acessível externamente. Prometheus (9090) e Grafana (3000) são ferramentas
operacionais que precisavam de política de acesso definida.

**Decisão:**
- **Prometheus (9090):** não publicado — rede interna do Compose apenas.
  Grafana consulta Prometheus via DNS interno (`http://prometheus:9090`).
  Acesso administrativo via `docker compose exec prometheus sh`.
- **Grafana (3000):** publicado como `127.0.0.1:3000:3000` — acessível
  apenas do host Docker (localhost). Operador acessa via navegador na
  máquina host ou túnel SSH.

**Alternativa descartada:** Expor ambas as portas ao host — violaria §9.3
e aumentaria a superfície de ataque sem necessidade operacional.

**Consequências:** [+ isolamento de rede mantido (apenas 4000 externa),
+ Grafana acessível para operação sem expor ao mundo; - acesso remoto ao
Grafana requer túnel SSH]

---

## ADR-006: Grafana provisioning automático vs configuração manual
**Data:** 2026-06-28 | **Fase:** 4 | **Status:** Accepted

**Contexto:** Para que o Grafana tenha um datasource configurado ao primeiro
acesso, duas abordagens: (A) provisioning automático via YAML, (B) configuração
manual via UI.

**Decisão:** Provisioning automático via `grafana/datasources/datasource.yml`.
O arquivo declara um datasource Prometheus apontando para
`http://prometheus:9090`, configurado como default, access mode `proxy`.
O Grafana detecta o arquivo na inicialização e configura o datasource
automaticamente — zero cliques.

**Alternativa descartada:** Configuração manual (B) — frágil (operador precisa
saber a URL e o tipo), não versionada, precisa ser refeita em cada rebuild.

**Consequências:** [+ zero configuração manual na primeira execução,
+ versionado no repositório, + reproduzível; - requer diretório de
provisionamento mapeado no `docker-compose.yml`]

---

## ADR-007: Alertas no Grafana (não no Prometheus Alertmanager)
**Data:** 2026-06-28 | **Fase:** 4 | **Status:** Accepted

**Contexto:** O §10.3 recomenda 5 alertas (KV-cache saturation, replica
ceiling, cluster max_workers, cold-start spike, dashboard exposure). A
infraestrutura de alertas precisava de um destino.

**Decisão:** Alertas configurados no Grafana (native alerting engine), não
no Prometheus Alertmanager. O `prometheus.yml` não declara `rule_files`
— alertas no Grafana são mais simples de configurar (UI nativa com suporte
a silences, routing, e notificações integradas).

**Alternativa descartada:** Prometheus + Alertmanager — adiciona um serviço
extra (`alertmanager`) ao stack, aumenta complexidade operacional, e o
Alertmanager requer configuração YAML complexa para roteamento.

**Consequências:** [+ simplicidade operacional, + UI nativa para gerenciar
alertas; - alertas não são versionados como código (configurados via UI),
- dependência de Grafana estar rodando para avaliar alertas]

---

## ADR-008: Licença Apache 2.0
**Data:** 2026-06-28 | **Fase:** 5 | **Status:** Accepted

**Contexto:** O IDIA Server foi desenvolvido no PUC-Behring Institute for AI,
uma instituição de pesquisa brasileira. O repositório é público e precisa
de uma licença que proteja a instituição e incentive o uso acadêmico e
comercial.

**Decisão:** Apache License 2.0. Ela é permissiva (como MIT), mas inclui
proteção explícita de patentes — crucial para instituições de pesquisa que
podem gerar propriedade intelectual. É a licença usada por TensorFlow,
PyTorch, Kubernetes, e pela maioria dos projetos de infraestrutura de IA.

**Alternativa descartada:** Três alternativas foram avaliadas:
- MIT: proteção insuficiente de patentes; não exige notice de alterações.
- GPLv3: muito restritiva para parcerias com indústria; pode desencorajar
  adoção comercial.
- CC-BY-NC: incompatível com a missão do instituto (permite apenas uso
  não-comercial).

**Consequências:** [+ proteção de patentes para o instituto,
+ compatibilidade com projetos de IA existentes (Apache 2.0 é a licença
padrão do ecossistema), + permite uso acadêmico e comercial; - não é
copyleft (alterações podem ser fechadas)]

---

## ADR-009: Visibilidade de modelos no Open WebUI — padrão override + access grants
**Data:** 2026-09-03 | **Fase:** Provisionamento | **Status:** Accepted

**Contexto:** Era preciso dar aos colegas uma interface web com autenticação
individual, mostrando no dropdown apenas os modelos que cada pessoa pode usar.

O `get_all_models()` do Open WebUI (`utils/models.py`) tem dois branches para
entradas na tabela `model`:

```python
if custom_model.base_model_id is None:    # OVERRIDE
    model['info'] = custom_model.model_dump()
elif custom_model.is_active:              # DERIVED
    if custom_model.id in existing_ids: continue   # ← silencioso
```

Com `base_model_id` preenchido, o código toma o branch "derived", faz
`continue`, e `model['info']` nunca é populado. O `get_filtered_models()`
então vê `model_info=None` e esconde o modelo de todo mundo que não seja
admin — sem erro, sem log, sem pista.

Descobrir isso custou uma sessão inteira: as entradas tinham sido criadas
com `base_model_id` igual ao próprio `id`, o que parecia razoável e era
exatamente o que ativava o branch errado. `user_id=NULL` agravava, falhando
a validação Pydantic (`ModelModel.user_id: str`, obrigatório).

**Decisão:**

1. Uma entrada na tabela `model` por modelo configurado, com
   `base_model_id = NULL` (ativa o branch "override") e
   `user_id = <uuid do admin>` (satisfaz o Pydantic).
2. Tabela `access_grant`: uma linha por usuário × modelo autorizado,
   com `permission = 'read'`.
3. O `colleague.sh` mantém as duas tabelas no passo [5/6], recriando os
   grants a cada provisionamento e apagando-os no revoke.
4. O LiteLLM permanece como segunda camada: a virtual key também lista os
   modelos permitidos, então esconder no dropdown não é o único controle.

**Alternativa descartada:** `BYPASS_MODEL_ACCESS_CONTROL=true`. Todos veem
todos os modelos. Rejeitada porque remove o controle de visibilidade por
completo — o dropdown fica poluído de modelos inacessíveis, e a pessoa
descobre a restrição só quando a requisição falha.

**Consequências:** [+ dropdown mostra apenas o que a pessoa pode usar;
+ grants gerenciados automaticamente, sem passo manual; + revoke limpa tudo,
sem lixo acumulado; − dependência do schema interno do SQLite do Open WebUI,
que pode mudar entre versões — mitigado pelo pin por digest, ver a nota abaixo]

**Nota de 2026-09-04 — a versão contra a qual este ADR foi verificado.**

A decisão acima descreve o schema de uma versão específica do Open WebUI, e
até esta data o `docker-compose.yml` puxava a imagem da tag `:main`. Uma tag
que se move não é uma versão: qualquer push upstream trocava o schema por
baixo do provisionamento, e o `create` falharia no meio — chave já emitida no
LiteLLM, conta não criada, artefato órfão para limpar à mão.

Que a deriva era real, e não hipótese, foi medido no dia: a imagem local com
que o provisionamento foi escrito é `sha256:a26effeb…`, de 2026-07-01, e a
tag `:main` no registry já apontava para `sha256:8afd2d77…`. Um
`docker compose pull` teria trocado a imagem.

A imagem passa a ser referenciada por digest, sem tag:

| | |
|---|---|
| digest | `sha256:a26effeb220e132482bf7e0560b3404843e7bc40d23051144e062960df8df6b0` |
| build | 2026-07-01, commit upstream `ecd48e2f718220a6400ecf49eafd4867a38feb10` |
| plataformas | índice OCI com `linux/amd64` e `linux/arm64` |
| tabelas verificadas | `user`, `auth`, `api_key`, `access_grant`, `model`, `config` |

**Atualizar a imagem passa a ser uma decisão**, e ela pede três coisas: reler
este ADR, reverificar as seis tabelas contra a nova versão, e trocar o digest
com o novo commit registrado aqui.

Uma correção de fato à consequência original, que dizia que "o teste de
provisionamento é o que detecta" uma mudança de schema: **não detecta.** O
teste dirige o provisionamento contra um Open WebUI simulado, que concorda com
o script por construção — nenhum teste hoje lê o schema real (#37). Enquanto
esse teste não existir, o pin é a única proteção, não a segunda.

---

## ADR-010: Provisionamento de usuários via colleague.sh
**Data:** 2026-09-03 | **Fase:** Provisionamento | **Status:** Accepted

**Contexto:** Dar acesso a um colega envolvia três sistemas e três passos
manuais: criar a virtual key no LiteLLM, criar a conta no Open WebUI, e
vincular a chave à conta. Cada passo tinha seu próprio jeito de falhar
silenciosamente, e o resultado parcial não era visível em lugar nenhum.

O requisito era simples de enunciar: o admin roda um comando, a pessoa
recebe um e-mail e uma senha, e tudo funciona.

**Decisão:** Um script (`scripts/colleague.sh`), seis passos, um comando:

| Passo | Ação |
|---|---|
| [1/6] | Apaga qualquer key LiteLLM com o mesmo alias |
| [2/6] | Cria a virtual key (tier, budget, rate limits, modelos) |
| [3/6] | Cria a conta no Open WebUI, ou atualiza a senha se já existir |
| [4/6] | Vincula a chave na tabela `api_key` (`key_{user_id}`) |
| [5/6] | Garante as entradas de `model` e recria os `access_grant` (ADR-009) |
| [6/6] | Grava a config global do dropdown |

O `revoke` desfaz os cinco artefatos: key, grants, api_key, auth e usuário.

Três propriedades foram escolhidas deliberadamente:

- **Valores chegam ao Python por `argv`, nunca por interpolação no fonte.**
  A versão anterior montava código Python concatenando strings do bash; um
  sobrenome com apóstrofo produzia `SyntaxError` no meio do provisionamento,
  deixando uma key órfã. Os heredocs usam delimitador entre aspas (`<<'PY'`),
  então o bash não expande nada dentro deles.
- **Nenhum segredo ou endereço no código.** `IDIA_PUBLIC_HOST` e
  `OWUI_DISCOVERY_KEY` vêm do `.env`. A versão anterior carregava ambos como
  literais, prestes a serem publicados no primeiro push.
- **Sem arrays associativos.** Os tiers são um `case`, não `declare -A`, para
  que o script rode no bash 3.2 do macOS e possa ser testado fora do servidor.

Os modelos de cada tier vêm da configuração do servidor (`MODEL_ID` ou
`MODELS_COUNT`/`MODEL_N_ID`), não de uma lista fixa. Isso evita a deriva em
que o script anunciava modelos que o servidor não servia mais.

**Alternativa descartada:** integração OAuth/SSO entre Open WebUI e LiteLLM.
Ambos suportam OAuth, mas o mapeamento de usuários entre os dois exigiria
middleware próprio — complexidade desproporcional para provisionar dezenas
de pessoas, não milhares.

**Consequências:** [+ provisionamento em um comando; + revoke completo, sem
resíduo; + testável sem Docker e sem servidor (`--dry-run`, `tiers`);
− manipulação direta do SQLite do Open WebUI (ver ADR-009)]

**Atualização (issue #6):** `create_user.sh` foi removido e o `./idia user create` passou a delegar para `colleague.sh key`. Havia dois vocabulários de tier — `hard/regular/light` e `light/regular/heavy/classroom` — com limites diferentes para o mesmo nome, e o primeiro não aplicava limite nenhum. Agora existe uma definição só. `hard` continua sendo aceito como sinônimo de `heavy`, com aviso.

---

## ADR-011: Tool calling via `enable_auto_tool_choice`, sem `tool_call_parser`
**Data:** 2026-09-03 | **Fase:** Configuração de engine | **Status:** Accepted

**Contexto:** Toda interface moderna — Open WebUI, Continue.dev, Aider,
Cursor — manda `tool_choice: "auto"` junto de `tools: [...]` em cada
requisição, mesmo quando a conversa não usa ferramenta nenhuma. O vLLM
recusa esses pedidos de saída:

```
auto tool choice requires --enable-auto-tool-choice and
--tool-call-parser to be set
```

Ou seja: sem isso, a interface web do instituto não conversa. Não é um
recurso avançado que fica faltando — é a requisição comum que falha.

A leitura literal da mensagem levou a adicionar os dois: `enable_auto_tool_choice`
**e** `tool_call_parser: "hermes"`. O resultado foi pior que o erro original —
o GPU worker morria durante `_initialize_kv_caches`, nos cinco modelos Qwen3
testados. A investigação mostrou por quê: o Qwen3 traz chat template próprio,
com tags `<tool_call>` nativas. O parser "hermes" sobrescrevia esse template e
os dois entravam em conflito.

**Decisão:** `enable_auto_tool_choice: true` em toda entrada gerada, e
**nenhum** `tool_call_parser`. O vLLM usa o chat template nativo do modelo.

A opção é incondicional no `MODEL_CONFIG_TEMPLATE`, não configurável por
modelo: um servidor cujo tool calling depende de o operador lembrar de ligar
uma flag é um servidor que vai chegar quebrado na mão do usuário.

**Alternativa descartada:** `tool_call_parser: "hermes"` para todos os
modelos. Derrubou o worker em 100% dos casos testados.

**Consequências:** [+ compatibilidade com qualquer cliente OpenAI sem
configuração; + sem conflito com templates nativos; − exige que o modelo
tenha chat template com suporte a tools — Qwen3 e Mistral Instruct têm;
um modelo sem isso aceitaria o parâmetro e ignoraria as ferramentas]

---

## ADR-012: PostgreSQL em container local para as virtual keys do LiteLLM
**Data:** 2026-09-03 | **Fase:** Gateway | **Status:** Accepted

**Contexto:** O `ARCHITECTURE.md` §1 diz que a camada de gateway existe
**especificamente** porque este deployment serve vários usuários, cada um com
budget e rate-limit próprios. Sem banco, o LiteLLM v1.85.0 não emite uma única
virtual key: `/key/generate` não tem onde persistir. O que sobra é a master
key — a credencial de administrador, que a §9.1 diz explicitamente nunca ser
distribuída.

Ou seja: a stack subia descrevendo um sistema multiusuário e só conseguia
atender um único cliente, com a chave errada.

SQLite foi tentado (`DATABASE_URL=sqlite://...`) e recusado pelo próprio
Prisma: o schema embutido na imagem Docker é hardcoded com
`provider = "postgresql"`.

**Decisão:** um container `postgres:16-alpine` no `docker-compose.yml`, com
volume nomeado `postgres_data`, healthcheck `pg_isready` e nenhuma porta
publicada. O LiteLLM recebe `DATABASE_URL` e depende do banco via
`condition: service_healthy` — sem isso ele sobe antes do Postgres aceitar
conexão e falha o boot na primeira migração.

`POSTGRES_PASSWORD` e `UI_PASSWORD` usam a forma `${VAR:?mensagem}` no
compose: sem valor no `.env`, o `docker compose` recusa subir com uma
mensagem nomeando a variável, em vez de subir com senha vazia.

**Alternativa descartada:** PostgreSQL gerenciado (AWS RDS). Rejeitado por
custo e complexidade desproporcionais a um deployment de instância única —
o Postgres ocupa ~50 MB de RAM ociosa numa máquina que tem dezenas de GB.
A troca continua possível: basta apontar `DATABASE_URL` para outro host.

**Consequências:** [+ virtual keys, budgets, rate limits e spend tracking
passam a funcionar; + o painel admin em `:4000/ui` fica utilizável;
− mais um container e mais um volume no ciclo de vida da stack;
− **sem backup automático** — o volume `postgres_data` guarda as chaves de
todos os usuários e o histórico de gasto, e perdê-lo é perder ambos. Um
`pg_dump` periódico é trabalho pendente, não coberto por este ADR]

---

## ADR-013: Open WebUI como serviço do Compose, publicado na 3001
**Data:** 2026-09-03 | **Fase:** Interface | **Status:** Accepted

**Contexto:** A interface que todos os usuários do instituto abrem existia
apenas como um `docker run` copiado de um bloco da documentação. Fora do
Compose, ela não tinha healthcheck, não reiniciava sozinha, não aparecia em
`./idia status`, não parava com `./idia stop`, e seu volume não estava
declarado. Reiniciar a máquina trazia tudo de volta menos a interface.

Havia um segundo problema mais silencioso: a rede do `docker run` era
`--network idia-server_default`, nome derivado do diretório onde o
repositório foi clonado. Clonar em `~/idia` em vez de `~/idia-server`
quebrava a conexão com o LiteLLM, e a mensagem de erro não dizia nada sobre
nomes de diretório.

**Decisão:** serviço `open-webui` no `docker-compose.yml`, com
`container_name` fixo (`idia-webui` por padrão), `depends_on: litellm` com
`condition: service_healthy`, healthcheck, limite de memória, `restart:
unless-stopped` e volume nomeado `webui_data`.

O nome do container é fixo de propósito: o `colleague.sh` acessa o SQLite
dentro dele para criar contas e grants de modelo (ADR-009). Um container
renomeado quebra o provisionamento, então o nome é configuração declarada,
não convenção implícita.

`ENABLE_SIGNUP=false`: uma conta auto-registrada não tem virtual key nem
`access_grant`, então o usuário entra e encontra um dropdown vazio. Contas
nascem pelo `./idia colleague create`, que é o que amarra as três coisas.

**A porta 3001 é publicada, e isso é uma exceção consciente à §9.1.** A regra
"só a 4000 é externa" foi escrita quando o único cliente era um SDK. A
interface web precisa ser alcançável das máquinas das pessoas, e um serviço
que ninguém alcança não serve para nada. O que a exceção **não** dispensa:
o Open WebUI não termina TLS, então em qualquer rede que não seja já
confiável ele pertence atrás de um proxy reverso que o faça. `OWUI_PORT`
existe para quem quiser mover a porta ou publicá-la apenas em `127.0.0.1`
e tunelar.

**Alternativa descartada:** manter o `docker run` documentado. É o estado
que produziu os problemas acima; documentar melhor não faz o container
reiniciar depois de um reboot.

**Consequências:** [+ a interface entra no ciclo de vida da stack;
+ `./idia stop` e `./idia status` passam a enxergá-la; + o volume é
declarado e sobrevive a `docker compose down`; − uma segunda porta externa,
sem TLS por padrão; − o `colleague.sh` fica acoplado ao nome do container,
agora ao menos declarado em um lugar só]
