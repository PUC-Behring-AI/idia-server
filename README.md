# IDIA Server — Inference & Deployment for Intelligent Agents

**Servidor de inferência LLM auto-hospedado com elasticidade automática de GPU
e carregamento sob demanda de modelos, implantável em qualquer host
multi-GPU via Docker Compose.**

[![Phase](https://img.shields.io/badge/phase-5%20Complete-brightgreen)](https://github.com/PUC-Behring-Institute-for-AI/idia-server)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://python.org)
[![Stack](https://img.shields.io/badge/stack-Ray%20Serve%20%7C%20vLLM%20%7C%20LiteLLM-orange)]()
[![License](https://img.shields.io/badge/license-Apache%202.0-green)](LICENSE)

---

- [1. Visão Geral](#1-visão-geral)
- [2. Arquitetura (Resumo Executivo)](#2-arquitetura-resumo-executivo)
- [3. Pré-requisitos](#3-pré-requisitos)
- [4. Início Rápido](#4-início-rápido)
- [5. Estrutura do Repositório](#5-estrutura-do-repositório)
- [6. Roadmap de Implementação](#6-roadmap-de-implementação)
- [7. Guia de Testes](#7-guia-de-testes)
- [8. Configuração](#8-configuração)
- [9. Segurança](#9-segurança)
- [10. Targets de Deploy](#10-targets-de-deploy)
- [11. Monitoramento](#11-monitoramento)
- [12. Gerenciamento de Usuários](#12-gerenciamento-de-usuários)
- [13. Multi-Model](#13-multi-model)
- [14. Mantenedor](#14-mantenedor)
- [15. Licença](#15-licença)
- [16. Referências](#16-referências)

---

## 1. Visão Geral

O **IDIA Server** é a plataforma de inferência de LLM do **PUC-Behring Institute
for AI**. Ele foi projetado para servir modelos de linguagem de grande porte
(LLMs) para múltiplos usuários e aplicações dentro do instituto, com controle
individual de orçamento e taxa de requisições, escalonamento automático de
réplicas (incluindo scale-to-zero para eliminar custo ocioso), e capacidade de
implantação idêntica em um servidor local multi-GPU e em qualquer instância com GPU via Docker Compose.

### Propriedades-chave

- **Três camadas bem definidas**: LiteLLM (gateway/auth), Ray Serve LLM
  (orquestração GPU), vLLM (motor de inferência) — cada camada com
  responsabilidades estritas e sem vazamento entre elas.
- **Elasticidade automática de GPU**: o autoscaler de réplicas do Ray Serve ajusta
  o número de réplicas por modelo conforme a demanda, até o limite de GPUs disponíveis.
- **Scale-to-zero**: modelos ociosos não ocupam GPU. A primeira requisição após
  um período ocioso dispara o carregamento automático (cold start) sem
  intervenção manual.
- **Carregamento sob demanda**: múltiplos modelos podem ser declarados com
  `min_replicas: 0`; cada um é carregado apenas quando recebe a primeira
  requisição.
- **Implantação via Docker Compose**: o mesmo `docker-compose.yml` funciona em
  qualquer host com GPU — laptop, workstation, ou instância cloud.
- **Segurança por isolamento de rede**: apenas a porta 4000 (LiteLLM) é exposta
  ao host. As portas internas do Ray (8000, 8265, 10001) nunca são mapeadas no
  `docker-compose.yml`.
- **Gateway com autenticação e budgets**: o LiteLLM gerencia chaves virtuais
  por usuário/equipe com limites de RPM e orçamento, eliminando a necessidade
  de autenticação no Ray.

Para a especificação arquitetural completa (725+ linhas), consulte
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Este README é um resumo
executivo e guia de operação; a arquitetura é o documento de referência
definitivo.

---

## 2. Arquitetura (Resumo Executivo)

### 2.1 Diagrama de fluxo

```
Cliente (app / script / curl)
        │  HTTPS, OpenAI request format, virtual key
        ▼
┌─────────────────────────┐
│  LiteLLM      (:4000)    │  CPU only — auth, budget, rate-limit, spend tracking
│  ─────────────────────   │
│  Porta ÚNICA exposta     │  ← toda requisição chega aqui
└──────────┬───────────────┘
           │ internal network only — nunca exposto externamente
           ▼
┌─────────────────────────┐
│  Ray Serve LLM (:8000)   │  Autoscaling, GPU placement, multi-model/LoRA routing
│  ─────────────────────   │
│  Porta interna           │  ← inacessível de fora do container
└──────────┬───────────────┘
           │ in-process / mesma GPU — sem barreira de rede
           ▼
┌─────────────────────────┐
│  vLLM engine instance(s) │  GPU — model weights + KV cache, por réplica
└─────────────────────────┘
```

### 2.2 As três camadas

| Camada | Componente | Porta | Responsabilidade | Não é responsabilidade |
|--------|-----------|-------|------------------|----------------------|
| Gateway | **LiteLLM** | `:4000` (externa) | Autenticação, chaves virtuais, budgets por chave, rate limits, registro de gastos, unificação opcional com APIs comerciais | Posicionamento GPU, autoscaling, carregamento de modelo |
| Orquestração | **Ray Serve LLM** | `:8000` (interna) | Autoscaling de réplicas (incluindo scale-to-zero), alocação GPU-aware, roteamento multi-modelo, multiplexação LoRA com evicção LRU | Autenticação por usuário, provedores externos |
| Motor | **vLLM** | in-process | Inferência: pesos em VRAM, KV cache (PagedAttention), continuous batching, geração de tokens | Tudo acima — é um motor single-modelo sem conceito de usuários, chaves ou outros modelos |

### 2.3 Os dois autoscalers

O sistema tem dois loops de autoscaling independentes em granularidades diferentes.
Confundi-los é a fonte mais comum de erro ao raciocinar sobre capacidade e custo.

| | Ray Serve Autoscaler | Ray Cluster Autoscaler |
|---|---|---|
| **Escopo** | Um deployment (um modelo) | O cluster inteiro (todos os nós) |
| **Adiciona/remove** | Réplicas (processos) | Nós (VMs) |
| **Gatilho** | `target_ongoing_requests` excedido para aquele deployment | Demanda agregada de recursos excede o que os nós atuais fornecem |
| **Onde configurar** | `autoscaling_config` dentro de cada `LLMConfig` |
| **Ativo localmente?** | Sim | Não (só em cloud) |

Em um host multi-GPU, o **autoscaler de réplicas** opera automaticamente:
precisa de mais uma réplica → se nenhum slot GPU estiver livre → o cluster
autoscaler solicita uma nova instância EC2.

### 2.4 Ciclo de vida de uma requisição

1. Cliente envia requisição ao LiteLLM com uma chave virtual.
2. LiteLLM valida a chave (budget/RPM), resolve `model_name` para URL do Ray
   Serve.
3. LiteLLM encaminha para o ingress do Ray Serve LLM
   (`http://ray-head:8000/v1/...`).
4. Ray Serve roteia para o deployment do modelo correto. Se o deployment está
   em `min_replicas: 0` e ocioso, esta requisição dispara um **cold start**.
5. A réplica do deployment (instância do vLLM) admite a requisição no batch
   corrente (continuous batching) e gera tokens.
6. Tokens fluem de volta: vLLM → Ray Serve → LiteLLM → cliente.
7. LiteLLM registra custo/latência contra a chave virtual.

Para a especificação detalhada de cada camada, parâmetros de configuração,
deploy local, fine-tuning, custos e troubleshooting, consulte o documento
completo em [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## 3. Pré-requisitos

### 3.1 Para deploy local (todas as fases)

| Requisito | Versão mínima | Verificar com |
|-----------|--------------|---------------|
| NVIDIA driver | Compatível com a GPU | `nvidia-smi` |
| NVIDIA Container Toolkit | — | `nvidia-ctk runtime configure --runtime=docker` |
| Docker Engine | 24+ | `docker --version` |
| Docker Compose | **v2** (`docker compose`, não `docker-compose`) | `docker compose version` |
| Espaço em disco | ~16 GB (modelo 8B FP16) | `df -h ~/.cache/huggingface` |
| GPU NVIDIA | Compute capability ≥ 7.0 (V100 ou mais recente) | `nvidia-smi --query-gpu=name --format=csv,noheader` |
| Python | 3.11+ | `python --version` |

### 3.2 Para desenvolvimento e testes

| Requisito | Versão | Instalação |
|-----------|--------|------------|
| Python | 3.11+ | — |
| pytest | 8.x | `pip install pytest` |
| PyYAML | 6.x | `pip install pyyaml` (opcional para testes config) |

---

## 4. Início Rápido

> O IDIA Server usa uma CLI unificada (`./idia`) como ponto de entrada único.
> Não use `docker compose up` diretamente — a CLI garante que os configs sejam
> renderizados antes de subir os containers.
>
> **Guia detalhado:** [`docs/DEPLOY.md`](docs/DEPLOY.md) — cobre pré-requisitos,
> passo a passo com saída esperada, multi-model, integração com
> clientes (Python, LangChain, OpenCode), e troubleshooting.

### 4.1 Preparação (uma vez)

```bash
# 1. Clonar o repositório
git clone https://github.com/PUC-Behring-Institute-for-AI/idia-server.git
cd idia-server

# 2. Criar arquivo de configuração
cp .env.example .env
# Editar .env com os seus valores:
#   HF_TOKEN         — token HuggingFace (https://huggingface.co/settings/tokens)
#   LITELLM_MASTER_KEY — chave admin (ex: sk-admin-minha-chave-secreta)
#   MODEL_ID         — alias curto (ex: mistral-7b)
#   MODEL_SOURCE     — repositório HF (ex: mistralai/Mistral-7B-Instruct-v0.3)
#   GRAFANA_ADMIN_PASSWORD — senha do Grafana
vim .env
```

### 4.2 Subir o servidor (um comando)

```bash
./idia deploy local
```

O que acontece automaticamente:
1. **Valida** o `.env` (detecta placeholders não substituídos)
2. **Renderiza** `rendered_serve_config.yaml` e `rendered_litellm_config.yaml`
3. **Inicia** os containers (`docker compose up -d`)
4. **Aguarda** o servidor ficar pronto (timeout: 10 min — cold start baixa os pesos)
5. **Executa** smoke test nos modelos configurados

> **Primeiro boot:** a primeira vez que o servidor sobe, os pesos do modelo são
> baixados do HuggingFace (~15 min para um modelo 8B). As execuções seguintes
> usam o cache Docker volume `idia_hf_cache` e sobem em ~1 min.

### 4.2.1 Inicialização automática no boot (opcional)

Para garantir que o servidor suba automaticamente quando a máquina ligar:

```bash
sudo ./idia service install      # Instala serviço systemd + inicia
sudo ./idia service uninstall    # Remove (para de iniciar no boot)
./idia service status            # Verifica status do serviço
```

Após instalado, o servidor:
- Sobe automaticamente em todo reboot
- Se recupera de falhas via `restart: unless-stopped` do Docker
- ⚠️ Virtual keys são perdidas em todo restart — recrie após reboot (§4.3)

### 4.3 Criar usuários

```bash
# Criar chave para uma pesquisadora (15 RPM / 50k TPM)
./idia user create alice hard

# Criar chave para um mestrando (4 RPM / 15k TPM)
./idia user create bob regular

# Criar chave para aluno de graduação (1 RPM / 5k TPM)
./idia user create carol light
```

### 4.4 Verificar saúde do sistema

```bash
./idia status
```

### 4.5 Uso programático (OpenAI-compatible)

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:4000",
    api_key="sk-...",   # chave virtual criada com ./idia user create
)

response = client.chat.completions.create(
    model="mistral-7b",
    messages=[{"role": "user", "content": "O que é continuous batching?"}],
    stream=True,
)

for chunk in response:
    print(chunk.choices[0].delta.content or "", end="")
```

### 4.6 Referência rápida da CLI

```
./idia setup                                    Configurar ambiente (Docker Compose, Python, .env)
./idia deploy local [--dry-run] [--no-wait]   Subir servidor local
./idia service install                         Instalar serviço systemd (auto-start no boot)
./idia service uninstall                       Remover serviço systemd
./idia service status                          Status do serviço systemd
./idia status                                  Saúde dos serviços + modelos carregados
./idia user create <nome> <tier>               Criar chave virtual (hard/regular/light)
./idia user list                               Listar chaves ativas
./idia logs [serviço]                          Ver logs
./idia stop                                    Parar servidor
./idia --help                                  Ajuda completa
```

---

## 5. Estrutura do Repositório

```
idia-server/
├── idia                   ← CLI unificada — único ponto de entrada (✓)
├── AGENTS.md              ← Regras do projeto para agentes OpenCode (Phase 1 ✓)
├── .gitignore             ← Inclui rendered_*.yaml (gerados, não versionados)
├── pyproject.toml         ← Configuração pytest + ruff (Phase 1 ✓)
├── .env.example           ← Template de secrets (Phase 2 ✓)
├── Dockerfile.ray         ← Imagem Ray Serve LLM (Phase 2 ✓)
├── serve_config.yaml      ← Template Ray Serve com ${VAR} placeholders (Phase 2 ✓)
├── config.yaml            ← Template LiteLLM com ${VAR} placeholders (Phase 2 ✓)
├── docker-compose.yml     ← Orquestração local (Phase 2 ✓)
├── prometheus.yml         ← Configuração de scrape (Phase 4 ✓)
├── scripts/
│   ├── render_config.py   ← Renderiza serve_config + litellm_config (Phase 2 ✓)
│   ├── smoke_test.sh      ← Smoke test pós-deploy com --wait (Tier 4 ✓)
│   └── create_user.sh     ← Criação de chaves virtuais LiteLLM (Tier 4 ✓)
├── grafana/
│   ├── datasources/datasource.yml   ← Provisioning automático do Prometheus
│   └── dashboards/
│       ├── dashboard.yml            ← Provider config
│       └── vllm-dashboard.json      ← Dashboard oficial vLLM (ID 25043)
├── tests/
│   ├── conftest.py             ← Fixtures compartilhadas
│   ├── test_docs.py            ← Estrutura de documentação (docs marker)
│   ├── test_config_schemas.py  ← Schema YAML de todos os configs (config marker)
│   ├── test_integration.py     ← render_config, multi-model, litellm render (integration)
│   ├── test_security.py        ← Portas, pinning, fronteiras de confiança (security)
│   └── test_contract.py        ← Contratos REST LiteLLM sem GPU (integration)
├── docs/
│   ├── ARCHITECTURE.md         ← Documento vivo (~1200 linhas)
│   ├── DEPLOY.md               ← Guia de operações completo (local)
│   ├── ADR.md                  ← Architecture Decision Records
│   └── audit_logs/             ← Relatórios de auditoria vetados
└── README.md                   ← Este arquivo
```

> **Arquivos gerados (não versionados):**
> `rendered_serve_config.yaml` e `rendered_litellm_config.yaml` são gerados
> por `./idia deploy local` antes de `docker compose up`. Eles estão no
> `.gitignore` — nunca commitar.

---

## 6. Roadmap de Implementação

O projeto está sendo desenvolvido em 5 fases incrementais, cada uma com revisão
humana antes de avançar para a próxima.

| Fase | Nome | Status | Entregáveis |
|------|------|--------|-------------|
| **1** | Fundação | ✅ **Concluída** | `AGENTS.md`, `.gitignore`, `pyproject.toml`, `tests/`, `docs/ARCHITECTURE.md`, `README.md` |
| **2** | Build Core | ✅ **Concluída** | `Dockerfile.ray`, `serve_config.yaml`, `docker-compose.yml`, `config.yaml`, `.env.example`, `render_config.py` |
| **3** | Deploy AWS | ✅ **Removida** | Substituída pela simplificação local-only — o autoscaler de GPUs locais supre a necessidade |
| **4** | Monitoramento | ✅ **Concluída** | `prometheus.yml`, Grafana + DCGM exporter, dashboards provisionados |
| **5** | Documentação + Automação | ✅ **Concluída** | `./idia` CLI, dual render (LiteLLM config), Tier 4 scripts, ADR.md, LICENSE |
| **6** | Simplificação local-only | ✅ **Concluída** | Removidos scripts AWS (deploy_cluster, cache, create_sg, create_iam_role), cluster.yaml, testes Floci |

Ao final de cada fase, a suíte de testes é executada para garantir que
nenhuma regressão foi introduzida.

---

## 7. Guia de Testes

### 7.1 Categorias

A suíte de testes usa **pytest 8.x** com quatro marcadores (**109 testes, 0 falhas**):

| Marcador | Categoria | O que valida | Requer infra? |
|----------|-----------|-------------|--------------|
| `docs` | Documentação | Estrutura de arquivos, seções de docs vivos, ADR, LICENSE | Não |
| `config` | Schema YAML | Todos os configs: `serve_config.yaml`, `docker-compose.yml`, `config.yaml`, `prometheus.yml`, Grafana | Não |
| `integration` | Integração + Render | `render_config.py` (serve + litellm), VRAM budget, multi-model, contratos LiteLLM | Não (pure Python) |
| `security` | Segurança | Isolamento de portas, pinning de imagens, fronteiras de confiança, DCGM | Não |

### 7.2 Como executar

```bash
# Instalar dependências de teste
pip install pytest pyyaml

# Testes rápidos (docs + config) — zero infraestrutura
pytest -m "docs or config" -v

# Suite completa (inclui integração e segurança)
pytest -v

# Testes de um arquivo específico
pytest tests/test_config_schemas.py -v

# Por marcador
pytest -m config -v
```

### 7.3 Política de skip

Testes que dependem de arquivos de fases futuras usam `pytest.skip()` com
mensagem explicativa — nunca falham pela ausência de algo que ainda será criado.
Isso garante que a suíte rode limpa desde a Fase 1.

### 7.4 Casos de teste atuais (Fase 1)

Em `tests/test_docs.py`:

| Teste | O que verifica |
|-------|---------------|
| `test_exists` | Cada arquivo obrigatório (`docs/ARCHITECTURE.md`, `AGENTS.md`, `README.md`) existe |
| `test_is_markdown` | Arquivos começam com `#` (cabeçalho markdown) |
| `test_contains_sections` | Documentos vivos contêm as seções de governança exigidas |
| `test_has_version_footer` | `ARCHITECTURE.md` tem footer de versão e tabela de histórico estrutural |

Em `tests/test_config_schemas.py`:

| Classe de teste | Arquivo alvo | Asserções principais |
|----------------|-------------|---------------------|
| `TestServeConfig` | `serve_config.yaml` | `proxy_location: EveryNode`, `http_options.port: 8000`, `applications` é lista não-vazia |
| `TestDockerCompose` | `docker-compose.yml` | Serviços `ray-head` e `litellm` presentes; `ipc: host` e `shm_size` em ray-head |
| `TestLiteLLMConfig` | `config.yaml` | `model_list` e `general_settings` presentes; master_key declarado |
| `TestPrometheusConfig` | `prometheus.yml` | `global` e `scrape_configs`; targets apontam para `ray-head:8080` e `litellm:4000` |
| `TestEnvExample` | `.env.example` | Declara `HF_TOKEN`, `LITELLM_MASTER_KEY`, `MODEL_ID`, `MODEL_SOURCE` |

---

## 8. Configuração

### 8.1 Variáveis obrigatórias

| Variável | Exemplo | Descrição |
|----------|---------|-----------|
| `HF_TOKEN` | `hf_xxx...` | Token HuggingFace para download de modelos |
| `LITELLM_MASTER_KEY` | `sk-admin-minha-chave` | Admin credential do LiteLLM |
| `MODEL_ID` | `mistral-7b` | Alias que clientes usam no campo `model` |
| `MODEL_SOURCE` | `mistralai/Mistral-7B-Instruct-v0.3` | Repositório no HuggingFace Hub |
| `GRAFANA_ADMIN_PASSWORD` | `sua-senha-grafana` | Senha do painel Grafana |

### 8.2 Variáveis opcionais (com defaults)

| Variável | Default | Descrição |
|----------|---------|-----------|
| `MAX_MODEL_LEN` | `8192` | Contexto máximo em tokens |
| `GPU_MEMORY_UTILIZATION` | `0.9` | Fração da VRAM para pesos + KV cache (0–1] |
| `GPU_COUNT` | `1` | Número de GPUs no host — usado para validação VRAM |
| `GPU_VRAM_GB` | `24.0` | VRAM por GPU em GB (A10G=24, A100=80) |
| `RAY_SHM_SIZE` | `4gb` | Shared memory para o container Ray |
| `RAY_MEMORY_LIMIT` | `16g` | Limite de RAM para o container Ray |

### 8.3 Variáveis para multi-model

Quando `MODELS_COUNT > 0`, as variáveis `MODEL_ID`/`MODEL_SOURCE` são ignoradas
e substituídas por entradas numeradas:

| Variável | Exemplo |
|----------|---------|
| `MODELS_COUNT` | `2` |
| `MODEL_1_ID` | `mistral-7b` |
| `MODEL_1_SOURCE` | `mistralai/Mistral-7B-Instruct-v0.3` |
| `MODEL_2_ID` | `qwen-2.5-14b` |
| `MODEL_2_SOURCE` | `Qwen/Qwen2.5-14B-Instruct` |

---

## 9. Segurança

### 9.1 Portas e perímetro de rede

| Porta | Componente | Acessível externamente? | Regra |
|-------|-----------|------------------------|-------|
| `4000` | LiteLLM (API) | ✅ **Sim** — única porta pública | Única entrada para clientes |
| `8000` | Ray Serve (ingress) | ❌ **Não** — rede interna Compose | Nunca mapear em `docker-compose ports` |
| `8265` | Ray Dashboard | ❌ **Não** — acesso via `docker compose exec` ou túnel SSH | Dashboard bound a `127.0.0.1` |
| `10001` | Ray Client | ❌ **Não** — rede interna | Nunca expor |

### 9.2 Imagens de contêiner

| Imagem | Fonte | Tag | Política |
|--------|-------|-----|----------|
| Ray Serve LLM | `rayproject/ray` | `2.56.0-py311-gpu` | **Pinada + SHA256** — nunca `:latest` |
| LiteLLM | `docker.litellm.ai/berriai/litellm` | `v1.85.0` | **Pinada** — nunca `:latest` |
| Prometheus | `prom/prometheus` | Semver tag específica | **Pinada** |
| Grafana | `grafana/grafana` | Semver tag específica | **Pinada** |

### 9.3 Fronteiras de confiança

O sistema tem **duas fronteiras de confiança** bem definidas:

1. **Master key (admin)**: usada para gerir chaves virtuais, acessar endpoints
   administrativos. Nunca distribuída a clientes finais.
2. **Chaves virtuais (clientes)**: emitidas pelo LiteLLM via `/key/generate`,
   escopadas a budgets e rate limits. Nem a master key nem qualquer credencial
   interna é derivável de uma chave virtual.

### 9.4 ShadowRay e CVE-2026-27482

O Ray Dashboard e Jobs API foram **projetados sem autenticação**, assumindo
execução em rede já confiável. Isso é um risco documentado e ativamente
explorado (campanhas ShadowRay desde 2023, ShadowRay 2.0 em 2026).

**Mitigações obrigatórias (aplicadas na arquitetura):**

1. A porta 8265 (dashboard) nunca é mapeada no `docker-compose.yml`.
2. O dashboard é bound a `127.0.0.1` via `--dashboard-host=127.0.0.1`.
3. Ray ≥ 2.54.0 é obrigatório (fecha CVE-2026-27482).
4. O ingress do Ray Serve (8000) nunca é exposto — só o LiteLLM (4000) é
   acessível.
5. Qualquer path de rede para o Ray cluster é equivalente a root em todos os
   nós — tratá-lo como um banco de dados sem autorização de consulta.

Para a especificação completa de segurança, consulte
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#9-security--operational-hardening).

---

## 10. Targets de Deploy

O IDIA Server suporta um modelo de deploy simples:

| Target | Elasticidade GPU | Documentação |
|--------|-----------------|-------------|
| **Local (Docker Compose)** | O autoscaler de réplicas do Ray Serve ajusta automaticamente ao número de GPUs disponíveis na máquina | [ARCHITECTURE.md §6](docs/ARCHITECTURE.md#6-local-deployment) |
| **Instância Cloud** | Mesmo stack Docker Compose — para mais potência, troque o tipo da instância | [ARCHITECTURE.md §7](docs/ARCHITECTURE.md#7-ec2-deployment-optional) |

O cliente sempre endereça a porta `:4000` — o host por trás dela é invisível para o cliente.

---

## 11. Monitoramento

O monitoramento foi implementado na **Fase 4** com Prometheus + Grafana + DCGM Exporter:

| Camada | Métricas | Scrape endpoint |
|--------|----------|----------------|
| **vLLM** | TTFT, e2e latência, cache usage, preemptions | `ray-head:8080` |
| **Ray Serve** | Réplicas, fila, autoscaling | `ray-head:8080` |
| **LiteLLM** | Custo/chave, requisições, latência | `litellm:4000` |
| **GPU (DCGM)** | Utilização, VRAM usada/livre, temperatura, power draw | `dcgm-exporter:9400` |

**Acessar Grafana:** `http://localhost:3000` (admin / `$GRAFANA_ADMIN_PASSWORD`)

O datasource Prometheus e o dashboard oficial do vLLM são provisionados
automaticamente. DCGM exporter ativa apenas em hosts Linux com drivers NVIDIA
(`docker compose --profile gpu up` — chamado automaticamente pelo `./idia`).

---

## 12. Gerenciamento de Usuários

Há dois caminhos. Para dar acesso a uma pessoa, use o primeiro.

### Provisionar um colega (recomendado)

Um comando cria a conta inteira — chave virtual no LiteLLM, conta no Open
WebUI, vínculo entre as duas e permissão de modelo. A pessoa recebe um e-mail
e uma senha e já consegue conversar.

```bash
./idia colleague create ana@idia.org "Ana Costa" --tier regular
./idia colleague status ana@idia.org      # chave, modelos concedidos, gasto
./idia colleague revoke ana@idia.org      # remove os cinco artefatos
./idia colleague tiers                    # definições e modelos concedidos
```

| Tier | Budget | RPM | TPM | Perfil típico |
|------|--------|-----|-----|--------------|
| `light` | $0,50/dia | 10 | 5.000 | Visitante, estagiário |
| `regular` | $2/dia | 60 | 30.000 | Pesquisador, uso diário |
| `heavy` | $10/dia | — | — | Pesquisador sênior |
| `classroom` | $20/dia | 300 | 200.000 | Sala de aula, 30+ alunos |

Os modelos concedidos são os que o servidor serve de fato; `--models`
restringe a um subconjunto. `--dry-run` mostra o plano sem criar nada.

Requer `IDIA_PUBLIC_HOST` e `OWUI_DISCOVERY_KEY` no `.env` — veja o
`.env.example`. Detalhes em [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
§5.8 e nos ADR-009 e ADR-010.

### Apenas uma chave de API

Para um script ou serviço que não precisa de conta web:

```bash
./idia colleague key bot@idia.org --tier light
./idia user list                          # chaves ativas
```

> **Atenção:** `./idia user create` é um caminho legado que emite chaves
> **sem rate limit e sem budget** — os tiers que ele anuncia não são
> aplicados. Ver [issue #6](../../issues/6). Use `./idia colleague` até
> que aquela issue seja resolvida.

---

## 13. Multi-Model

O IDIA Server suporta N modelos simultâneos. O Ray Serve carrega cada modelo
sob demanda (scale-to-zero), e o LiteLLM roteia por nome.

### Configurar no .env

```bash
# Trocar de single-model para multi-model:
MODELS_COUNT=2
MODEL_1_ID=mistral-7b
MODEL_1_SOURCE=mistralai/Mistral-7B-Instruct-v0.3
MODEL_2_ID=qwen-2.5-14b
MODEL_2_SOURCE=Qwen/Qwen2.5-14B-Instruct

# Opcional — GPU_COUNT deve cobrir todos os modelos simultâneos:
GPU_COUNT=2       # um modelo por GPU
GPU_VRAM_GB=24.0  # VRAM por GPU
```

### Redesployar

```bash
./idia stop && ./idia deploy local
```

### Limitações multi-model

- Cada modelo ocupa uma GPU inteira (sem GPU sharing por default)
- O `render_config.py` valida que `MODELS_COUNT × GPU_UTILIZATION ≤ GPU_COUNT`
  e rejeita configurações que estouram a VRAM antes de subir qualquer container
- Modelos em `min_replicas: 0` ficam dormentes e não ocupam VRAM até o primeiro request

---

---

## 14. Mantenedor

**Anaximandro Souza**

| | |
|---|---|
| **GitHub** | [@anaxsouza](https://github.com/anaxsouza) |
| **E-mail** | anaximandrosouza@icloud.com |
| **Instituição** | PUC-Behring Institute for AI |
| **Papel** | Arquiteto e mantenedor principal do IDIA Server |
| **Responsabilidades** | Definição de arquitetura, implementação, revisão de código, documentação, deploy e operação |

### Diretrizes para contribuições

1. Toda contribuição deve passar por revisão do mantenedor antes de merge.
2. Mudanças na arquitetura devem ser refletidas no `ARCHITECTURE.md` no mesmo
   PR/commit (regra do Contrato de Evolução de Documentos — veja
   [`ARCHITECTURE.md §16`](docs/ARCHITECTURE.md#16-document-evolution-contract)).
3. Commits seguem [Conventional Commits](https://www.conventionalcommits.org/)
   com referência `[phase-N]` quando sob plano ativo.
4. A suíte de testes deve passar limpa antes de qualquer merge.
5. Dúvidas sobre a arquitetura devem ser direcionadas ao mantenedor ou
   documentadas em issues no repositório.

---

## 15. Licença

```
Copyright 2026 PUC-Behring Institute for AI

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
```

O IDIA Server é distribuído sob **Apache License 2.0** — permissiva, com
proteção de patentes, compatível com TensorFlow/PyTorch e adequada para
instituições de pesquisa brasileiras.

---

## 16. Referências

### Documentação interna

| Documento | Descrição |
|-----------|-----------|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Especificação arquitetural completa (~1157 linhas) incluindo componentes, build, deploy, segurança, monitoramento, custos e troubleshooting |
| [`docs/ADR.md`](docs/ADR.md) | Architecture Decision Records — decisões arquiteturais documentadas por fase |
| [`AGENTS.md`](AGENTS.md) | Regras do projeto para agentes OpenCode — governança, evolução de documentos, testes |

### Documentação externa

| Recurso | URL |
|---------|-----|
| vLLM — Docker deployment | https://docs.vllm.ai/en/stable/deployment/docker/ |
| vLLM — Métricas Prometheus | https://docs.vllm.ai/en/stable/design/metrics/ |
| vLLM — Paralelismo e scaling | https://docs.vllm.ai/en/latest/serving/parallelism_scaling/ |
| Ray Serve LLM — Arquitetura | https://docs.ray.io/en/latest/serve/llm/architecture/overview.html |
| Ray Serve LLM — Serving guide | https://docs.ray.io/en/latest/serve/llm/index.html |
| Ray — KubeRay LLM example | https://docs.ray.io/en/latest/cluster/kubernetes/examples/rayserve-llm-example.html |
| Ray — Security guide | https://docs.ray.io/en/latest/ray-security/index.html |
| ShadowRay / CVE-2023-48022 | https://www.oligo.security/blog/shadowray-attack-ai-workloads-actively-exploited-in-the-wild |
| ShadowRay 2.0 (2026) | https://www.penligent.ai/hackinglabs/the-zombie-vulnerability-a-2026-autopsy-of-cve-2023-48022-and-the-shadowray-2-0-resurgence/ |
| CVE-2026-27482 | https://www.sentinelone.com/vulnerability-database/cve-2026-27482/ |
| LiteLLM — Docker quickstart | https://docs.litellm.ai/docs/proxy/docker_quick_start |
| LiteLLM — Load balancing | https://docs.litellm.ai/docs/proxy/load_balancing |
| LiteLLM — Health check routing | https://docs.litellm.ai/docs/proxy/health_check_routing |
| Fine-tuning comparison (2026) | https://dev.to/ultraduneai/eval-003-fine-tuning-in-2026-axolotl-vs-unsloth-vs-trl-vs-llama-factory-2ohg |
| CNCF Survey 2025 | https://www.cncf.io/announcements/2026/01/20/kubernetes-established-as-the-de-facto-operating-system-for-ai-as-production-use-hits-82-in-2025-cncf-annual-cloud-native-survey/ |

---

*README version: 2.3 | Last updated: 2026-07-28 | Maintainer: @anaxsouza*
