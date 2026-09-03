# AGENTS.md — IDIA Server
# Regras do projeto para qualquer agente que trabalhe neste repositório.
# Lido por Claude Code, OpenCode e afins; não depende de nenhum deles.
# Version: 2.0
# Last updated: 2026-09-03

## Project

**Name:** IDIA Server (Inference & Deployment for Intelligent Agents)
**Description:** Servidor de inferência LLM auto-hospedado, com autoscaling de
  réplicas (incluindo scale-to-zero), provisionamento de usuários com budget e
  rate-limit individuais, e interface web. Deploy local via Docker Compose.
**Stack:** Python 3.11+, Docker Compose v2, Ray Serve LLM (2.56.0),
  vLLM (0.22.0, bundled by ray[serve,llm]==2.56.0), LiteLLM (1.85.0), Prometheus, Grafana
**Testing:** pytest 8.x, PyYAML (config schema validation)
**Repository:** https://github.com/PUC-Behring-AI/idia-server

## Architecture (3-Tier)

| Tier | Component | Role | Port |
|------|-----------|------|------|
| Gateway | **LiteLLM** | Auth, virtual keys, budgets, rate-limits, spend tracking | `:4000` (única porta externa) |
| Orchestration | **Ray Serve LLM** | Replica autoscaling (scale-to-zero), GPU placement, multi-model routing, LoRA multiplexing | `:8000` (interna apenas) |
| Engine | **vLLM** | Inference: weights in VRAM, KV cache, token generation | in-process (sem porta) |

Fluxo: `Client → :4000 → LiteLLM → :8000 → Ray Serve → vLLM`

Referência arquitetural completa: `docs/ARCHITECTURE.md`

## Directory Layout

Os marcadores de fase saíram: as fases terminaram, e "Phase 2 ✓" ao lado de um
arquivo dizia quando ele nasceu, não o que ele faz. O que segue é o papel de
cada artefato. `tests/test_docs.py::TestDirectoryTree` falha se um nome listado
aqui não existir em disco.

```
idia-server/
├── idia                        ← CLI unificada: deploy, status, user, colleague, service
├── AGENTS.md                   ← este arquivo — regras do projeto para agentes
├── README.md                   ← porta de entrada; operação vive no docs/DEPLOY.md
├── .env.example                ← template de configuração, sem segredos
├── .gitignore
├── pyproject.toml              ← pytest, ruff
├── Dockerfile.ray              ← imagem Ray Serve LLM + vLLM
├── serve_config.yaml           ← template do Ray Serve; as entradas são geradas
├── docker-compose.yml          ← orquestração dos 7 serviços
├── prometheus.yml              ← scrape config
├── .claude/
│   ├── portao                  ← comando do gate local, lido pelo git-guard
│   └── issue-vizinhas          ← seção exigida no corpo de toda issue nova
├── scripts/
│   ├── render_config.py        ← fonte única dos configs do Ray e do LiteLLM
│   ├── colleague.sh            ← provisionamento de usuários, 6 passos
│   ├── gate.sh                 ← o portão: pytest, shellcheck, ruff, YAML
│   ├── smoke_test.sh           ← verificação pós-deploy, com --wait
│   ├── setup_environment.sh    ← prepara a máquina (Linux)
│   ├── install_service.sh      ← unit systemd
│   └── uninstall_service.sh
├── grafana/
│   ├── datasources/datasource.yml   ← Prometheus provisionado
│   └── dashboards/
│       ├── dashboard.yml
│       └── vllm-dashboard.json      ← dashboard oficial vLLM (ID 25043)
├── tests/
│   ├── conftest.py             ← fixtures compartilhadas
│   ├── test_docs.py            ← estrutura da documentação e da árvore
│   ├── test_config_schemas.py  ← schema dos configs e do gerado pelo LiteLLM
│   ├── test_integration.py     ← render, multi-model, orçamento de VRAM
│   ├── test_engine_config.py   ← dtype, quantização, residência, tool calling
│   ├── test_stack_services.py  ← topologia do Compose, portas, banco
│   ├── test_colleague.py       ← provisionamento, tiers, guardas de injeção
│   ├── test_security.py        ← portas, pinning, fronteiras de confiança
│   ├── test_contract.py        ← contratos REST do LiteLLM, sem GPU
│   └── test_deploy_dry_run.py  ← dry-run, CLI, consistência do health check
└── docs/
    ├── ARCHITECTURE.md         ← documento vivo de arquitetura
    ├── DEPLOY.md               ← guia de operação
    ├── ADR.md                  ← Architecture Decision Records
    └── audit_logs/             ← relatórios de auditoria consumidos
```

## Histórico de fases

Todas concluídas. A tabela existia como plano; o que resta dela é o registro
de como o projeto chegou aqui, e o detalhe vive no histórico estrutural do
`ARCHITECTURE.md` §16.7 e nos ADRs.

| Fase | Entregou |
|------|----------|
| 1–2 | Fundação e build core: Dockerfile, entrypoint, Compose, testes |
| 3 | Deploy AWS — **removido** depois; ver ADR-003 e ADR-004 (superseded) |
| 4 | Monitoramento: Prometheus, Grafana provisionado, DCGM |
| 5 | Documentação: ARCHITECTURE, DEPLOY, ADR |
| Post-5 | Automação: CLI unificada, render duplo |
| Post-6 | Simplificação local-only |
| Post-6b | Systemd, para subir no boot |
| Post-6c | Provisionamento de usuários, config de engine por modelo, PostgreSQL e Open WebUI no Compose, portão local |

O trabalho agora é dirigido por issues, não por fases. `gh issue list` é o
plano.

## Document Evolution Contract

### ARCHITECTURE.md — Living Document Rules

O documento de arquitetura evolui com o código. Estas regras previnem desync:

**SYNC-REQUIRED Triggers** — qualquer alteração em:
- `Dockerfile.ray` — imagem base, dependências, entrypoint
- `serve_config.yaml` — modelos, autoscaling, engine_kwargs
- `docker-compose.yml` — serviços, portas, networks, volumes, GPU config
- `scripts/render_config.py` — `_render_litellm_config()`, que é onde o roteamento do LiteLLM realmente mora (§4.3)
- `prometheus.yml` — scrape targets, alert rules
- Qualquer arquivo em `tests/` que introduza nova categoria de teste (#)
- Port mappings, topologia de rede, perímetros de segurança
- Estratégia de carregamento de modelos ou GPU placement

**Minor Update** — version bump, ajuste de parâmetro, nova env var:
- Editar apenas a seção afetada.
- Sem revisão completa do documento.
- Atualizar footer com data e seções alteradas.

**Major Update** — nova camada, novo target de deploy, mudança de pattern:
- Revisão completa do documento.
- Seções antigas marcadas com `[DEPRECATED — see section X]`.
- Requer aprovação humana antes do merge.

**Desync Prevention:**
- Se código e ARCHITECTURE.md discordam: o código é a verdade, mas o doc deve ser atualizado no mesmo PR/commit.
- Toda task de implementação que afeta a arquitetura declara: `[UPDATES ARCHITECTURE.md — section X]` no plano.
- Nunca mergear código sem a atualização correspondente do architecture doc.

**Version Footer:**
```markdown
---
*Document version: 1.1 | Last updated: 2026-06-28 | Sections changed: [list]*
```

### AGENTS.md — Update Rules

- Atualizado quando uma nova Fase é planejada (novos stacks, ferramentas, workflows).
- Atualizado quando versões de componentes mudam materialmente.
- Atualizado quando novas constraints são descobertas durante o desenvolvimento.
- Atualizado quando o diretório ou a suíte de testes muda significativamente.
- A seção **Testing Strategy** abaixo deve refletir exatamente os testes implementados em `tests/`.

---

## Anti-Drift Rule (AXIOM — NON-OVERRIDABLE)

Toda tarefa de implementação que cria, modifica ou remove um artefato de
infraestrutura (Dockerfile, config YAML, Compose, entrypoint, script de
deploy, pipeline de CI/CD, teste de integração ou segurança) DEVE:

1. Declarar no plano: `[UPDATES ARCHITECTURE.md — section X]`
2. Atualizar a seção correspondente no architecture doc no mesmo commit
3. Atualizar o footer de versão do architecture doc
4. Adicionar entrada na Structural Change History

**Violação:** se um artefato for mergeado sem a atualização correspondente da
arquitetura, o commit é considerado incompleto. A correção deve ser feita
antes de qualquer outro trabalho.

Esta regra está em vigor desde a Fase 2 e se aplica a todas as fases
subsequentes.

---

## Governance & Maintainability Axioms (AXIOM — NON-OVERRIDABLE)

Estas regras existem porque a rastreabilidade de decisões e a facilidade de
manutenção são prioridades do projeto. Um novo membro da equipe ou um
agente OpenCode deve conseguir entender qualquer parte do sistema usando
apenas a documentação e os commits — sem entrevistar o autor original.

### 0. Decision Closure Rule — Planos só existem com decisões fechadas

Nenhum plano de implementação é considerado completo enquanto houver
decisões de projeto pendentes. O autor do plano deve:

1. Identificar todas as questões em aberto durante a análise do problema.
2. Documentar cada questão explicitamente no plano.
3. **Fechar cada decisão** antes de concluir o plano, usando:
   - Melhores práticas da área quando o usuário não tiver preferência.
   - A recomendação fundamentada do autor quando o usuário delegar.
   - Investigação adicional (skills, pesquisa, código existente) quando
     necessário — nunca palpites não verificados.
4. Registrar a decisão e sua justificativa no plano ou na documentação.

**Violação:** um plano apresentado com questões em aberto não aprovadas
não autoriza implementação. A implementação deve parar até que todas as
decisões estejam fechadas.

**Exemplo:** se o plano levanta "qual instância EC2 usar?" sem responder,
o plano está incompleto. O autor deve pesquisar, recomendar e documentar
a escolha (ex.: g5.xlarge por 1× A10G 24GB — adequado para modelos 7-8B).

### 1. Architecture Feedback Loop — Toda descoberta de implementação realimenta a arquitetura

A implementação inevitavelmente revela detalhes não antecipados na
arquitetura original. Quando isso acontece:

1. A descoberta é registrada.
2. A arquitetura (`ARCHITECTURE.md`) é atualizada para refletir o
   entendimento corrigido.
3. A implementação prossegue sobre a arquitetura atualizada — nunca
   sobre a versão desatualizada.

**Ciclo:** `Arquitetura → Implementação → Descoberta → Atualização da
Arquitetura → Continuação da Implementação`

**Isso se aplica a:**
- Parâmetros que se revelam diferentes do esperado.
- Workflows que exigem passos adicionais não documentados.
- Dependências ou versões que se provam incompatíveis.
- Qualquer diferença entre o comportamento real e o especificado.

**Registro:** cada iteração do ciclo deve ser rastreável via commit ou
entrada na Structural Change History do `ARCHITECTURE.md`.

### 2. Traceability Axiom — Todo commit deve ser compreensível por um novo membro 6 meses depois

Um commit não é apenas "o que mudou" — é **por que mudou**, qual decisão
foi tomada, e qual alternativa foi descartada.

| Critério | Obrigatório? | Exemplo (bom) | Exemplo (ruim) |
|----------|-------------|---------------|----------------|
| **Por que** esta mudança existe? | ✅ | "serve_config.yaml: pre-render workflow porque config tem placeholders ${VAR} desde a Fase 2" | "serve_config.yaml: update config" |
| **Qual decisão** foi tomada? | ✅ | "Dockerfile.ray: usar imagem rayproject/ray:2.56.0-py311-gpu — bundla vLLM 0.22.0 via ray[serve,llm]" | "Dockerfile: update image" |
| **Qual alternativa** foi descartada? | ✅ | "Opção A (instalar vllm separado) descartada porque quebra o pinning de versão do ray[serve,llm]" | "Dockerfile: fix deps" |
| **O que** mudou (diff)? | ✅ (implícito no git) | — | — |

**Na prática:** a mensagem do commit deve conter, em linguagem natural,
as respostas para "por que", "qual decisão" e "qual alternativa".

**Documentação derivada:** quando uma decisão de implementação modifica
a arquitetura, o `ARCHITECTURE.md` deve ser atualizado no mesmo commit,
e a entrada na Structural Change History deve referenciar o commit.

### 3. Maintainability Over Novelty — Preferir o conhecido sobre o novo

Quando múltiplas abordagens técnicas resolvem o mesmo problema:

1. Preferir a abordagem mais documentada, mais testada e mais conhecida
   pela equipe.
2. Abordagens experimentais ou de vanguarda exigem justificativa
   explícita de por que a abordagem estabelecida não atende.
3. "Porque é mais novo/mais rápido/melhor" não é justificativa suficiente
   sem evidência mensurável para o caso de uso específico.
4. Se uma abordagem nova é escolhida, documentar explicitamente o que
   se espera ganhar e qual o plano de fallback.

**Exceção:** quando o problema ativo não pode ser resolvido por
abordagens estabelecidas — nesse caso, documentar por que.

---

## Code Quality Axioms (AXIOM — NON-OVERRIDABLE)

Estas regras existem porque a auditoria de 2026-06-28 revelou padrões
de falha recorrentes: validação de entrada ausente, dependências não
declaradas, I/O sem diagnóstico, e cobertura de testes insuficiente
para casos de erro. Elas se aplicam a todo código e toda fase.

### 4. Input Validation Rule — Toda env var com tipo restrito deve ser validada

Toda variável de ambiente com tipo numérico (int, float) ou range deve
ser validada antes do uso. A validação deve:
- Rejeitar valores que não podem ser convertidos para o tipo esperado.
- Rejeitar valores fora do range documentado.
- Emitir mensagem clara com o valor recebido e o range esperado.
- Usar `sys.exit(1)` para falhas de validação no entrypoint.

**Aplica-se a:** `GPU_MEMORY_UTILIZATION` (range 0-1), `MAX_MODEL_LEN`
(inteiro positivo).

### 5. Dependency Declaration Rule — Todo import Python deve ter entry em pyproject.toml

Nenhuma dependência pode ser importada sem estar declarada em
`[project.dependencies]` no `pyproject.toml`, com version bounds
explícitos (`>=` para mínimo, `<` para máximo).

**Veda:** confiar em dependências transitivas (ex: Ray inclui PyYAML).
Se o código faz `import yaml`, `pyyaml` deve estar em `pyproject.toml`.

### 6. Error Handling Rule — Toda operação de I/O deve ter diagnóstico explícito

Toda operação de arquivo, rede, ou subprocesso deve ser envolvida em
`try/except` com mensagens que:
- Identifiquem o arquivo/recurso específico que falhou.
- Expliquem a causa provável (permissão, encoding, não encontrado).
- Sugiram uma ação corretiva para o operador.

**Exceção:** operações em funções puras de teste (que não fazem I/O).

### 7. Test Coverage Rule — Caminhos de erro devem ser testados

Para toda função com validação de entrada, os casos de erro devem ser
testados ao lado dos caminhos felizes. A cobertura mínima inclui:
- Valores fora do range esperado.
- Valores com tipo incorreto.
- Caracteres especiais que podem subverter o formato de saída.
- Arquivos ausentes ou inacessíveis.

### 8. Secret Hygiene Rule — Env vars com valores reais nunca são logadas

Nenhuma variável de ambiente com valor real deve ser impressa em
stdout/stderr, exceto em modo `--debug` ou `--dry-run` explicitamente
ativado. Identificadores não-sensíveis (MODEL_ID, nomes de modelo)
podem ser logados. Senhas, tokens, chaves de API nunca devem ser
logados — nem mesmo de forma ofuscada.

### 9. Severity Calibration Rule — Mitigações existentes devem ser avaliadas antes da severidade

Ao classificar a severidade de uma vulnerabilidade:
1. Mapear a superfície de ataque real (quem pode explorar? por qual vetor?).
2. Identificar mitigações existentes (firewall, binding local, rede interna).
3. Atribuir severidade APÓS avaliar mitigações — não antes.

**Guia:**
- CRÍTICO: exploração remota sem autenticação, sem mitigações.
- ALTO: exploração remota com mitigações parciais.
- MÉDIO: exploração que requer acesso prévio (rede interna, SSH, física).
- BAIXO: melhoria defensiva sem risco imediato.

---

## Security Constraints (from ARCHITECTURE 

Derivadas da arquitetura. **Não negociáveis.**

| Regra | Fonte |
|-------|-------|
| **`:4000`** é a ÚNICA porta exposta ao host | §9.1 |
| **`:8000`** (Ray ingress) é interna — nunca mapeada em `docker-compose ports` | §9.3 |
| **`:8265`** (Ray dashboard) é interna — acesso via `docker compose exec` ou túnel SSH | §9.2 |
| **`:10001`** (Ray Client) é interna | §9.2 |
| Todas as imagens **pinnadas a tags imutáveis** — nunca `:latest` | §9.1 |
| **Duas fronteiras de confiança**: master key (admin) vs. virtual keys (clientes) | §9.1 |
| TLS termina na borda (reverse proxy local), nunca dentro dos containers | §9.1 |
| Ray cluster tratado como banco sem autorização — qualquer path de rede = root | §9.2 |
| Dashboard bound a `127.0.0.1`, nunca `0.0.0.0` | §9.2 |
| Ray ≥ 2.54.0 obrigatório (fecha CVE-2026-27482) | §9.2 |

## Container Image Policy

- **`Dockerfile.ray`**: `FROM rayproject/ray:2.56.0-py311-gpu@sha256:9e0af0a2820745fc567bfb3777f7fd38107a9ce72635c5861e473c24ea4dd150`, pinado.
  `RUN pip install "ray[serve,llm]==2.56.0"` — sem vllm separado (bundled como 0.22.0).
- **LiteLLM**: `docker.litellm.ai/berriai/litellm:v1.85.0`, pinado.
- **Prometheus**: `prom/prometheus`, pinado a semver tag específica.
- **Grafana**: `grafana/grafana`, pinado a semver tag específica.
- Nenhuma imagem usa `:latest`.

## Env Var Convention

- Secrets em `.env` (nunca commitado).
- `.env.example` é o template documentado (commitado).
- Todas as env vars seguem `UPPER_SNAKE_CASE`.
- Obrigatórias: `HF_TOKEN`, `LITELLM_MASTER_KEY`, `MODEL_ID`, `MODEL_SOURCE`.
- Opcionais (com defaults documentados): `MAX_MODEL_LEN`, `GPU_MEMORY_UTILIZATION`.

## Model Configuration

O modelo é configurável via `.env` com duas variáveis:

| Variável | Exemplo | Obrigatória |
|----------|---------|-------------|
| `MODEL_ID` | `mistral-7b` | Sim |
| `MODEL_SOURCE` | `mistralai/Mistral-7B-Instruct-v0.3` | Sim |

A implementação do templating usa o entrypoint Python `scripts/render_config.py`, que substitui placeholders `${VAR}` por variáveis de ambiente antes de delegar ao Ray Serve.

---

## Testing Strategy

O IDIA Server usa **pytest 8.x** como executor. A suíte cobre cinco categorias de teste,
cada uma com seu marcador e requisitos de infraestrutura.

### Categorias de Teste

| Marcador | Categoria | O que valida | Requer infraestrutura? | Fase |
|----------|-----------|-------------|----------------------|------|
| `docs` | Documentação | Estrutura de arquivos obrigatórios, seções de documentos vivos, footer de versão | Não — roda com `pip install pytest` | 1 |
| `config` | Schema de configuração | Estrutura YAML de `serve_config.yaml`, `docker-compose.yml`, `config.yaml`, `prometheus.yml`, `.env.example`; Grafana datasource provisioning | Não — apenas PyYAML | 1 |
| `integration` | Integração | `render_config.py`: substituição de env vars, validação YAML, dry-run, caminhos de erro; consistência do Compose (build source, pinning, env vars) | Componente unitário: apenas pytest; full suite: Docker + GPU | 2 |
| `security` | Segurança | Isolamento de portas (`:8000`, `:8265`, `:10001` inacessíveis externamente; apenas `:4000` externa; `:9090` não publicada; `:3000` bound a localhost), pin de imagens (`no :latest`), fronteiras de confiança (master_key declarado), binding do dashboard | Verificação de YAML: apenas pytest; verificação de rede: Docker | 2 |
| (none) | Contrato LiteLLM | Simulação de API LiteLLM: rejeição de modelo inexistente, auth ausente, mensagens inválidas, formato de resposta | Não — puro Python com mock | 5 (Tier 4) |
| `deploy` | Dry-run + CLI | Validação de render_config.py, esquema .env, CLI unificada | Não — puro Python + shell | Post-5 |

### Como executar

```bash
# Instalar dependências de teste
pip install pytest pyyaml

# Rodar testes rápidos (docs + config) — zero infraestrutura
pytest -m "docs or config" -v

# Rodar todos os testes (inclui integração e segurança simulados)
pytest -v

# Rodar testes de um arquivo específico
pytest tests/test_config_schemas.py -v

# Rodar por marcador
pytest -m config -v
```

### O que cada teste valida

#### `test_docs.py` (— docs)

| Teste | O que verifica |
|-------|---------------|
| `test_exists` | Cada arquivo obrigatório (`docs/ARCHITECTURE.md`, `AGENTS.md`, `README.md`) existe |
| `test_is_markdown` | Arquivos começam com `#` (cabeçalho markdown) |
| `test_contains_sections` | Documentos vivos contêm as seções de governança exigidas (Document Evolution Contract, Structural Change History) |
| `test_has_version_footer` | ARCHITECTURE.md tem footer de versão e tabela de histórico estrutural |
| `test_phase_markers_match_code` | README.md: marcadores de fase (Phase N ✓) correspondem a arquivos existentes no disco |
| `test_directory_listed_files_exist` | Todos os arquivos listados na árvore do README existem |
| `test_adr_exists` | ADR.md existe e não está vazio |
| `test_adr_has_entries` | ADR.md contém pelo menos 4 entradas ADR |
| `test_adr_required_sections` | Cada entrada ADR tem Contexto, Decisão, Alternativa descartada, Consequências |
| `test_adr_references_phase` | Cada entrada ADR referencia a Fase de origem |
| `test_adr_has_status` | Cada entrada ADR tem Status (Accepted/Superseded/Deprecated) |
| `test_license_exists` | LICENSE existe e não está vazio |
| `test_license_is_apache2` | LICENSE é Apache 2.0 |

#### `test_config_schemas.py` (— config)

Cada classe de teste valida a estrutura de um arquivo de configuração contra a especificação na arquitetura:

| Classe | Arquivo alvo | Key assertions |
|--------|-------------|---------------|
| `TestServeConfig` | `serve_config.yaml` | `proxy_location: EveryNode`, `http_options.port: 8000`, `applications` é lista não-vazia |
| `TestDockerCompose` | `docker-compose.yml` | Serviços `ray-head` e `litellm` presentes; `ipc: host` e `shm_size` em ray-head |
| `TestLiteLLMConfig` | saída de `render_litellm_config()` | `model_list` não-vazia, rota para `ray-head:8000`, master_key como referência de env |
| `TestPrometheusConfig` | `prometheus.yml` | `global` e `scrape_configs`; targets apontam para `ray-head:8080` e `litellm:4000`; scrape_interval=15s; sem rule_files (alertas no Grafana) |
| `TestGrafanaDatasourceConfig` | `grafana/datasources/datasource.yml` | datasource Prometheus configurado como default; url=http://prometheus:9090; access=proxy |
| `TestEnvExample` | `.env.example` | Declara `HF_TOKEN`, `LITELLM_MASTER_KEY`, `MODEL_ID`, `MODEL_SOURCE` |

#### `test_integration.py` (— integration)

| Classe/Teste | O que verifica |
|-------------|---------------|
| `TestRenderConfig.test_render_with_minimal_env` | `render()` substitui placeholders com env vars |
| `TestRenderConfig.test_render_injects_defaults` | Optional vars (GPU_MEMORY_UTILIZATION) usam default quando ausentes |
| `TestRenderConfig.test_render_validates_full_template` | Estrutura do YAML renderizado corresponde a `§5.3` (proxy_location, port, min/max_replicas) |
| `TestRenderConfig.test_dry_run_flag` | `--dry-run` produz YAML válido sem executar serve |
| `TestRenderConfigErrors.test_missing_required_var_fails` | Exit 1 com mensagem se MODEL_ID ausente |
| `TestRenderConfigErrors.test_bad_yaml_template_fails` | Exit 1 se template inválido após substituição |
| `TestRenderSchemaErrors.test_gpu_util_above_range_fails` | GPU_MEMORY_UTILIZATION=1.5 → exit |
| `TestRenderSchemaErrors.test_gpu_util_negative_fails` | GPU_MEMORY_UTILIZATION=-0.5 → exit |
| `TestRenderSchemaErrors.test_max_model_len_non_numeric_fails` | MAX_MODEL_LEN=abc → exit |
| `TestRenderSchemaErrors.test_model_id_with_yaml_special_chars_escaped` | MODEL_ID com `:{}` é escapado, não injetado |
| `TestComposeConsistency.test_ray_head_builds_locally` | ray-head usa build local (Dockerfile.ray) |
| `TestComposeConsistency.test_litellm_uses_pinned_image` | litellm usa tag semver, não :latest |
| `TestComposeConsistency.test_ray_head_passes_vars_to_entrypoint` | ray-head passa MODEL_ID, MODEL_SOURCE, MAX_MODEL_LEN, GPU_MEMORY_UTILIZATION |

#### `test_security.py` (— security)

| Classe/Teste | O que verifica |
|-------------|---------------|
| `TestPortIsolation.test_only_4000_published` | Apenas porta 4000 aparece em `ports:` no Compose |
| `TestPortIsolation.test_ray_ingress_not_published` | Porta 8000 NÃO está em `ports:` |
| `TestPortIsolation.test_dashboard_not_published` | Porta 8265 NÃO está em `ports:` |
| `TestPortIsolation.test_ray_client_not_published` | Porta 10001 NÃO está em `ports:` |
| `TestImagePinning.test_dockerfile_no_latest` | Dockerfile.ray não usa `:latest` |
| `TestImagePinning.test_compose_no_latest` | Nenhum serviço no Compose usa `:latest` |
| `TestTrustBoundaries.test_generated_config_never_embeds_the_master_key` | o config renderizado nunca contém o valor real da master key |
| `TestDashboardBinding.test_dashboard_host_set_to_localhost` | serve_config.yaml http_options.host=0.0.0.0 (proxy interno)
| `TestMonitoringPortIsolation.test_prometheus_port_not_published` | Porta 9090 (Prometheus) não está em `ports:` no Compose
| `TestMonitoringPortIsolation.test_grafana_port_bound_localhost` | Porta 3000 (Grafana) bound a 127.0.0.1

### Política de Skipping

Testes que dependem de arquivos de fases futuras usam `pytest.skip()` com mensagem
explicativa — nunca falham pela ausência de algo que ainda será criado.
Isso permite que a suíte rode limpa desde a Fase 1.

### Adicionando Novos Testes

1. Criar arquivo `tests/test_<area>.py`.
 2. Usar o marcador apropriado: `@pytest.mark.docs`, `@pytest.mark.config`,
    `@pytest.mark.integration`, `@pytest.mark.security`.
3. Usar as fixtures compartilhadas de `conftest.py` (`repo_root`, `docs_dir`, `config_files`).
4. Se o teste depende de um arquivo de fase futura, usar `pytest.skip()` se o arquivo não existir.
5. Registrar o novo marcador em `pyproject.toml` se for nova categoria.
6. Atualizar esta seção no AGENTS.md.

---

## O portão local

`./scripts/gate.sh` roda antes de abrir qualquer PR: `pytest`, `bash -n` e
`shellcheck` em todo shell, `ruff` no Python mantido, e parse dos YAML de
configuração. Ele grava o marcador que o `git-guard` confere — sem isso o
hook recusa o PR que o gate acabou de aprovar.

O caminho está em `.claude/portao`, que pertence ao repositório e não à
máquina. `.claude/issue-vizinhas` exige a seção `### Vizinhas` no corpo de
toda issue nova: uma issue que nunca pergunta com quem colide é indistinguível
de uma que perguntou e não achou nada, e só uma das duas é honesta.

## Git Conventions

Segue `~/.config/opencode/AGENTS.md §8` (Conventional Commits).
Commits incluem referência `[phase-N]` quando sob plano ativo.

## Stack-Specific Rules

- **Docker Compose v2** obrigatório (`docker compose`, não `docker-compose`).
- **Nunca** expor portas internas (8000, 8265, 10001) no `docker-compose.yml`.
- Toda imagem pinada por tag semver — `:latest` proibido.
- Secrets via `.env` + variáveis de ambiente, nunca hardcoded.
- Configs YAML seguem schemas validados por `tests/test_config_schemas.py`.
