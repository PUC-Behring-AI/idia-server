# IDIA Server

**Servidor de inferência LLM do PUC-Behring Institute for AI.**

[![Stack](https://img.shields.io/badge/stack-Ray%20Serve%20%7C%20vLLM%20%7C%20LiteLLM-orange)]()
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://python.org)
[![License](https://img.shields.io/badge/license-Apache%202.0-green)](LICENSE)

---

## O que ele resolve

Um colega do instituto recebe um e-mail e uma senha, abre uma página web,
escolhe um modelo e conversa. Você sabe, por pessoa, quanto ela gastou e
quantas requisições fez — e nada disso sai da GPU do instituto.

Para quem escreve código, o mesmo servidor é um endpoint compatível com a API
da OpenAI. O SDK oficial aponta para a porta 4000 e funciona sem uma linha de
código específica, o que vale igualmente para Continue.dev, Aider e Cursor.

Modelo ocioso libera a GPU; a primeira requisição depois disso o recarrega
sozinha. Quem paga a conta escolhe entre custo de GPU parada e latência do
primeiro pedido, e a escolha é por modelo.

## Começando

```bash
cp .env.example .env        # preencha HF_TOKEN, LITELLM_MASTER_KEY,
                            # POSTGRES_PASSWORD, UI_PASSWORD
./idia deploy local         # primeira vez: 5–15 min baixando os pesos
./idia status               # serviços, modelos carregados, GPU
```

Dar acesso a alguém:

```bash
./idia colleague create ana@idia.org "Ana Costa" --tier regular
```

Um comando cria a chave virtual, a conta na interface web, o vínculo entre as
duas e a permissão de modelo. A pessoa recebe uma credencial só.

| Tier | Orçamento | RPM | Perfil |
|------|-----------|-----|--------|
| `light` | $0,50/dia | 10 | Visitante, estagiário |
| `regular` | $2/dia | 60 | Pesquisador |
| `heavy` | $10/dia | — | Pesquisador sênior |
| `classroom` | $20/dia | 300 | Sala de aula, 30+ alunos |

## Consumindo

```python
from openai import OpenAI

client = OpenAI(base_url="http://<host>:4000/v1", api_key="sk-...")
resposta = client.chat.completions.create(
    model="mistral-7b",
    messages=[{"role": "user", "content": "Explique PagedAttention em uma frase."}],
)
```

A primeira requisição depois de um período ocioso paga o cold start dentro da
própria chamada. Não existe API separada de "acordar".

## Como funciona, em três camadas

```
Cliente  ──HTTPS──▶  LiteLLM :4000  ──▶  Ray Serve :8000  ──▶  vLLM
                     auth, budget,       autoscaling,          pesos na VRAM,
                     rate-limit,         placement na GPU,     KV cache,
                     spend tracking      roteamento            geração
```

Duas portas são alcançáveis pela rede: **4000** (API) e **3001** (interface
web). Todo o resto — ingress do Ray, dashboard, PostgreSQL, Prometheus — fica
na rede interna do Compose. O dashboard do Ray executa código arbitrário para
quem conseguir alcançá-lo, e é por isso que ele nunca é publicado.

## Onde está o resto

| Documento | Para quê |
|---|---|
| [`docs/DEPLOY.md`](docs/DEPLOY.md) | **Operar**: subir, configurar, criar usuários, monitorar, diagnosticar |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | **Entender**: por que dois autoscalers, como o custo se comporta, qual o modelo de segurança |
| [`docs/ADR.md`](docs/ADR.md) | **Por que assim**: as decisões registradas, com as alternativas descartadas |
| [`AGENTS.md`](AGENTS.md) | Regras do projeto para agentes e contribuintes |

## Estado atual

Um modelo servido por vez, escolhido no `.env`; o modo multi-model existe e
está documentado em `ARCHITECTURE.md` §5.3. Deploy local via Docker Compose,
com serviço systemd opcional para subir no boot. O caminho AWS foi removido —
o histórico está no ADR-003 e no ADR-004, ambos marcados como superseded.

A suíte roda sem GPU e sem Docker:

```bash
pip install pytest pyyaml
./scripts/gate.sh          # o mesmo portão exigido antes de abrir um PR
```

## Contribuindo

Mudança de arquitetura acompanha o `ARCHITECTURE.md` no mesmo PR — é o
Contrato de Evolução de Documentos, §16. Decisão com trade-off entre
alternativas viáveis vira um ADR. Commits em
[Conventional Commits](https://www.conventionalcommits.org/). O
`./scripts/gate.sh` precisa passar antes do PR: ele roda a suíte, o
`shellcheck`, o `ruff` e valida os YAML.

Achou um problema fora do escopo do que está fazendo? Abra uma issue com
`file:line` e a cena concreta, em vez de um `TODO` no código.

## Mantenedor

**Anaximandro Souza** — [@anaxsouza](https://github.com/anaxsouza),
PUC-Behring Institute for AI.

## Licença

Apache License 2.0 — permissiva, com proteção explícita de patentes. O
raciocínio está no ADR-008; o texto completo em [`LICENSE`](LICENSE).
