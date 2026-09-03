# IDIA Server — Guia de Operações

> **Documento de referência para o mantenedor.** Cobre todos os cenários de
> deploy, desde a instalação de pré-requisitos até a configuração de múltiplos
> modelos, gestão de usuários e monitoramento.
>
> Para uma visão arquitetural do sistema, consulte `docs/ARCHITECTURE.md`.
> Para regras de governança e agentes OpenCode, consulte `AGENTS.md`.

---

## Índice

1. [Visão geral do fluxo](#1-visão-geral-do-fluxo)
2. [Pré-requisitos](#2-pré-requisitos)
3. [Deploy local — servidor no instituto](#3-deploy-local--servidor-no-instituto)
   - 3.1 Clonar o repositório
   - 3.2 Configurar variáveis de ambiente
   - 3.3 Deploy (um único comando)
   - 3.4 Validar sem subir (dry-run)
   - 3.5 Verificar saúde dos serviços
   - 3.6 Enviar a primeira requisição
   - 3.7 Inicialização automática no boot
4. [Configuração multi-model](#4-configuração-multi-model)
5. [Gestão de usuários](#5-gestão-de-usuários)
6. [Monitoramento](#6-monitoramento)
7. [Integração com clientes](#7-integração-com-clientes)
8. [Manutenção](#8-manutenção)
9. [Referência de variáveis de ambiente](#9-referência-de-variáveis-de-ambiente)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Visão geral do fluxo

```
Mantenedor edita .env
        │
        ▼
./idia deploy local
        │
        ├─ [1/5] render_config.py --render-all
        │         ├─ rendered_serve_config.yaml   → Ray Serve
        │         └─ rendered_litellm_config.yaml → LiteLLM
        │
        ├─ [2/5] docker compose pull (imagens)
        ├─ [3/5] docker compose up -d --build
        ├─ [4/5] wait loop: GET /health (10 min timeout)
        └─ [5/5] smoke_test.sh --wait
                        │
                        ▼
              http://localhost:4000  ✓
```

**Por que `./idia` e não `docker compose up` diretamente?**

O LiteLLM não faz substituição de variáveis de ambiente (`${VAR}`) no seu
arquivo de configuração. Se você rodar `docker compose up` sem o passo de
pré-renderização, os modelos terão o nome literal `"${MODEL_ID}"` e
**100% das requisições falharão** com `model not found`. O `./idia deploy
local` garante que os arquivos renderizados existam antes de subir os
containers.

---

## 2. Pré-requisitos

### 2.1 Hardware

| Cenário | GPU mínima | VRAM mínima | RAM | Disco |
|---------|-----------|-------------|-----|-------|
| Modelo 7-8B (Llama 3.1 8B, Mistral 7B) | 1× NVIDIA GPU | 20 GB | 32 GB | 100 GB |
| Modelo 13-14B (Qwen 2.5 14B) | 1× A100 / 2× A10G | 28 GB | 64 GB | 150 GB |
| Modelo 30B+ | 2-4× A100 / 4× A10G | 60+ GB | 128 GB | 300 GB |
| Desenvolvimento sem GPU (CPU-only) | — | — | 16 GB | 50 GB |

> **Nota:** Modelos rodando em CPU são 50-100× mais lentos. Útil apenas
> para testar o pipeline de configuração, não para uso em produção.

### 2.2 Software — Linux (Ubuntu 22.04+)

```bash
# 1. Docker Engine + Compose v2
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER" && newgrp docker
docker compose version  # deve mostrar v2.x

# 2. NVIDIA Container Toolkit (para GPU passthrough)
distribution=$(. /etc/os-release && echo "$ID$VERSION_ID")
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
    sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L "https://nvidia.github.io/libnvidia-container/$distribution/libnvidia-container.list" | \
    sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
    sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# Verificar: deve listar sua GPU
docker run --rm --gpus all nvidia/cuda:12.1.0-base-ubuntu22.04 nvidia-smi

# 3. Python 3.11+
sudo apt-get install -y python3.11 python3-pip
python3 --version  # 3.11.x

# 4. curl e jq
sudo apt-get install -y curl jq
```

### 2.3 Software — macOS (Apple Silicon / Intel)

```bash
# 1. Docker Desktop (com suporte a Compose v2)
# Baixar em: https://www.docker.com/products/docker-desktop/
# Habilitar: Docker Desktop → Settings → Features in development → Enable VirtioFS

# 2. Python 3.11+
brew install python@3.11
python3 --version  # 3.11.x

# 3. curl e jq
brew install curl jq

# Nota: macOS não suporta GPU passthrough para containers.
# O servidor pode ser iniciado sem GPU para testar configuração,
# mas não para inferência em produção.
```

### 2.4 Token HuggingFace

Muitos modelos de LLM são "gated" (exigem aceitação de termos de uso e um
token de acesso). Para obter o token:

1. Criar conta em https://huggingface.co (se não tiver)
2. Ir em https://huggingface.co/settings/tokens
3. Clicar em **New token** → tipo **Read** → copiar o token (`hf_...`)
4. Para modelos Llama (Meta): ir em
   https://huggingface.co/mistralai/Mistral-7B-Instruct-v0.3 e clicar em
   **Request access** (aprovação automática em minutos)

### 2.5 Verificação final

```bash
# Todos esses comandos devem retornar sem erro:
docker compose version     # Docker Compose v2.x
python3 --version          # Python 3.11+
nvidia-smi                 # Mostra GPU(s) disponíveis
curl --version             # qualquer versão
```

---

## 3. Deploy local — servidor no instituto

### 3.1 Clonar o repositório

```bash
git clone https://github.com/PUC-Behring-AI/idia-server.git
cd idia-server
```

### 3.2 Configurar variáveis de ambiente

```bash
cp .env.example .env
```

Abrir `.env` em um editor e preencher:

```bash
# ──────────────────────────────────────────────────────────────
# OBRIGATÓRIOS — o servidor não sobe sem estes
# ──────────────────────────────────────────────────────────────

# Token HuggingFace para baixar pesos do modelo
# Obter em: https://huggingface.co/settings/tokens
HF_TOKEN=hf_aBcDeFgHiJkLmNoPqRsTuVwXyZ

# Chave master do LiteLLM — usada para criar virtual keys de usuários
# Gerar uma chave segura:
#   python3 -c "import secrets; print('sk-idia-' + secrets.token_hex(16))"
LITELLM_MASTER_KEY=sk-idia-a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6

# Nome curto do modelo — é o que os clientes usarão no campo "model"
# Ex: "llama-3.1-8b", "mistral-7b", "qwen-2.5-14b"
MODEL_ID=mistral-7b

# ID completo no HuggingFace Hub — usado para baixar os pesos
# Ex: "mistralai/Mistral-7B-Instruct-v0.3", "mistralai/Mistral-7B-Instruct-v0.3"
MODEL_SOURCE=mistralai/Mistral-7B-Instruct-v0.3

# ──────────────────────────────────────────────────────────────
# OPCIONAIS — os defaults são adequados para a maioria dos casos
# ──────────────────────────────────────────────────────────────

# Comprimento máximo de contexto em tokens (default: 8192)
# Reduzir para economizar VRAM em GPUs menores
MAX_MODEL_LEN=8192

# Fração de VRAM a reservar para pesos + KV cache (default: 0.9)
# Valores: 0.1 a 1.0 — nunca 1.0 (sistema precisa de overhead)
GPU_MEMORY_UTILIZATION=0.9

# Número de GPUs no servidor (default: 1)
# Usado para validação de VRAM em modo multi-model
GPU_COUNT=1

# VRAM por GPU em GB (default: 24.0 — A10G / RTX 3090 / RTX 4090). Ajuste para sua GPU.
# Para outras GPUs: A100 = 80.0, RTX 3090 = 24.0, V100 = 16.0
GPU_VRAM_GB=24.0

# Senha do admin Grafana — troque antes de expor na rede
GRAFANA_ADMIN_PASSWORD=minha-senha-segura
```

> **Segurança:** O arquivo `.env` nunca deve ser commitado. O `.gitignore`
> já o exclui. Confirmar com `git status` — `.env` não deve aparecer.

### 3.3 Deploy (um único comando)

```bash
./idia deploy local
```

**Saída esperada:**

```
══════════════════════════════════════
  IDIA Server — Local Deploy
══════════════════════════════════════

[1/5] Rendering configs (serve_config + litellm_config)...
[✓] rendered_serve_config.yaml
[✓] rendered_litellm_config.yaml
[2/5] Pulling Docker images (skipping build)...
[3/5] Starting services...
[+] Running 5/5
 ✔ Container idia-server-ray-head-1    Started
 ✔ Container idia-server-litellm-1     Started
 ✔ Container idia-server-prometheus-1  Started
 ✔ Container idia-server-grafana-1     Started
[✓] Services started
[4/5] Waiting for server to be ready...
       URL: http://localhost:4000/health
       Timeout: 600s
       ⚠ Note: First boot downloads model weights — this may take 5-15 min

       . 0s elapsed
       . 10s elapsed
       . 20s elapsed
       ...
       . 480s elapsed      ← download + carregamento dos pesos
[✓] Server is ready (490s elapsed)
[5/5] Running smoke test...
[✓] Smoke test passed

══════════════════════════════════════
  IDIA Server — Server Running
══════════════════════════════════════

  API endpoint:  http://localhost:4000
  Grafana:       http://localhost:3000  (admin / $GRAFANA_ADMIN_PASSWORD)

Next steps:
  ./idia user create alice hard       # Create a user (researcher tier)
  ./idia user create bob  regular     # Create a user (grad student tier)
  ./idia status                       # Check all services
  ./idia logs                         # View logs
```

> **Primeiro deploy:** O download dos pesos do Llama 3.1 8B leva de 5 a 15
> minutos dependendo da velocidade da conexão (~16 GB). Deploys subsequentes
> iniciam em 2-3 minutos porque os pesos ficam no volume Docker `idia_hf_cache`.

### 3.4 Validar sem subir (dry-run)

Para verificar se a configuração está correta sem iniciar containers:

```bash
./idia deploy local --dry-run
```

Este comando renderiza os dois arquivos de configuração e os imprime. Útil
para verificar se `MODEL_ID`, `MODEL_SOURCE` e variáveis opcionais estão
sendo aplicados corretamente.

**Inspecionar os rendered configs:**

```bash
# Configuração do Ray Serve (serve_config renderizado)
cat rendered_serve_config.yaml

# Configuração do LiteLLM (gerado dinamicamente por render_config.py)
cat rendered_litellm_config.yaml
```

O `rendered_litellm_config.yaml` deve ter o `model_name` e o `model`
com o valor real de `MODEL_ID` (não `${MODEL_ID}`):

```yaml
model_list:
- litellm_params:
    api_base: http://ray-head:8000/v1
    api_key: no-auth-internal
    model: openai/mistral-7b       # ← valor real, não placeholder
  model_name: mistral-7b           # ← valor real
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  max_parallel_requests: 20
```

### 3.5 Verificar saúde dos serviços

```bash
./idia status
```

**Saída esperada (servidor saudável):**

```
══════════════════════════════════════
  IDIA Server — Status
══════════════════════════════════════

Services:
NAME                           STATUS          PORTS
idia-server-ray-head-1         Up (healthy)
idia-server-litellm-1          Up (healthy)    0.0.0.0:4000->4000/tcp
idia-server-prometheus-1       Up
idia-server-grafana-1          Up              127.0.0.1:3000->3000/tcp

LiteLLM health:
[✓] LiteLLM is healthy

Loaded models:
  • mistral-7b

GPU status:
  GPU: NVIDIA A10G | VRAM: 14352 MiB / 24576 MiB | Util: 0 %
```

### 3.6 Enviar a primeira requisição

```bash
# Testar diretamente com curl (substitua SK pela sua chave master ou virtual)
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer sk-idia-a1b2c3d4..." \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mistral-7b",
    "messages": [
      {"role": "user", "content": "Em uma frase, o que é inteligência artificial?"}
    ],
    "temperature": 0.7,
    "max_tokens": 200
  }'
```

**Resposta esperada:**

```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "model": "mistral-7b",
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": "Inteligência artificial é o campo da ciência da computação..."
    },
    "finish_reason": "stop"
  }],
  "usage": {
    "prompt_tokens": 22,
    "completion_tokens": 45,
    "total_tokens": 67
  }
}
```

---

### 3.7 Inicialização automática no boot

Para garantir que o servidor suba automaticamente quando a máquina ligar,
instale o serviço systemd:

```bash
sudo ./idia service install
```

**O que isso faz:**
- Cria uma systemd unit em `/etc/systemd/system/idia-server.service`
- Configura o serviço para iniciar após `docker.service` e `network-online.target`
- Habilita o serviço para iniciar automaticamente no boot
- Inicia o servidor imediatamente (equivale a `./idia deploy local --no-wait`)

**Fluxo no boot:**
1. Sistema liga → systemd inicia o Docker daemon
2. `idia-server.service` executa `./idia deploy local --no-wait`
3. Configs são renderizados, containers sobem com `restart: unless-stopped`
4. LiteLLM fica disponível em `:4000` assim que o modelo carregar
   (~1-2 min em boots subsequentes com cache; ~15 min no primeiro boot)

**Verificar status:**
```bash
./idia service status              # status do serviço (systemd ou compose)
systemctl status idia-server       # via systemd diretamente
journalctl -u idia-server -f       # logs do serviço
./idia status                      # saúde dos containers
```

**Desinstalar:**
```bash
sudo ./idia service uninstall
```

> ⚠️ **Persistência de virtual keys:** As chaves de usuário do LiteLLM
> são armazenadas em memória e são **perdidas em todo restart** (boot,
> crash, `docker compose down`). Após cada reboot:
> 1. Recrie as chaves com `./idia user create <nome> <tier>` ou
> 2. Restaure de backup (veja §5.4 "Backup das chaves de usuários").
>
> Este é um problema conhecido da versão open-source do LiteLLM.
> LiteLLM Pro oferece persistência via banco de dados externo.

---

## 4. Configuração multi-model

O IDIA Server suporta N modelos simultaneamente. Cada modelo roda como um
deployment independente no Ray Serve, e o LiteLLM roteia para o correto
baseado no campo `model` da requisição.

### 4.1 Requisitos de VRAM

Antes de configurar múltiplos modelos, calcule se a VRAM disponível é suficiente:

```
VRAM necessária = MODELS_COUNT × GPU_MEMORY_UTILIZATION × tamanho_estimado
```

Tamanhos estimados (FP16, sem quantização):

| Modelo | Parâmetros | VRAM mínima |
|--------|-----------|------------|
| Llama 3.1 / Mistral 7B | 7-8B | ~16 GB |
| Qwen 2.5 14B | 14B | ~28 GB |
| Llama 3.1 70B | 70B | ~140 GB |
| Llama 3.1 405B | 405B | ~800 GB |

**Exemplo:** 2 modelos de 8B com `GPU_MEMORY_UTILIZATION=0.9`:
- Necessário: 2 × 0.9 × 16 GB = 28.8 GB
- Viável em: 2× A10G (48 GB total), 1× A100 (80 GB)
- Inviável em: 1× A10G (24 GB) — o `render_config.py` bloqueia o deploy

### 4.2 Editar `.env`

```bash
# Comentar ou remover as variáveis de single-model:
# MODEL_ID=mistral-7b
# MODEL_SOURCE=mistralai/Mistral-7B-Instruct-v0.3

# Habilitar modo multi-model:
MODELS_COUNT=2

MODEL_1_ID=mistral-7b
MODEL_1_SOURCE=mistralai/Mistral-7B-Instruct-v0.3

MODEL_2_ID=qwen-2.5-7b
MODEL_2_SOURCE=Qwen/Qwen2.5-7B-Instruct

# Ajustar recursos:
GPU_COUNT=2                    # GPUs disponíveis no servidor
GPU_VRAM_GB=24.0               # VRAM de cada GPU
GPU_MEMORY_UTILIZATION=0.85    # Ligeiramente menor para acomodar overhead
```

### 4.3 Re-deploy

```bash
./idia stop
./idia deploy local
```

O `render_config.py` valida automaticamente o orçamento de VRAM antes de
gerar os configs. Se os modelos não couberem, o deploy falha com diagnóstico:

```
FATAL: VRAM budget exceeded.
  Models requested : 2
  Est. VRAM/model  : 16.00 GB (utilization=0.85)
  Total required   : 27.20 GB
  Available (GPUs) : 24.00 GB (1 GPU × 24.00 GB)
  Fix: Reduce MODELS_COUNT, lower GPU_MEMORY_UTILIZATION, or add more GPUs.
```

### 4.4 Verificar os dois modelos

```bash
./idia status
# Deve mostrar:
#   Loaded models:
#     • mistral-7b
#     • qwen-2.5-7b

# Testar ambos:
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -d '{"model": "mistral-7b", "messages": [{"role":"user","content":"ping"}]}'

curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -d '{"model": "qwen-2.5-7b", "messages": [{"role":"user","content":"ping"}]}'
```

---

## 5. Gestão de usuários

O IDIA Server usa o sistema de virtual keys do LiteLLM. Cada usuário recebe
uma chave única com limites de uso definidos pelo tier.

### 5.1 Tiers disponíveis

| Tier | RPM | TPM | Indicado para |
|------|-----|-----|---------------|
| `hard` | 15 | 50 000 | Pesquisadores, usuários intensivos |
| `regular` | 4 | 15 000 | Mestrandos, estudantes de pós-graduação |
| `light` | 1 | 5 000 | Graduandos, uso ocasional |

### 5.2 Criar usuário

```bash
./idia user create <nome> <tier>
```

Exemplos:

```bash
./idia user create alice hard
# {
#   "key": "sk-idia-user-a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6",
#   "key_alias": "alice",
#   "team_id": "hard",
#   "models": ["mistral-7b"],
#   "expires": null
# }

./idia user create carlos regular
./idia user create diana light
```

> **Importante:** A chave é gerada uma única vez e exibida apenas no momento
> da criação. Não há como recuperá-la depois. Armazene em local seguro e
> envie ao usuário por canal seguro (e-mail institucional criptografado ou
> similar).

### 5.3 Listar usuários

```bash
./idia user list
# Active virtual keys:
#   alice (hard) — expires: never
#   carlos (regular) — expires: never
#   diana (light) — expires: never
```

### 5.4 Revogar acesso

LiteLLM permite revogar chaves via API:

```bash
# Revogar chave de um usuário:
curl -X POST http://localhost:4000/key/delete \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"keys": ["sk-idia-user-a1b2c3d4..."]}'
```

### 5.5 Criar chave com expiração

Para acesso temporário (ex: alunos de um semestre):

```bash
# Calcular data de expiração (ex: fim do semestre, 6 meses):
EXPIRES=$(python3 -c "
from datetime import datetime, timedelta
exp = datetime.utcnow() + timedelta(days=180)
print(exp.strftime('%Y-%m-%dT%H:%M:%S.000Z'))
")

# A API LiteLLM aceita `expires` diretamente:
curl -X POST http://localhost:4000/key/generate \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"key_alias\": \"estudante-turma-2026\",
    \"team_id\": \"light\",
    \"expires\": \"$EXPIRES\",
    \"models\": [\"mistral-7b\"]
  }"
```

---

## 6. Monitoramento

### 6.1 Grafana (dashboards)

Acessar: **http://localhost:3000** (local) ou via túnel SSH (remoto)

Credenciais: `admin` / `$GRAFANA_ADMIN_PASSWORD`

O dashboard **vLLM Metrics** (provisionado automaticamente) exibe:

| Painel | Métrica | Alerta sugerido |
|--------|---------|-----------------|
| Request Throughput | req/s | < 0.1 req/s por mais de 10 min durante horário de pico |
| Time to First Token | ms P95 | > 5 000 ms |
| Inter-token Latency | ms P95 | > 500 ms |
| GPU KV Cache Hit Rate | % | < 20% (indica que o contexto está grande demais) |
| GPU Memory Usage | % VRAM | > 95% (risco de OOM) |
| Running Requests | contagem | > 50 (possível gargalo de throughput) |

### 6.2 Métricas via CLI

```bash
# Ver todas as métricas Ray Serve expostas:
docker compose exec ray-head curl -s http://localhost:8080/metrics | grep vllm

# Métricas chave:
# vllm:num_requests_running     — requisições em execução no vLLM
# vllm:gpu_cache_usage_perc     — uso do KV cache
# vllm:time_to_first_token_ms   — latência da primeira resposta
```

### 6.3 Logs por serviço

```bash
./idia logs               # todos os serviços (Ctrl+C para sair)
./idia logs ray-head      # Ray Serve + vLLM (inferência)
./idia logs litellm       # LiteLLM (gateway, auth, routing)
./idia logs prometheus    # Prometheus (scraping)
./idia logs grafana       # Grafana (dashboards)
```

### 6.4 Métricas de uso LiteLLM

```bash
# Resumo de uso por chave (últimas 24h):
curl -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
    http://localhost:4000/spend/logs?limit=100 | jq .
```

---

## 7. Integração com clientes

O endpoint é compatível com a API OpenAI. Qualquer cliente que suporte
`base_url` personalizado funciona diretamente.

### 7.1 Python — SDK OpenAI

```python
from openai import OpenAI

# Substituir pelo endpoint real e chave do usuário:
client = OpenAI(
    base_url="http://localhost:4000/v1",      # local
    # base_url="http://<remote-host>:4000/v1",   # acesso remoto
    api_key="sk-idia-user-a1b2c3d4..."
)

# Chat completion:
response = client.chat.completions.create(
    model="mistral-7b",
    messages=[
        {"role": "system", "content": "Você é um assistente de pesquisa especializado em biologia molecular."},
        {"role": "user", "content": "Explique o mecanismo de CRISPR-Cas9."}
    ],
    temperature=0.7,
    max_tokens=1000,
    stream=False  # True para streaming
)

print(response.choices[0].message.content)
print(f"Tokens usados: {response.usage.total_tokens}")
```

**Streaming:**

```python
stream = client.chat.completions.create(
    model="mistral-7b",
    messages=[{"role": "user", "content": "Escreva um resumo sobre RNA mensageiro."}],
    stream=True
)

for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
print()
```

### 7.2 LangChain

```python
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    base_url="http://localhost:4000/v1",
    api_key="sk-idia-user-...",
    model="mistral-7b",
    temperature=0.7
)

response = llm.invoke("Qual a diferença entre RNA e DNA?")
print(response.content)
```

### 7.3 OpenCode / agentes de IA

Para usar o IDIA Server como provider em OpenCode ou outros agentes,
configurar como provider OpenAI-compatible:

```jsonc
// ~/.config/opencode/opencode.json — adicionar provider:
{
  "providers": {
    "idia": {
      "api_key": "sk-idia-user-...",
      "base_url": "http://localhost:4000/v1",
      "name": "IDIA Server (local)"
    }
  },
  "model": "idia/mistral-7b"
}
```

### 7.4 curl (scripts de automação)

```bash
#!/usr/bin/env bash
# Exemplo de script de automação usando o IDIA Server

IDIA_ENDPOINT="http://localhost:4000"
IDIA_KEY="sk-idia-user-..."
MODEL="mistral-7b"

query_llm() {
    local prompt="$1"
    curl -sf "$IDIA_ENDPOINT/v1/chat/completions" \
        -H "Authorization: Bearer $IDIA_KEY" \
        -H "Content-Type: application/json" \
        -d "{
            \"model\": \"$MODEL\",
            \"messages\": [{\"role\": \"user\", \"content\": \"$prompt\"}],
            \"temperature\": 0.3,
            \"max_tokens\": 500
        }" | jq -r '.choices[0].message.content'
}

# Uso:
result=$(query_llm "Resuma em 2 frases: o que é machine learning?")
echo "$result"
```

---

## 8. Manutenção

### 8.1 Trocar o modelo

```bash
# Editar .env:
MODEL_ID=mistral-7b
MODEL_SOURCE=mistralai/Mistral-7B-Instruct-v0.3

# Re-deploy:
./idia stop && ./idia deploy local

# Verificar: o volume idia_hf_cache é preservado entre deploys.
# Se o novo modelo não estiver em cache, será baixado automaticamente.
```

### 8.2 Atualizar o servidor (nova versão do repositório)

```bash
git pull origin main

# Re-renderizar e reiniciar:
./idia stop
./idia deploy local
```

> **Nota:** Se `Dockerfile.ray` foi atualizado, a imagem será reconstruída
> automaticamente pelo `docker compose up --build`.

### 8.3 Limpar cache de modelos

```bash
# Listar volumes:
docker volume ls | grep idia

# Remover cache HuggingFace (força re-download no próximo boot):
docker volume rm idia_hf_cache

# Remover todos os volumes (dados de métricas também):
./idia stop && docker compose down -v
```

### 8.4 Backup das chaves de usuários

As virtual keys do LiteLLM são armazenadas em memória (por padrão). Em caso
de restart, todas as chaves são perdidas. Para persistência:

```bash
# Exportar chaves antes de parar:
curl -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
    http://localhost:4000/key/info > backup_keys_$(date +%Y%m%d).json

# Após restart, recriar chaves a partir do backup.
# (LiteLLM Pro suporta banco de dados para persistência — ver docs oficiais)
```

### 8.5 Verificar consistência dos configs

```bash
# Rodar a suíte de testes de configuração (não requer GPU, ~5 segundos):
pip install pytest pyyaml
pytest tests/ -m "config or docs or security" -v

# Esperado: todos passam sem infraestrutura
```

---

## 9. Referência de variáveis de ambiente

| Variável | Obrigatória | Default | Descrição |
|----------|------------|---------|-----------|
| `HF_TOKEN` | Sim | — | Token HuggingFace para baixar modelos gated |
| `LITELLM_MASTER_KEY` | Sim | — | Chave admin LiteLLM para criar virtual keys |
| `MODEL_ID` | Sim* | — | Alias do modelo (usado pelos clientes no campo `model`) |
| `MODEL_SOURCE` | Sim* | — | ID do modelo no HuggingFace Hub |
| `MODELS_COUNT` | Não | 0 | Número de modelos em modo multi-model (0 = single) |
| `MODEL_N_ID` | Condicional | — | Alias do N-ésimo modelo (quando `MODELS_COUNT > 0`) |
| `MODEL_N_SOURCE` | Condicional | — | ID HF do N-ésimo modelo (quando `MODELS_COUNT > 0`) |
| `MAX_MODEL_LEN` | Não | 8192 | Comprimento máximo de contexto em tokens |
| `GPU_MEMORY_UTILIZATION` | Não | 0.9 | Fração de VRAM reservada (0.0–1.0) |
| `GPU_COUNT` | Não | 1 | Número de GPUs (usado para validação VRAM) |
| `GPU_VRAM_GB` | Não | 24.0 | VRAM por GPU em GB |
| `GRAFANA_ADMIN_PASSWORD` | Não | — | Senha admin Grafana |
| `RAY_MEMORY_LIMIT` | Não | 16g | Limite de RAM para o container Ray head |
| `RAY_MEMORY_RESERVATION` | Não | 8g | Reserva de RAM para o container Ray head |
| `RAY_SHM_SIZE` | Não | 4gb | Tamanho do shared memory para comunicação Ray |

(*) Obrigatória em modo single-model. Desnecessária quando `MODELS_COUNT > 0`.

---

## 10. Troubleshooting

### "model not found" em todas as requisições

**Causa:** `docker compose up` foi executado diretamente, sem pre-renderizar
os configs. O `rendered_litellm_config.yaml` não existe ou está velho, e o
LiteLLM sobe sem saber rotear para o modelo que o `.env` declara.

**Solução:**
```bash
./idia stop
./idia deploy local   # pré-renderiza antes de subir
```

### Servidor não sobe após reboot

**Causa:** Serviço systemd não instalado ou Docker não habilitado no boot.

```bash
# Verificar se o serviço está enabled:
systemctl is-enabled docker           # Deve retornar "enabled"
systemctl is-enabled idia-server      # Deve retornar "enabled"

# Verificar logs do serviço:
journalctl -u idia-server --since "5 minutes ago"
```

**Solução:** Se `idia-server` não estiver enabled:
```bash
sudo ./idia service install
```

Se `docker` não estiver enabled:
```bash
sudo systemctl enable --now docker
```

### Timeout no step 4/5 (wait loop)

**Causa A:** Primeiro deploy com modelo grande — download normal.
```bash
# Verificar progresso do download:
./idia logs ray-head | grep -E "Downloading|Loading|model"
```

**Causa B:** `HF_TOKEN` inválido.

> 💡 O modelo padrão (`mistralai/Mistral-7B-Instruct-v0.3`) é **não-gated** —
> não precisa aceitar termos. Se você estiver usando um modelo gated
> (ex: `meta-llama/Llama-3.1-8B-Instruct`), aceite os termos no site do
> HuggingFace e gere um novo token.

```bash
# Testar token diretamente:
curl -H "Authorization: Bearer $HF_TOKEN" \
    "https://huggingface.co/api/models/mistralai/Mistral-7B-Instruct-v0.3"
# Se retornar 401, o token está inválido.
```

**Causa C:** VRAM insuficiente — vLLM falha com OOM e Ray entra em crashloop.
```bash
# Verificar se há OOM nos logs:
./idia logs ray-head | grep -iE "out of memory|CUDA error|OOM"
# Se sim: reduzir GPU_MEMORY_UTILIZATION ou usar modelo menor
```

### "FATAL: VRAM budget exceeded"

**Causa:** Configuração multi-model com modelos que não cabem nas GPUs disponíveis.

**Solução:** Ajustar em `.env`:
- Reduzir `MODELS_COUNT`
- Reduzir `GPU_MEMORY_UTILIZATION` (ex: 0.9 → 0.7)
- Usar modelos menores
- Aumentar `GPU_COUNT` se houver mais GPUs

### 401 Unauthorized

**Causa:** Chave inválida, expirada, ou ausente no header.
```bash
# Verificar chave:
curl -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
    http://localhost:4000/v1/models
# Se retornar 200, a master key funciona.

# Verificar se a virtual key existe:
./idia user list
```

### 429 Too Many Requests

**Causa:** Rate limit do tier excedido. Aguardar 60 segundos ou usar tier superior.

### Grafana não abre (localhost:3000 recusado)

**Causa:** No deploy local, Grafana está `Up` mas ainda inicializando.
```bash
docker compose ps grafana        # Checar se está "Up"
./idia logs grafana | tail -20   # Ver se há erro de startup
```

**Causa:** Grafana não é exposto externamente — usar túnel SSH:
```bash
ssh -L 3000:127.0.0.1:3000 user@<host-remoto>
```

### Modelo não aparece no `./idia status` (Loaded models vazio)

**Causa:** Ray Serve em `min_replicas: 0` — nenhuma réplica ativa até a
primeira requisição.

**Solução:** Enviar uma requisição para "acordar" o modelo:
```bash
curl -sf http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -d '{"model":"mistral-7b","messages":[{"role":"user","content":"ping"}]}'
# Primeira resposta pode demorar 30-90s (cold start do Ray replica)
```

---

*Document version: 1.1 | Last updated: 2026-07-28 | Maintainer: @anaxsouza*
