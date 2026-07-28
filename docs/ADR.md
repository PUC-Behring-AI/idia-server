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
