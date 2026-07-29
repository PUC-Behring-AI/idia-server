# Self-Hosted Inference Server: vLLM + Ray Serve + LiteLLM
### Architecture, Deployment, Elastic Scaling, Cost, and Operations Reference

---

## 1. Scope

This document specifies a self-hosted LLM inference server with **automatic GPU elasticity** and **on-demand model loading**, deployable identically on a local multi-GPU host and on AWS. It is a complete, standalone reference: build artifacts, both deployment targets, client integration, security, monitoring, fine-tuning, cost planning, and troubleshooting.

The system is three tiers:

| Tier | Component | Role |
|---|---|---|
| Gateway | **LiteLLM** | Auth, virtual keys, per-key budgets and rate limits, spend tracking |
| Orchestration | **Ray Serve LLM** | Replica autoscaling (including scale-to-zero), GPU placement, multi-model routing, LoRA multiplexing |
| Engine | **vLLM** | Inference: model weights in VRAM, KV cache, token generation |

The gateway tier exists specifically because this deployment serves **multiple users/applications, each requiring individual budget and rate-limit enforcement**. A single-tenant deployment with no per-user accounting could omit LiteLLM and expose Ray Serve's own OpenAI-compatible ingress directly; that variant is noted where relevant but is not the configuration documented here.

All paths, model names, and credentials below are placeholders.

---

## 2. Glossary

| Term | Definition |
|---|---|
| **vLLM** | Open-source inference engine. Loads model weights into GPU memory and exposes an OpenAI-compatible HTTP API. |
| **PagedAttention** | vLLM's memory-management algorithm. Partitions the KV cache into fixed-size, non-contiguous blocks — analogous to OS virtual-memory paging — eliminating memory fragmentation. |
| **KV Cache** | Per-token key/value tensors cached during autoregressive decoding to avoid recomputing attention over the full sequence each step. The dominant consumer of GPU memory at serving time. |
| **Continuous Batching** | Scheduling strategy where new requests join an in-flight batch as soon as a GPU memory slot frees up, rather than waiting for a fixed batch window. Primary driver of vLLM's throughput and the basis of multi-tenant cost efficiency (§14). |
| **Ray / Ray Core** | Distributed compute framework. Schedules Python tasks and stateful actors across a pool of machines (a "cluster"), abstracting away which physical node/GPU executes the work. |
| **Ray Cluster** | One head node plus zero or more worker nodes, managed as a single logical pool of CPU/GPU/memory that Ray schedules onto. |
| **Ray Actor** | A stateful Python process Ray schedules onto cluster resources. Ray Serve replicas are actors under the hood. |
| **Ray (Cluster) Autoscaler** | Cluster-level autoscaler. Provisions or terminates entire cloud VMs (nodes) based on aggregate demand across the whole cluster — this is what adds *physical GPU capacity*. |
| **Ray Serve** | Ray's model-serving library. Deploys "Deployments" (a named group of replicas) behind an HTTP ingress. |
| **Ray Serve (Application) Autoscaler** | Application-level autoscaler, distinct from the cluster autoscaler. Adds or removes *replicas of one specific deployment* based on that deployment's own in-flight request load — this adds *logical capacity for one model*, independent of whether new physical nodes are needed. |
| **Ray Serve LLM** | Purpose-built Ray Serve module for LLM serving. Wraps an inference engine (vLLM or SGLang) and adds multi-model routing, autoscaling, and multiplexing. |
| **LLMConfig / ModelLoadingConfig** | Ray Serve LLM's configuration objects describing a model's source, engine arguments, and `autoscaling_config`. |
| **Model Multiplexing** | Serving multiple model variants (typically LoRA adapters sharing one base model) from a shared replica pool, swapping the active variant per request, with **LRU eviction** when GPU memory is needed for a different variant. The mechanism behind "load on demand, drop the least-recently-used when full." |
| **Scale-to-Zero** | `min_replicas: 0` in a deployment's autoscaling config. The deployment holds zero resident replicas while idle, freeing its GPU entirely. |
| **Cold Start** | The latency of provisioning a new replica and loading model weights into VRAM, paid by the first request after scaling up from zero (or after a new node joins the cluster). |
| **Duty Cycle** | The fraction of wall-clock time a deployment actually has a replica running. Under scale-to-zero, cost is proportional to duty cycle, not to the clock (§14). |
| **KubeRay** | Kubernetes operator that manages Ray clusters as native k8s resources (`RayCluster`/`RayService` CRDs). The path taken if Ray standalone is outgrown. |
| **OpenAI-compatible API** | An HTTP interface implementing OpenAI's `/v1/chat/completions` schema. Any client built on the OpenAI SDK works unmodified. |
| **LiteLLM** | Open-source AI gateway/proxy. Issues scoped virtual keys, tracks spend, enforces budgets/rate-limits, and can unify self-hosted backends with commercial provider APIs under one endpoint. |
| **Master Key** | LiteLLM's admin credential, used to issue virtual keys. Never distributed to end clients. |
| **Virtual Key** | A LiteLLM-issued credential scoped to a budget, rate limit, and/or model-access policy — what end clients receive. |
| **Tensor Parallelism (TP)** | Splitting a model's weight matrices across multiple GPUs so one forward pass spans devices. Used when a model does not fit on one GPU — a *fit* problem, not a *throughput* problem. |
| **LoRA / QLoRA** | Parameter-efficient fine-tuning. Freezes base weights `W`, trains small low-rank matrices `A`,`B` such that `y = xW + xAB`. QLoRA does this on a 4-bit quantized base. |
| **ShadowRay** | Real-world attack campaign (active since 2023, resurgent in 2026 as "ShadowRay 2.0") exploiting unauthenticated, publicly exposed Ray Dashboard/Jobs API instances for remote code execution and cryptomining. See §9.2. |
| **AWS DLC (Deep Learning Containers)** | AWS-maintained, pre-optimized Docker images for ML frameworks, including vLLM, ready for EC2/ECS/EKS/SageMaker. |
| **Fargate** | AWS's serverless container compute. **Does not support GPU** — rules out the simplest "serverless container" path for any GPU tier of this stack. |

---

## 3. Architecture & Mechanism

### 3.1 Tier responsibilities

| Tier | Owns | Does NOT own |
|---|---|---|
| **LiteLLM** | Auth, virtual keys, per-key budget/rate-limit, spend tracking, optional unification with commercial APIs | GPU placement, autoscaling, model loading |
| **Ray Serve LLM** | Replica autoscaling (incl. scale-to-zero), GPU-aware placement, multi-model routing, LoRA multiplexing/eviction | Per-user auth/budget, external providers |
| **vLLM** | Inference: weights in VRAM, KV cache, token generation | Everything above — it is a single-model engine with no concept of users, keys, or other models |

```
Client (app / script / curl)
        │  HTTPS, OpenAI request format, virtual key
        ▼
┌─────────────────────────┐
│  LiteLLM      (:4000)    │  CPU only — auth, budget, rate-limit, spend tracking
└──────────┬───────────────┘
           │ internal network only — never exposed externally
           ▼
┌─────────────────────────┐
│  Ray Serve LLM (:8000)   │  Autoscaling, GPU placement, multi-model/LoRA routing
│  - replica autoscaler    │
│  - model multiplexer     │
└──────────┬───────────────┘
           │ in-process / same-node GPU scheduling
           ▼
┌─────────────────────────┐
│  vLLM engine instance(s) │  GPU — model weights + KV cache, per replica
└─────────────────────────┘
```

The client always addresses `:4000`. The host behind that address — laptop, single EC2 instance, or an autoscaling cluster — is invisible to the client. This is the property that makes local and cloud deployment identical from the consumer's side, and it is the reason the gateway is the outermost tier.

### 3.2 The two autoscalers

The system has two independent autoscaling loops operating at different granularities. Conflating them is the most common source of confusion when reasoning about capacity and cost.

| | Ray Serve Autoscaler | Ray (Cluster) Autoscaler |
|---|---|---|
| **Scope** | One deployment (one model) | The whole cluster (all nodes) |
| **Adds/removes** | Replicas (processes) | Nodes (VMs) |
| **Trigger** | `target_ongoing_requests` exceeded for that deployment | Aggregate resource demand exceeds what current nodes provide |
| **Where configured** | `autoscaling_config` inside each `LLMConfig` | Not applicable (local-only deployment) |
| **Answers** | "Do I need another copy of this model running?" | "Do I need another physical GPU machine at all?" |

On a single local multi-GPU host, only the **replica** autoscaler is active — there is no second node to add. On AWS via the Ray Cluster Launcher, **both** operate in sequence: Ray Serve decides it needs another replica → if no GPU slot is free on existing nodes → the cluster autoscaler requests a new EC2 instance to host it. This chained behavior is the mechanism by which added budget converts to added capacity with no manual code changes (§13).

### 3.3 GPU auto-detection

A single Ray process started with access to all GPUs on a node (`--gpus all` at the container level) automatically detects each GPU as a separate schedulable resource and places replicas onto whichever GPU is free. No per-GPU configuration block, no manual device pinning, no per-device config entry is required. Adding a physical GPU to a host requires only a container restart so Ray re-enumerates devices (on a bare-metal Ray process, GPUs present at boot are picked up on the next `ray start`).

### 3.4 Request lifecycle

1. Client sends a request to LiteLLM with a virtual key.
2. LiteLLM validates the key against its budget/rate-limit policy, resolves `model_name` to a Ray Serve backend URL.
3. LiteLLM forwards to Ray Serve LLM's ingress (`http://ray-head:8000/v1/...`).
4. Ray Serve's `OpenAiIngress` routes to the correct model's deployment. If that deployment is at `min_replicas: 0` and idle, this request triggers a **cold start** (§13.2).
5. The deployment's replica (a vLLM engine instance) admits the request into its running batch (continuous batching) and generates tokens.
6. Tokens stream back: vLLM → Ray Serve → LiteLLM → client.
7. LiteLLM logs cost/latency against that virtual key.

---

## 4. Component Reference

### 4.1 vLLM

The engine runs inside a Ray Serve LLM deployment. Its tuning parameters are supplied through `engine_kwargs` in the `LLMConfig` rather than as standalone CLI flags. Common parameters:

| Parameter | Purpose |
|---|---|
| `dtype` | Weight precision (`bfloat16`, `float16`, `fp8`). Lower precision trades quality for VRAM. |
| `gpu_memory_utilization` | Fraction of GPU memory reserved for weights + KV cache (default 0.9). Lower it when other processes share the GPU. |
| `max_model_len` | Maximum context length served; bounds KV-cache sizing. |
| `tensor_parallel_size` | Number of GPUs to shard one model across. Required only when a model does not fit on one GPU. |
| `quantization` | `awq`, `gptq`, `fp8` — reduces VRAM when a model would not otherwise fit. |

vLLM requires NVIDIA compute capability ≥ 7.0 (V100 and newer).

### 4.2 Ray Serve LLM — configuration anatomy

```python
from ray.serve.llm import LLMConfig, ModelLoadingConfig, build_openai_app
from ray import serve

llm_config = LLMConfig(
    model_loading_config=ModelLoadingConfig(
        model_id="mistral-7b",                          # alias clients/LiteLLM use
        model_source="mistralai/Mistral-7B-Instruct-v0.3",
    ),
    engine_kwargs=dict(
        dtype="bfloat16",
        gpu_memory_utilization=0.9,
        max_model_len=8192,
    ),
    deployment_config=dict(
        autoscaling_config=dict(
            min_replicas=1,             # 0 = scale-to-zero; see §13.2
            max_replicas=4,
            target_ongoing_requests=64,
        )
    ),
)
app = build_openai_app({"llm_configs": [llm_config]})
serve.run(app)
```

| `autoscaling_config` field | Meaning |
|---|---|
| `min_replicas` | Floor. `0` enables scale-to-zero with automatic wake-on-request (§13.2). |
| `max_replicas` | Ceiling for this *deployment* (not the cluster). |
| `target_ongoing_requests` | Desired average concurrent requests per replica; the controller scales to keep actual load near this value. |
| `max_ongoing_requests` | Hard cap per replica before requests queue. |

### 4.3 LiteLLM — routing configuration

LiteLLM treats Ray Serve's ingress as a custom OpenAI-compatible provider: the provider token is `openai` (meaning "speak the OpenAI protocol to this base URL"), and everything after the first `/` is the model identifier passed through to the backend.

The configuration is maintained in `config.yaml` at the repository root, rendered with env var substitution at runtime. The master key is injected via `${LITELLM_MASTER_KEY}` (required — no fallback; see SEC-01).

```yaml
model_list:
  - model_name: mistral-7b
    litellm_params:
      model: openai/mistral-7b        # must match model_id in ModelLoadingConfig
      api_base: http://ray-head:8000/v1 # Ray Serve's ingress, internal network only
      api_key: "no-auth-internal"       # Ray's ingress has no per-request key by default — see §9.3

general_settings:
  master_key: ${LITELLM_MASTER_KEY}    # required — no fallback (SEC-01)
  max_parallel_requests: 20            # global concurrent request cap

litellm_settings:
  require_auth_for_metrics_endpoint: false  # Prometheus scrape without bearer token
  default_team_settings:
    - team_alias: hard
      rpm_limit: 15
      tpm_limit: 50000
    - team_alias: regular
      rpm_limit: 4
      tpm_limit: 15000
    - team_alias: light
      rpm_limit: 1
      tpm_limit: 5000
```

The `master_key` is validated by `render_config.py` (required env var). The `require_auth_for_metrics_endpoint` setting exists because LiteLLM 1.84.0+ changed the default to require authentication on the `/metrics` endpoint (PR #24600), breaking Prometheus scrape targets that do not send a bearer token. This flag restores public access — a safe default because LiteLLM's port 4000 is only reachable within the internal Compose network, not externally.

For the full file, see `config.yaml` at the repository root. For client consumption patterns, see §8.

---

## 5. Build Process

### 5.1 Directory layout

```
inference-server/
├── Dockerfile.ray         # builds the Ray Serve LLM image
├── serve_config.yaml      # Ray Serve application config (models, autoscaling)
├── docker-compose.yml     # local / single-EC2 orchestration
├── config.yaml            # LiteLLM model routing
├── .env                   # secrets, not committed
└── prometheus.yml         # monitoring, §10
```

### 5.2 `Dockerfile.ray`

```dockerfile
FROM rayproject/ray:2.56.0-py311-gpu@sha256:9e0af0a2820745fc567bfb3777f7fd38107a9ce72635c5861e473c24ea4dd150
RUN pip install --no-cache-dir "ray[serve,llm]==2.56.0"
WORKDIR /app
COPY serve_config.yaml scripts/render_config.py ./
CMD ["python3", "/app/render_config.py"]
```

The base image `ray:2.56.0-py311-gpu` is CUDA 12.1 (the `-gpu` tag is an alias for `-cu121` — same amd64 digest). The SHA256 is pinned for deterministic builds (§9.1). `ray[serve,llm]==2.56.0` resolves `vllm==0.22.0` internally via its own `llm-requirements.txt` — no separate vLLM pin is needed or desired, since overriding the bundled version would risk breaking the tested combination. vLLM 0.22.0 constrains `transformers>=4.56,!=5.0-5.5.0`, which keeps the package in the 4.x series and avoids the `AttributeError: 'PreTrainedConfig' object has no attribute 'max_position_embeddings'` regression introduced by transformers 5.x in `standardize_rope_params` for Llama models with custom `rope_scaling`. Ray 2.56.0 also includes PR #62464 which fixes the root cause: `_infer_supports_vision` now uses `AutoConfig.from_pretrained()` instead of `PretrainedConfig.from_pretrained()`, correctly resolving the concrete `LlamaConfig` class.

> **Migration note (2.55.0 → 2.56.0):** `rayproject/ray-ml` GPU tags were discontinued after 2.47.x. The correct base image is `rayproject/ray` (non-ml), which is identical in terms of Ray + CUDA dependencies for LLM workloads.

The CMD delegates to a Python entrypoint (`render_config.py`, see §5.6) that reads `serve_config.yaml`, substitutes `${VAR}` placeholders from environment variables, writes the rendered YAML to a temp file, and then `exec`s `serve run` — replacing the Python process with Ray Serve without a fork. This substitution is necessary because `serve_config.yaml` is consumed by Ray Serve directly and cannot access shell env vars natively.

### 5.3 `serve_config.yaml`

```yaml
proxy_location: EveryNode
http_options:
  host: 0.0.0.0       # binds inside the container only — never publish this port on the host, §9.3
  port: 8000

applications:
  - name: llms
    import_path: ray.serve.llm:build_openai_app
    route_prefix: "/"
    args:
      llm_configs:
        - model_loading_config:
            model_id: ${MODEL_ID}
            model_source: ${MODEL_SOURCE}
          engine_kwargs:
            dtype: bfloat16
            gpu_memory_utilization: ${GPU_MEMORY_UTILIZATION}
            max_model_len: ${MAX_MODEL_LEN}
          deployment_config:
            health_check_period_s: 30     # crashloop protection (STRUCT-14 / T4.3)
            health_check_timeout_s: 10
            autoscaling_config:
              min_replicas: 0
              max_replicas: 4
              target_ongoing_requests: 64
```

Placeholders `${VAR}` are substituted at runtime by `render_config.py` (§5.6).
The env vars that map to each placeholder are documented in §5.5 and in
`.env.example` at the repository root.

**Multi-model mode:** Set `MODELS_COUNT=N` and `MODEL_1_ID` / `MODEL_1_SOURCE`
through `MODEL_N_ID` / `MODEL_N_SOURCE`. The `##LLM_CONFIGS##` marker in the
template triggers dynamic generation of `N` model entries, each with its own
deployment and independent autoscaling (min_replicas=0, scale-to-zero).
Single-model mode (`MODEL_ID` / `MODEL_SOURCE`) remains fully backward-compatible.

See `tests/test_integration.py::TestRenderConfig::test_multi_model_renders_multiple_entries`
for the expected output structure.

### 5.4 `docker-compose.yml`

```yaml
services:
  ray-head:
    build:
      context: .
      dockerfile: Dockerfile.ray
    ipc: host
    shm_size: "${RAY_SHM_SIZE:-4gb}"
    volumes:
      - idia_hf_cache:/root/.cache/huggingface
    environment:
      - HUGGING_FACE_HUB_TOKEN=${HF_TOKEN}
      # Single-model (backward compatible)
      - MODEL_ID=${MODEL_ID}
      - MODEL_SOURCE=${MODEL_SOURCE}
      # Multi-model: set MODELS_COUNT=N and MODEL_N_ID/MODEL_N_SOURCE (§5.3)
      - MODELS_COUNT=${MODELS_COUNT:-}
      - MODEL_1_ID=${MODEL_1_ID:-}
      - MODEL_1_SOURCE=${MODEL_1_SOURCE:-}
      - MODEL_2_ID=${MODEL_2_ID:-}
      - MODEL_2_SOURCE=${MODEL_2_SOURCE:-}
      - MODEL_3_ID=${MODEL_3_ID:-}
      - MODEL_3_SOURCE=${MODEL_3_SOURCE:-}
      - MAX_MODEL_LEN=${MAX_MODEL_LEN:-8192}
      - GPU_MEMORY_UTILIZATION=${GPU_MEMORY_UTILIZATION:-0.9}
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/metrics"]
      interval: 15s
      timeout: 5s
      retries: 5
      start_period: 60s
    deploy:
      resources:
        limits:
          memory: "${RAY_MEMORY_LIMIT:-16g}"
        reservations:
          memory: "${RAY_MEMORY_RESERVATION:-8g}"
          devices:
            - driver: nvidia
              count: all              # all GPUs on the host — Ray distributes replicas across them itself
              capabilities: [gpu]
    restart: unless-stopped
    # NO "ports:" mapping — dashboard (8265), ingress (8000) and client (10001)
    # stay on the internal Compose network only. See §9.3.

  litellm:
    image: docker.litellm.ai/berriai/litellm:v1.85.0
    depends_on:
      ray-head:
        condition: service_healthy
    ports:
      - "4000:4000"                  # the only port exposed to the host
    volumes:
      - ./config.yaml:/app/config.yaml
    environment:
      - LITELLM_MASTER_KEY=${LITELLM_MASTER_KEY}
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:4000/health"]
      interval: 15s
      timeout: 5s
      retries: 5
      start_period: 30s
    command: ["--config=/app/config.yaml"]
    restart: unless-stopped

  prometheus:
    image: prom/prometheus:v2.55.0
    command:
      - '--config.file=/etc/prometheus/prometheus.yml'
      - '--storage.tsdb.retention.time=15d'
      - '--storage.tsdb.retention.size=5GB'
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml
      - prometheus_data:/prometheus
    deploy:
      resources:
        limits:
          memory: "1g"               # prevent unbounded /prometheus growth
    restart: unless-stopped
    # NO ports: published — Prometheus is queried by Grafana on internal
    # Compose network. For admin access: docker compose exec prometheus sh.

  grafana:
    image: grafana/grafana:11.4.0
    depends_on:
      - prometheus
    ports:
      - "127.0.0.1:3000:3000"       # localhost only — no external access
    volumes:
      - ./grafana/datasources:/etc/grafana/provisioning/datasources
      - ./grafana/dashboards:/etc/grafana/provisioning/dashboards
      - grafana_data:/var/lib/grafana
    environment:
      - GF_SECURITY_ADMIN_USER=admin
      - GF_SECURITY_ADMIN_PASSWORD=${GRAFANA_ADMIN_PASSWORD}
    restart: unless-stopped

volumes:
  prometheus_data:
    name: idia_prometheus_data
  grafana_data:
    name: idia_grafana_data
  idia_hf_cache:
    name: idia_hf_cache
```

Changes from Phase 2:
- **HF cache volume:** bind mount → named volume `idia_hf_cache` (SEC-03). Read-write
  inside the container but isolated from the host's `~/.cache/huggingface`,
  preventing container compromise from exfiltrating host HF tokens.
- **Health checks:** ray-head probes `/metrics` on port 8080; litellm probes
  `/health` on port 4000 (SEC-08). LiteLLM uses `depends_on.condition:
  service_healthy` so it starts only after ray-head is ready.
- **Memory limits:** ray-head limited to 16 GB with an 8 GB reservation
  (SEC-11). Both configurable via `RAY_MEMORY_LIMIT` and
  `RAY_MEMORY_RESERVATION`.
- **Shared memory:** `shm_size` configurable via `RAY_SHM_SIZE` env var
  (default 4 GB) for large-model compatibility (INFRA-02).
- **Grafana admin password:** read from `GRAFANA_ADMIN_PASSWORD` env var
  (SEC-07). Without this, Grafana uses `admin:admin` as default credentials.
- **Prometheus retention:** `--storage.tsdb.retention.time=15d` and
  `--storage.tsdb.retention.size=5GB` prevent the `/prometheus` volume from
  growing unbounded (INFRA-01).
- **DCGM Exporter (T4.2):** added `dcgm-exporter` service with `profiles: ["gpu"]`
  to expose NVIDIA GPU metrics (utilization, VRAM, temperature) to Prometheus.
  Only activates with `docker compose --profile gpu up` — skipped on macOS/CI
  where no GPU is available. Port 9400 internal only (not published).

### 5.5 `.env`

See `.env.example` at the repository root for the full documented template.
Only `.env` (without `.example`) contains secrets and is never committed.

```
HF_TOKEN=hf_xxx
LITELLM_MASTER_KEY=sk-litellm-admin-change-me
MODEL_ID=mistral-7b
MODEL_SOURCE=mistralai/Mistral-7B-Instruct-v0.3
MAX_MODEL_LEN=8192          # optional — see defaults below
GPU_MEMORY_UTILIZATION=0.9  # optional — see defaults below
GRAFANA_ADMIN_PASSWORD=      # required if Grafana is enabled (see §5.4)
```

**Variable reference:**

| Variable | Required | Type | Default | Used by |
|----------|----------|------|---------|---------|
| `HF_TOKEN` | Yes | str | — | `Dockerfile.ray` → HuggingFace Hub |
| `LITELLM_MASTER_KEY` | Yes | str | — | `config.yaml` (LiteLLM) |
| `MODEL_ID` | Yes | str | — | `serve_config.yaml` (Ray) |
| `MODEL_SOURCE` | Yes | str | — | `serve_config.yaml` (Ray) |
| `MAX_MODEL_LEN` | No | int | 8192 | `serve_config.yaml` (vLLM engine_kwargs) |
| `GPU_MEMORY_UTILIZATION` | No | float | 0.9 | `serve_config.yaml` (vLLM engine_kwargs) |
| `GPU_COUNT` | No | int | 1 | `render_config.py` (multi-model VRAM budget check, T4.1) |
| `GPU_VRAM_GB` | No | float | 24.0 | `render_config.py` (multi-model VRAM budget check, T4.1) |
| `GRAFANA_ADMIN_PASSWORD` | Yes* | str | — | `docker-compose.yml` (Grafana) |
| `RAY_SHM_SIZE` | No | str | 4gb | `docker-compose.yml` (ray-head shm_size) |
| `RAY_MEMORY_LIMIT` | No | str | 16g | `docker-compose.yml` (ray-head deploy.limits.memory) |
| `RAY_MEMORY_RESERVATION` | No | str | 8g | `docker-compose.yml` (ray-head deploy.reservations.memory) |

\* `GRAFANA_ADMIN_PASSWORD` is required when the Grafana service is included
in the stack; without it Grafana falls back to its built-in `admin:admin`
credentials, which is a security risk (SEC-07).

The template YAML (`serve_config.yaml`) uses `${VAR}` placeholders; the
Python entrypoint (§5.6) substitutes them at container startup. LiteLLM
parses `${VAR:default}` internally — both use the same convention but with
different substitution engines.

### 5.6 Entrypoint script — `scripts/render_config.py`

The Docker CMD in `Dockerfile.ray` (§5.2) does not call `serve` directly.
Instead it launches a Python entrypoint that performs env var substitution
on `serve_config.yaml` before delegating to Ray Serve.

**Why a Python entrypoint instead of `envsubst` or shell?**

| Approach | Mechanism | Dependencies | Error handling |
|----------|-----------|-------------|----------------|
| Shell `envsubst` | `gettext-base` + `envsubst` | Must `apt-get install` in image | Silent — unknown placeholders passed through as literals |
| **Python (chosen)** | `yaml.safe_load` + `re.sub` + `os.execlp` | Python + PyYAML (both already in the `ray` base image) | Explicit: missing required vars → exit 1; invalid YAML → exit 1 |

**Behavior:**

1. Locate `serve_config.yaml` (searches script directory then `/app`).
2. Read template with `${VAR}` placeholders and optional `##LLM_CONFIGS##`
   marker for multi-model (`_read_file` with explicit `try/except` for
   file-not-found, permission, and encoding errors).
3. Collect environment:
   - **Single-model mode** (default): required vars `MODEL_ID`, `MODEL_SOURCE`
     must be set.
   - **Multi-model mode** (when `MODELS_COUNT=N` is set): required vars
     `MODEL_1_ID`/`MODEL_1_SOURCE` through `MODEL_N_ID`/`MODEL_N_SOURCE`
     must be set; `MODEL_ID`/`MODEL_SOURCE` are not validated.
   - Optional vars (`MAX_MODEL_LEN`, `GPU_MEMORY_UTILIZATION`) get defaults
     via `_apply_defaults()`.
 4. **Schema validation:** validates values against constraints:
    - `GPU_MEMORY_UTILIZATION`: float in (0, 1]
    - `MAX_MODEL_LEN`: positive integer
    - `GPU_COUNT`: positive integer (≥ 1)
    - `GPU_VRAM_GB`: positive float
    - **VRAM budget (T4.1):** in multi-model mode, checks that `MODELS_COUNT × GPU_MEMORY_UTILIZATION ≤ GPU_COUNT`. If exceeded, exits with diagnostic. This prevents silently scheduling 3 models on 1 GPU at 90% utilization each (would OOM).
5. Handle `##LLM_CONFIGS##` marker:
   - **Multi-model**: generate N `llm_config` entries from `MODEL_N_ID`/
     `MODEL_N_SOURCE`, replace marker with generated YAML, remove fallback
     single-model entry.
   - **Single-model**: remove marker, keep fallback entry for backward
     compatibility.
6. Substitute `${VAR}` placeholders using regex `\$\{(\w+)\}`. Values
   containing YAML special characters (`:`, `{`, `}`, `\n`, `#`) are
   automatically escaped as quoted YAML scalars via `_escape_yaml_value()`
   to prevent YAML injection (SEC-06).
7. Validate rendered YAML: parse with `yaml.safe_load`, verify structural
   keys (`applications`, `llm_configs`, each entry with non-empty
   `model_id` and `model_source`).
8. Write rendered YAML to a deterministic path (`/tmp/idia_serve_config.yaml`,
   overwritten on each run) — replaces the previous `NamedTemporaryFile`
   approach that leaked files on `os.execlp` (BUG-03).
9. `exec serve run` on the rendered file (replaces the Python process).

**Testing hook:** the module exposes a `render()` pure function and a
`--dry-run` CLI flag that prints the rendered YAML to stdout without
launching Ray Serve — used by `tests/test_integration.py`.

**Dependency declaration:** `import yaml` requires `pyyaml>=6.0,<7.0` declared
in `pyproject.toml` (INFRA-03). Previously relied on Ray's transitive
inclusion, which left the version unspecified.

---

## 6. Local Deployment

### 6.1 Prerequisites

- NVIDIA driver matching the GPU(s).
- NVIDIA Container Toolkit configured as the Docker runtime (`nvidia-ctk runtime configure --runtime=docker`).
- Docker Engine with Compose v2 (`docker compose`, not the legacy `docker-compose` binary).
- Disk for model weights under `~/.cache/huggingface` (an 8B FP16 model is ~16 GB).
- A single multi-GPU host is sufficient — only the replica autoscaler (§3.2) is exercisable locally; node autoscaling requires a cloud provider.

### 6.2 Steps

```bash
mkdir inference-server && cd inference-server
# place all files from §5
docker compose up -d
docker compose logs -f ray-head     # watch model load; first run downloads weights + builds image
```

### 6.3 Verification

```bash
# Confirm Ray sees all GPUs on the host:
docker compose exec ray-head ray status

# End-to-end request through the full stack:
curl -X POST http://localhost:4000/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"mistral-7b","messages":[{"role":"user","content":"ping"}]}'
```

### 6.4 Local-specific considerations

- **Adding a GPU**: install the card, confirm with `nvidia-smi` on the host, `docker compose restart ray-head`. No file edit required — Ray re-enumerates devices on restart (§3.3).
- **Boot-time startup**: install a systemd unit with `sudo ./idia service install`. The unit runs `docker compose up -d` via `./idia deploy local --no-wait` after Docker is ready. Docker's `restart: unless-stopped` handles individual container recovery; systemd ensures the stack comes back after `docker compose down` + reboot.
- **No node-level autoscaler locally**: the cluster autoscaler (§3.2) never activates on a fixed box; capacity is bounded by the physical GPUs in the machine.
- **Power/thermal**: sustained inference behaves like sustained training for thermal purposes; verify airflow for multi-hour runs.
- **Dashboard access**: do not map port 8265 to the host. Use `docker compose exec -it ray-head bash` and curl `localhost:8265` from inside the container, or a temporary `ssh -L` tunnel from a machine on the same private network (§9.2).

---

## 7. EC2 Deployment (optional)

The same Docker Compose stack from §5 and §6 runs identically on a GPU EC2
instance. Capacity is bounded by the instance's GPU count — to scale up,
stop the instance, change its type, and restart. No node-level autoscaling
is needed for this path.

**Prerequisites:**
- NVIDIA driver + NVIDIA Container Toolkit on the EC2 instance
- Docker Engine with Compose v2

**Deployment:** Identical to §6.2. Copy the repo, configure `.env`, run
`docker compose up -d`. The same security rules from §9 apply: only port
4000 should be reachable from your network.

**Instance sizing guide:**

| Instance family | GPU | Typical fit |
|---|---|---|
| g5.xlarge | 1× A10G (24GB) | 7–8B models |
| g6.12xlarge | 4× L4 (24GB each) | 13–34B, or multiple 7–8B replicas |
| p4d.24xlarge | 8× A100 (40GB) | 70B-class |

---

## 8. Client Consumption

Clients always target the LiteLLM endpoint, never Ray or vLLM directly. They never observe that autoscaling or cold starts exist.

Issue a virtual key per user/team (never hand out the master key):

```bash
curl -X POST 'http://<host>:4000/key/generate' \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"rpm_limit": 60, "max_budget": 20}'
# → {"key": "sk-12..."}
```

Consume via the OpenAI SDK — no vLLM-, Ray-, or LiteLLM-specific code:

```python
from openai import OpenAI
client = OpenAI(base_url="http://<host>:4000", api_key="sk-12...")
resp = client.chat.completions.create(
    model="mistral-7b",          # = model_name in config.yaml
    messages=[{"role": "user", "content": "Explain PagedAttention in one sentence."}],
    stream=True,
)
for chunk in resp:
    print(chunk.choices[0].delta.content or "", end="")
```

A request landing on a scaled-to-zero model pays the cold-start latency (§13.2) inside this same call — there is no separate "wake up" API.

**Failure modes to handle distinctly:**

| Response | Source | Meaning | Remediation |
|---|---|---|---|
| `429` | LiteLLM | Virtual key exceeded its `rpm_limit`/`max_budget` | Back off; raise the limit if legitimate |
| `5xx` after a delay | Ray/vLLM | Engine overloaded or replica unhealthy | Alert; check replica count and queue depth |
| First-request latency in seconds–minutes | Ray | Expected cold start, not a failure | None — distinguish from a regression in dashboards |

---

## 9. Security & Operational Hardening

### 9.1 Baseline controls

- **Pin every image tag** (`ray`, `litellm`, and any standalone `vllm`) using both a semantic version tag and a SHA256 digest — never `:latest`. LiteLLM had a supply-chain incident (compromised PyPI releases during a window in March 2026); pinning to an immutable version tag is the mitigation. The SHA256 also guards against tag mutation (a tag can be reassigned without changing the version string).
- **Two trust boundaries**: LiteLLM's master key (admin) vs. virtual keys (clients). Neither the master key nor any internal backend credential is ever derivable from a client-facing virtual key.
- **TLS terminates at the edge** (ALB/NLB on AWS, a reverse proxy locally), not inside any container.
- **Only port 4000 is ever reachable externally.** Ray ingress (8000), dashboard (8265), and Client port (10001) stay internal in every deployment target.

### 9.2 [IMPORTANT] Ray Dashboard / Jobs API — a documented, actively exploited risk

Ray's dashboard and Jobs API were **designed without authentication**, on the explicit assumption that the cluster runs inside an already-trusted network. Ray faithfully executes code passed to it and does not distinguish a tuning experiment from a rootkit install or an S3 bucket inspection. Anyone able to reach the associated ports can execute arbitrary code on the cluster. This is not theoretical:

- **CVE-2023-48022** (CVSS 9.8; disputed by Anyscale as "a feature, not a bug") enabled unauthenticated remote code execution via the Jobs API on any internet-reachable Ray dashboard. Researchers found thousands of publicly exposed, compromised Ray servers worldwide — the "ShadowRay" campaign — some compromised for at least seven months.
- A 2026 resurgence ("ShadowRay 2.0") shows the same exposure pattern still exploited at scale, driven by the dashboard's default `0.0.0.0` bind colliding with operators who expose it for convenience.
- **CVE-2026-27482** (fixed in Ray 2.54.0+) allowed unauthenticated denial-of-service via an incomplete browser-request blacklist (it blocked `POST`/`PUT` but not `DELETE`), letting a malicious webpage terminate running Serve applications via DNS rebinding.

**Mandatory mitigations:**

1. Never map the dashboard port (8265), Client port (10001), or Prometheus port (9090) to a host port, on Compose or on the Cluster Launcher. Verify with `docker compose ps` / `docker port` after every deploy.
2. Bind the dashboard to `127.0.0.1`; reach it remotely only via SSH tunnel or a reverse proxy with its own authentication.
3. Ray ≥ 2.52.0 ships built-in token authentication — enable it as a second layer, not a replacement for network isolation.
4. Run Ray ≥ 2.54.0 to close CVE-2026-27482.
5. Treat the cluster like a database with no query authorization: any network path to it is equivalent to root on every node.
6. Grafana (port 3000) is bound to `127.0.0.1` — accessible only from the Docker host, not from external networks.

### 9.3 Ray Serve's ingress has no per-request key

Ray Serve LLM's `OpenAiIngress` does not check a per-request API key by default. This is acceptable **only because** LiteLLM is the sole externally reachable component and Ray's ingress (8000) is never published to the host or public network — the same isolation principle as §9.2. If Ray's ingress is ever exposed directly (e.g. "temporarily" for testing), authentication must be added at a reverse proxy in front of it first.

---

## 10. Monitoring & Observability

### 10.1 What each layer exposes

| Layer | Endpoint | Key signals |
|---|---|---|
| vLLM (inside each replica) | Prometheus format, default-on | `vllm:time_to_first_token_seconds`, `vllm:e2e_request_latency_seconds`, `vllm:gpu_cache_usage_perc`, `vllm:num_preemptions_total`, `vllm:num_requests_waiting` |
| Ray Serve | Built-in Prometheus metrics + Grafana dashboards | replica count per deployment, queue depth, autoscaling events, per-request routing |
| Ray (cluster) | Dashboard (internal-only, §9.2) | node count, GPU utilization per node, autoscaler decisions/logs |
| LiteLLM | Built-in Prometheus integration + spend logs | per-key/team cost, request count, latency, fallback events |

The two most actionable engine signals: `gpu_cache_usage_perc` approaching 1.0 together with rising `num_preemptions_total` means the KV cache is undersized for current load — the engine is evicting and recomputing context, degrading latency before it errors. Ray Serve LLM emits its engine-level metrics through the same Prometheus endpoint as Ray's cluster metrics, so one scrape config covers both.

### 10.2 Prometheus + Grafana (Phase 4)

Implemented as two additional services in `docker-compose.yml`, plus a
provisioned Grafana datasource — no manual configuration needed after
`docker compose up`.

**Prometheus** (`prometheus.yml` at the repository root):

```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

scrape_configs:
  - job_name: ray-serve
    static_configs:
      - targets:
          - "ray-head:8080"      # Ray metrics export port — distinct from
                                 # dashboard (8265) and ingress (8000)
        labels:
          layer: orchestration

  - job_name: litellm
    static_configs:
      - targets:
          - "litellm:4000"       # LiteLLM metrics (/metrics) on the same
                                 # port as the API
        labels:
          layer: gateway

  - job_name: dcgm
    static_configs:
      - targets:
          - "dcgm-exporter:9400"   # NVIDIA GPU metrics via DCGM Exporter (T4.2)
        labels:
          layer: gpu
```

Key properties:
- Port 9090 is **not exposed to the host** — Prometheus is queried by
  Grafana on the internal Compose network. For admin access:
  `docker compose exec prometheus sh`.
- Image pinned to `prom/prometheus:v2.55.0` — no `:latest` (§9.1).
- Scrape interval 15s — appropriate for inference servers; engine-level
  metrics (TTFT, cache usage) change at request granularity, not
  sub-second.
- **Data retention:** flags `--storage.tsdb.retention.time=15d` and
  `--storage.tsdb.retention.size=5GB` prevent the `/prometheus` volume from
  growing unbounded (INFRA-01). Configured via the `command` array in
  `docker-compose.yml` rather than the config file, keeping `prometheus.yml`
  focused on scrape configuration.

**Grafana** with automatic provisioning:

```yaml
grafana:
  image: grafana/grafana:11.4.0
  depends_on:
    - prometheus
  ports:
    - "127.0.0.1:3000:3000"     # localhost only — no external access
  volumes:
    - ./grafana/datasources:/etc/grafana/provisioning/datasources
    - ./grafana/dashboards:/etc/grafana/provisioning/dashboards
    - grafana_data:/var/lib/grafana
```

Key properties:
- Bound to `127.0.0.1` — only the Docker host can access the UI
  (§9.3 mitigation #6).
- Image pinned to `grafana/grafana:11.4.0` — no `:latest` (§9.1).
- **Automatic datasource provisioning**: `grafana/datasources/datasource.yml`
  configures Prometheus as the default datasource pointing to
  `http://prometheus:9090` — no manual setup.
- **Provisioned dashboards**: `grafana/dashboards/vllm-dashboard.json` is the
  official vLLM dashboard (grafana.com/dashboards/25043), versioned alongside
  the pinned vLLM engine. Dashboards are pinned to specific Grafana versions
  to prevent format drift. Additional dashboards can be added to this directory
  for automatic provisioning.

**Version alignment:** Dashboard JSONs must match the deployed Grafana version
(`grafana/grafana:11.4.0`). If upgrading Grafana, re-download the official
dashboards matching the new version. The vLLM dashboard targets the
`vllm==0.22.0` metrics schema (bundled by `ray[serve,llm]==2.56.0`).

**Accessing Grafana:**

```bash
# Set up a tunnel if needed (from your laptop to the host):
ssh -L 3000:localhost:3000 user@host
# Then open:
open http://localhost:3000
# Default credentials: admin / admin (change on first login)
```

### 10.3 Recommended alerts

| Alert | Condition | Why |
|---|---|---|
| KV-cache saturation | `vllm:gpu_cache_usage_perc > 0.95` for 5m | Preemption/recompute imminent |
| Replica ceiling reached | deployment at `max_replicas` for >10m | The `autoscaling_config` ceiling, not GPU capacity, is the bottleneck — raise it or investigate demand |
| Cluster at `max_workers` | autoscaler logs show node count pinned at ceiling | Bounded by physical GPU count on the local host |
| Cold-start latency spike | p99 TTFT spikes correlated with a scale-up-from-zero event | Expected; distinguish from a genuine regression |
| Dashboard reachable externally | any external hit on 8265/10001 | Should be impossible per §9.2 — treat as an incident, not a warning |

### 10.4 Request-level tracing

For debugging a specific slow request rather than aggregate trends, vLLM supports OpenTelemetry tracing via `--otlp-traces-endpoint` — complementary to, not a replacement for, the Prometheus path above.

---

## 11. Testing & Validation

Testing an inference server spans four layers, each with distinct
infrastructure requirements and failure modes.

### 11.1 Test categories

| Category | Scope | Infrastructure | When to run | Phase |
|----------|-------|---------------|-------------|-------|
| **docs** | File structure, markdown headers, governance sections | None (`pytest`) | Every commit | 1 |
| **config** | YAML schema validation for every config artifact | PyYAML | Every commit | 1 |
| **integration** | Docker build, `docker compose up`, GPU detection, E2E inference, autoscaling; unit tests for `render_config.py` (env var substitution, YAML validation) | Docker + NVIDIA GPU for full suite; unit component runs with `pytest` only | Before release | 2 |
| **security** | Port isolation (`:8000`, `:8265` unreachable externally), image pinning (no `:latest`), trust boundaries, dashboard binding | YAML/config-level checks run with `pytest` only; network-level checks require Docker | Before release | 2 |

### 11.2 Test location and execution

Tests live in `tests/` and use **pytest 8.x** with shared fixtures
from `tests/conftest.py`. Category markers (`@pytest.mark.docs`,
`@pytest.mark.config`, `@pytest.mark.integration`, `@pytest.mark.security`)
allow selective execution.

```bash
# Quick validation — zero infrastructure required
pip install pytest pyyaml
pytest -m "docs or config" -v

# Full suite (requires Docker + GPU for integration/security tests)
pytest -v
```

### 11.3 Config schema validation

Every YAML artifact in this repository is validated against structural
expectations derived from this architecture document. Tests skip
gracefully when a future-phase file does not yet exist rather than
failing. See `tests/test_config_schemas.py` for the full mapping
between each config file and its assertions.

### 11.4 Skip policy

Tests that depend on files or infrastructure from later phases use
`pytest.skip()` with an explanatory message. This guarantees the
test suite passes cleanly at every phase, even when only a subset of
artifacts exist.

### 11.5 Test files and what they cover

| File | Phase | Marker | Key tests |
|------|-------|--------|-----------|
| `tests/test_docs.py` | 1 | `docs` | Required file existence, markdown headers, governance sections, version footer |
| `tests/test_config_schemas.py` | 1, 4 | `config` | YAML schema validation for `serve_config.yaml`, `docker-compose.yml`, `config.yaml`, `prometheus.yml`, `.env.example`; Grafana datasource provisioning config |
| `tests/test_integration.py` | 2 | `integration` | `render_config.py` env var substitution (required/optional), dry-run mode, error paths; Compose consistency (build source, image pinning, env var propagation) |
| `tests/test_security.py` | 2, 4 | `security` | Port isolation (only 4000 externally accessible), image pinning (no `:latest`), trust boundaries (master key declared), dashboard binding. Phase 4: Prometheus port (9090) not published, Grafana bound to 127.0.0.1 |
| `tests/test_deploy_dry_run.py` | 1 | `config` | Dry-run validation: `render_config.py --dry-run`, `.env.example` schema, `./idia` CLI wrapper, `--no-wait` flag, service/setup subcommands |

| `tests/test_contract.py` | 5 | (none) | LiteLLM API contract tests via mock HTTP server: model not found, missing auth, invalid messages, response format |

### 11.6 Simulated integration testing (Mac/non-GPU environments)

Because integration and security tests require Docker + NVIDIA GPU for
full validation, a subset of tests exercise the code paths and config
structures without real infrastructure:

- `TestRenderConfig` calls `render_config.render()` — a pure function that
  substitutes env var placeholders and validates YAML output without
  launching any container.
- `TestRenderConfigErrors` tests error paths (missing required env vars,
  invalid YAML templates) via `--dry-run` flag and direct function calls.
- `TestComposeConsistency` validates `docker-compose.yml` structure
  (build context, image tags, env var lists) by parsing YAML only.
- `TestPortIsolation` verifies that only port 4000 is accessible externally
  (127.0.0.1:3000 is permitted for Grafana) by inspecting the YAML, not
  by running containers.
- `TestPrometheusConfig` (Phase 4) validates the `prometheus.yml` structure:
  scrape interval, target addresses (ray-head:8080, litellm:4000), and the
  absence of Prometheus-level alert rules (delegated to Grafana).
- `TestGrafanaDatasourceConfig` (Phase 4) validates the Grafana datasource
  provisioning YAML: URL points to `http://prometheus:9090`, access is
  `proxy`, datasource is Prometheus and set as default.
- `TestMonitoringPortIsolation` (Phase 4) verifies that Prometheus (9090)
  is not published in any `ports:` section and that Grafana (3000) is
  bound to `127.0.0.1` only.

Tests that genuinely require GPU (`docker compose build`, `ray status`,
E2E inference) are documented and intended for pre-release validation on
GPU-equipped hardware.

For the complete testing reference, see `AGENTS.md` (Testing Strategy).

---

## 12. Fine-Tuning & Multi-Model Serving

### 12.1 Why LoRA

`y = xW + xAB` — frozen base `W`, small trained low-rank matrices `A`,`B`. The adapter is typically under 1% of the base model's size, which is what makes both training (consumer-GPU feasible) and serving (many adapters per GPU) practical. Serving 100 rank-16 adapters on one 8B base costs roughly `16 GB + 100 × 0.06 GB` ≈ 22 GB — versus ~1.6 TB if each adapter required a full model copy. That ratio is the entire economic argument for multi-LoRA over per-customer dedicated models.

### 12.2 Training framework choice

| Framework | Strength | Best fit |
|---|---|---|
| **Unsloth** | Fastest, lowest VRAM, single-GPU focus | Limited hardware, fast iteration |
| **Axolotl** | Config-driven, strong multi-GPU support | Production training, team handoff |
| **TRL** | Reference RLHF/GRPO implementation | When the objective itself, not speed, is the hard part |
| **LLaMA-Factory** | GUI-first, broad model coverage | Fastest path for non-specialists |

[DEBATED]: relative speed/VRAM benchmarks vary by source; the directional ranking is consistent, exact multipliers are not. All four emit standard HuggingFace-format adapters, cross-compatible without conversion.

### 12.3 Serving adapters via multiplexing

Ray Serve LLM's model multiplexing loads adapters on demand and evicts them LRU when GPU memory is needed for a different one, without restarting the engine — adapters sharing one base model swap in sub-second time, far cheaper than a full cold start.

[SPECULATIVE — verify field names against current Ray Serve LLM docs before deploying]: the general pattern is to declare a `lora_config` alongside the base `LLMConfig` rather than a flat list of adapter paths. This module evolves between minor versions; confirm the production syntax against Ray's LoRA-serving guide rather than relying on this document.

### 12.4 Exposing a fine-tuned variant

A fine-tuned variant becomes a second entry in `llm_configs` (or a multiplexed adapter under the same base entry) plus a second `model_name` in LiteLLM's `config.yaml` pointing at the same Ray ingress. Clients select it by changing the `model` field — same endpoint, same key, same SDK call shape as §8.

---

## 13. Scaling

### 13.1 The two levers

- **More concurrent capacity for a model already running** → raise `max_replicas` in that model's `autoscaling_config` (§4.2). No infrastructure change.
- **More physical GPU capacity** → install hardware or upgrade the EC2 instance type, then restart the container (§6.4).

### 13.2 Scale-to-zero and automatic wake-on-request

`min_replicas: 0` frees a model's GPU allocation entirely while idle. The mechanism is fully automatic: the first request after an idle period triggers Ray Serve to provision a replica and load the model into VRAM, then serves that request once loading completes — no manual restart, no standing always-on cost, no separate "wake up" call. This applies uniformly regardless of model size; there is no flag that disables it for larger deployments.

The only cost is cold-start duration on that first request, which scales with how much weight must load and across how many GPUs — seconds for a single-GPU model, minutes for a model spanning a full multi-GPU node. Whether that wait is acceptable is a product decision, not a constraint the system imposes: if idle-GPU savings matter more than first-request latency, `min_replicas: 0` is correct for any deployment, including multi-GPU ones.

To enable: set `min_replicas: 0` in the deployment's `autoscaling_config`. No other configuration changes with model size.

### 13.3 On-demand model loading — two distinct cases

| Case | Mechanism | Where configured |
|---|---|---|
| Same model, more concurrent capacity | Replica autoscaler adds copies of the *same* deployment | `autoscaling_config` |
| Different model/variant not currently resident | Separate `LLMConfig` with its own `min_replicas: 0` (full cold start), or LoRA multiplexing with LRU eviction (sub-second swap between adapters sharing one base) | `llm_configs` list / multiplexing config (§12.3) |

### 13.4 Honest limits

- Capacity is bounded by `max_replicas` per model and physical GPU count on the host. These are deliberately finite — operator-chosen guardrails against runaway cost, not limitations to remove.
- Provisioning additional replicas is bounded by GPU availability on the local host. The replica autoscaler reacts quickly because it reuses already-running containers when spare VRAM exists.
- On EC2, a service-quota increase for the GPU instance family is required before deploying GPU instances.

### 13.5 The zero-usage floor cost

Scale-to-zero removes GPU cost while a deployment is idle, but not all cost. Two things persist regardless of traffic:

1. **The head node.** In local-only deployments, the Ray head runs on the same host as GPU workers — there is no separate head node cost. If deploying to EC2, the instance itself is the single node running both the Ray head and all GPU workers.
2. **Persisted model storage.** Weights cached on EBS so a cold start reads from local disk instead of re-downloading from HuggingFace on every wake-up. Billed by GB-month whether or not the model is ever loaded.

| Component | Cost driver | Approx. monthly cost |
|---|---|---|
| Head node (`m5.large`, CPU-only, always on) | $0.096/hr × 730h | ~$70 |
| Model storage (EBS gp3, $0.08/GB-month), per cached model | weight size on disk | ~$2 for a 24GB model, up to ~$60 for a 750GB-class MoE |
| GPU workers | `min_workers: 0`, no replica running | $0 |

The floor with zero requests, ever, is the head node plus staged model storage — on the order of $70–140/month depending on how many model sizes are pre-cached, never $0. This is the price of being ready to respond instantly to the next request, not the price of serving anyone.

---

## 14. Cost & Capacity Planning

All figures are directional and in USD; verify against the AWS console before budgeting. Two inputs drive everything: which **model class** is served (it sets the per-replica hardware) and how many **concurrent requests** must be handled at peak (it sets the replica count).

### 14.1 Model classes

The parameter count, not the model name, determines cost. Three classes bracket the realistic range of open-weight coding models:

| Class | Anchor (community open-weight, mid-2026) | Hardware shape | Why |
|---|---|---|---|
| **Small** | 24B-class dense (e.g. Devstral-Small-2) | 1 GPU (g6.xlarge) | Fits one 24GB card in FP8; the most accessible self-hosted tier |
| **Medium** | ~70B dense | slice of an 8×A100 node (p4d.24xlarge) | Needs >1 GPU or aggressive quantization; A100/H100 sold only as full 8-GPU nodes |
| **Large** | 700B+ MoE (e.g. GLM-class, ~40B active) | full 8×H200 node (p5e.48xlarge) | Hundreds of GB of weights; fits only across a whole node |

[ESTABLISHED]: these model/hardware classes and AWS instance prices are confirmed by multiple current sources. [SPECULATIVE]: the concurrent-requests-per-replica figures below are estimates (throughput ÷ per-user demand ÷ duty cycle), not published benchmarks for these specific models — use them as a starting point to validate against real load, not as final numbers.

### 14.2 Per-replica unit economics

| Class | Instance | VRAM need | Concurrent req/replica [SPECULATIVE] | $/hr | $/mo at 24/7 |
|---|---|---|---|---|---|
| Small | g6.xlarge (1× L4 24GB) | ~24GB FP8 | ~18 | $0.80 | ~$587 |
| Medium | 1/8 of p4d.24xlarge (1× A100 40GB) | ~35GB INT4 | ~30 | $4.10 equiv. | ~$2,990 equiv. |
| Large | full p5e.48xlarge (8× H200) | ~754GB FP8 | ~200 | $47.76 | ~$34,865 |

The Medium and Large rows expose a **step-function cost shape**: A100/H100/H200 are sold only as full 8-GPU nodes. You cannot rent "one-eighth of a p4d" — the node bills whole whether one GPU or all eight are in use. Small does not have this problem (g6.xlarge is one purchasable GPU), so it scales smoothly.

### 14.3 Monthly cost vs. concurrent users (24/7, always-on)

| Peak concurrent | Small (nodes / $) | Medium (full p4d nodes / $) | Large (full p5e nodes / $) |
|---|---|---|---|
| 10 | 1 / $587 | 1 / $23,902 | 1 / $34,865 |
| 50 | 3 / $1,761 | 1 / $23,902 | 1 / $34,865 |
| 200 | 12 / $7,044 | 1 (at capacity) / $23,902 | 1 (at capacity) / $34,865 |
| 1,000 | 56 / $32,872 | 5 / $119,510 | 5 / $174,325 |

Reading the table:
- **Large has a brutal fixed floor** ($34,865/mo even for 10 users) because the MoE needs a whole 8-GPU node just to load. It is only defensible if its per-task quality justifies ~5–7× Small's per-user cost at every scale.
- **Medium is worst at small scale** ($23,902/mo for 10 users): paying for a full 8×A100 node to serve a handful of people is the least efficient point in the matrix. It becomes competitive only once the node fills (~200 concurrent), because node cost is fixed whether 1 or 8 GPUs are busy.
- **Small scales almost linearly** — no node step function — and converges to ~$5/user/mo at any reasonable scale.

### 14.4 Cost per registered user (assuming 15% peak concurrency)

Peak concurrency ≈ 15% of registered users is a reasonable placeholder for an internal dev tool — substitute real telemetry (§10) when available.

| Peak concurrent | ≈ registered users | Small | Medium | Large |
|---|---|---|---|---|
| 10 | ~67 | $8.76 | $356.75 | $520.37 |
| 50 | ~333 | $5.29 | $71.78 | $104.70 |
| 200 | ~1,333 | $5.28 | $17.93 | $26.16 |
| 1,000 | ~6,667 | $4.93 | $17.93 | $26.15 |

Per-user cost falls with scale in every class, but the gap between classes does not close: Large stays ~5× Small per user even at 1,000 concurrent.

### 14.5 The scale-to-zero effect — paying only for the hours in use

The tables above assume always-on. Under scale-to-zero (§13.2), cost is proportional to **duty cycle** — the fraction of time a replica is actually up — plus the fixed floor (§13.5). The key dynamic: the more users, the less this saves, because with enough traffic something is almost always running, collapsing back toward always-on.

| Peak concurrent | Approx. duty cycle | Small | Medium | Large |
|---|---|---|---|---|
| 10 | ~25% | ~$150 + floor | ~$6,000 + floor | ~$8,700 + floor |
| 50 | ~50% | ~$880 + floor | ~$12,000 + floor | ~$17,400 + floor |
| 200 | ~80% | ~$5,600 + floor | ~$19,100 + floor | ~$27,900 + floor |
| 1,000 | ~98% | ~$32,200 + floor | ~$117,000 + floor | ~$170,800 + floor |

Duty-cycle percentages are placeholders for spread-out-but-bursty usage; replace with measured data. "+ floor" is the §13.5 zero-usage cost (~$70–140/mo), which does not disappear.

Implication: scale-to-zero is most valuable at low, sparse utilization — exactly where always-on is most wasteful. At high utilization it converges to the §14.3 numbers. For the Large class, sparse usage saves the most dollars but pays the longest cold start (minutes) on the first request after idle; whether that latency is acceptable is the §13.2 product decision.

---

## 15. Troubleshooting Reference

| Symptom | Likely cause | Fix |
|---|---|---|
| `ray status` shows 0 GPUs in the container | NVIDIA Container Toolkit misconfigured, or `count: all` omitted from the Compose `deploy` block | Verify with `docker compose exec ray-head nvidia-smi` first |
| LiteLLM `502`/timeout reaching Ray | Wrong `api_base` — `localhost` instead of the service name `ray-head` | Use `http://ray-head:8000/v1` |
| First request after deploy hangs for minutes | Expected cold start (§13.2) downloading weights, not a hang | Watch `docker compose logs -f ray-head` for download progress |
| `prometheus.yml: not a directory` on `up` | File didn't exist before first run; Docker auto-created a directory | `rm -rf prometheus.yml`, create the actual file, retry |
| Slow under load, no errors | KV-cache thrashing (§10.1) | Check `gpu_cache_usage_perc`/preemptions; lower `max_model_len` or add capacity |
| Replica count stuck at `max_replicas` | Ceiling reached, not a bug (§10.3) | Raise `max_replicas`, or add more physical GPUs if the host is full |
| External port scanner hits on 8265 | Dashboard leaked to a public interface — active incident, not a slow-fix misconfiguration | Drop the published port immediately, rotate credentials Ray could reach, review §9.2 |
| `429` from LiteLLM | Virtual key budget/RPM hit | Expected; raise the limit if legitimate, else investigate the caller |
| LoRA request returns base-model output | Multiplexing config not matching the adapter's declared name | Confirm against current Ray Serve LLM docs (§12.3) — config surface changes between minor versions |

---

## 16. Document Evolution Contract

### 16.1 Principles

1. ARCHITECTURE.md and the code evolve together — never one without the other.
2. Code is the source of truth; the document is the map. If they disagree, code
   prevails, but the document must be corrected immediately.
3. Every structural change must be reflected in the document before merge.

### 16.2 SYNC-REQUIRED triggers

A change in any of the following *requires* an update to this document:

- `Dockerfile.ray` — base image, dependencies, entrypoint
- `serve_config.yaml` — models, autoscaling, `engine_kwargs`
- `docker-compose.yml` — services, ports, networks, volumes, GPU config
- `config.yaml` — LiteLLM routing, model list, health checks
- `prometheus.yml` — scrape targets, alert rules
- Any test file in `tests/` that introduces a new test category (docs, config,
  integration, security)
- Port mappings, network topology, security perimeters
- Model loading strategy or GPU placement logic

### 16.3 Minor update

Version bump, parameter tweak, new env var:
- Edit the affected section only.
- No full review required.
- Update the footer with date and sections changed.

### 16.4 Major update

New tier, new deployment target, architectural pattern change:
- Full document review required.
- Superseded sections marked `[DEPRECATED — see section X]`.
- Requires human approval before merge.

### 16.5 Desync prevention

- Never merge code without its corresponding update to this document.
- Every implementation task that affects the architecture declares:
  `[UPDATES ARCHITECTURE.md — section X]` in its plan.
- If code and ARCHITECTURE.md disagree, the document is updated in the same
  PR/commit — never deferred.

### 16.6 ADR.md — Decision Records

As decisões arquiteturais mais importantes são registradas em
[`docs/ADR.md`](ADR.md) com o formato:
- **ADR-[N]**: título, data, fase de origem, status
- **Contexto**: problema que motivou a decisão
- **Decisão**: o que foi decidido e por que
- **Alternativa descartada**: opção(ões) rejeitada(s) e justificativa
- **Consequências**: efeitos colaterais (positivos e negativos)

A criação de um novo ADR é obrigatória quando uma decisão arquitetural
envolve trade-offs significativos entre múltiplas alternativas viáveis.

### 16.7 Structural Change History

| Date | Change | Reason |
|------|--------|--------|
| 2026-06-28 | Document created | Initial architecture specification |
| 2026-06-28 | Added §11 Testing & Validation; renumbered §11–§15 to §12–§16; added §16 Document Evolution Contract | Living document governance + test suite |
| 2026-06-28 | Phase 2: Added entrypoint script (render_config.py), expanded .env vars with table, updated Dockerfile CMD, serve_config placeholders, LiteLLM config, integration/security tests, new §5.6 Entrypoint Script | Build Core implementation |
| 2026-06-28 | Phase 3: Updated §7.3 (pre-render workflow for cluster.yaml — decision record), expanded §7.2 (step-by-step EC2 + Compose guide with security group table), added §7.3 deploy automation script reference, extended test tables in §11 with Phase 3 tests (TestClusterYaml extended, TestClusterSecurity); new Governance & Maintainability Axioms in AGENTS.md (Decision Closure, Architecture Feedback Loop, Traceability Axiom) | AWS Deployment implementation |
| 2026-06-28 | Phase 5: Updated §16 (added §16.6 ADR.md decision records); created `docs/ADR.md` with 8 ADRs (phases 1-5); updated `LICENSE` (Apache 2.0); added cross-doc consistency tests (TestReadmeDirectoryTree, TestADRValidation); updated §18 footer | Final Documentation — revision + handoff |
| 2026-06-28 | Audit remediation — 23 fixes: (§5.2 Dockerfile: vLLM pinned 0.5.4); (§5.4 Compose: HF named volume, Grafana password, health checks, memory limits, Prometheus retention, shm override); (§5.6 entrypoint: schema validation, YAML escape, deterministic path, dependency declaration); Config: SEC-01/SEC-04 fixed; Deploy: SEC-02/SEC-10/BUG-04/BUG-05 fixed; AGENTS.md: 6 new Code Quality Axioms; Tests: TestRenderSchemaErrors added + type validation | Audit response (2026-06-28) |
| 2026-06-28 | Structural audit remediation — 16 findings: (§5.3 serve_config: multi-model via ##LLM_CONFIGS## marker, min_replicas:0); (§5.4 Compose: multi-model env vars); (§5.6 entrypoint: multi-model MODELS_COUNT support); (§7.3 cluster.yaml: idle_timeout_minutes, run_options for HF_TOKEN, vLLM pin 0.5.4); (§7.6 budget protection); (§10.3 dashboards: provisioned vLLM dashboard); Config: rate limiting tiers, multi-model routing; AGENTS.md: multi-model docs; Tests: multi-model render tests (2 new) | Structural audit (2026-06-28) |
| 2026-06-28 | Tier 4 — VRAM budget, health check, DCGM, scripts, contract tests: (§5.3 serve_config: health_check_period_s/timeout_s); (§5.4 Compose: dcgm-exporter with gpu profile); (§5.5 .env: GPU_COUNT, GPU_VRAM_GB); (§5.6 entrypoint: VRAM budget validation); (§7.3 scripts: create_security_groups.sh, cache_models.sh, smoke_test.sh, create_user.sh, deploy multi-model); (§10.2 prometheus: dcgm scrape target); Tests: contract tests (test_contract.py), VRAM schema tests (4 new), health check assertions; AGENTS.md: contract test category | Tier 4 implementation (2026-06-28) |

---

## 17. References

- vLLM — Docker deployment & metrics: https://docs.vllm.ai/en/stable/deployment/docker/, https://docs.vllm.ai/en/stable/design/metrics/
- vLLM — Parallelism & scaling: https://docs.vllm.ai/en/latest/serving/parallelism_scaling/
- Ray Serve LLM — architecture & serving guide: https://docs.ray.io/en/latest/serve/llm/index.html, https://docs.ray.io/en/latest/serve/llm/architecture/overview.html
- Ray — KubeRay LLM example (`autoscaling_config` reference): https://docs.ray.io/en/latest/cluster/kubernetes/examples/rayserve-llm-example.html
- Ray — Cluster YAML / AWS autoscaler reference: https://docs.ray.io/en/latest/cluster/vms/references/ray-cluster-configuration.html, https://github.com/ray-project/ray/blob/master/python/ray/autoscaler/aws/example-full.yaml
- Ray — Security guide & token authentication (2.52.0+): https://docs.ray.io/en/latest/ray-security/index.html
- ShadowRay / CVE-2023-48022: https://www.oligo.security/blog/shadowray-attack-ai-workloads-actively-exploited-in-the-wild, https://www.penligent.ai/hackinglabs/the-zombie-vulnerability-a-2026-autopsy-of-cve-2023-48022-and-the-shadowray-2-0-resurgence/
- CVE-2026-27482 (dashboard DELETE bypass): https://www.sentinelone.com/vulnerability-database/cve-2026-27482/
- LiteLLM — Docker quickstart, routing/load balancing, health-check routing: https://docs.litellm.ai/docs/proxy/docker_quick_start, https://docs.litellm.ai/docs/proxy/load_balancing, https://docs.litellm.ai/docs/proxy/health_check_routing
- Fine-tuning framework comparison: https://dev.to/ultraduneai/eval-003-fine-tuning-in-2026-axolotl-vs-unsloth-vs-trl-vs-llama-factory-2ohg
- AWS — EC2 GPU & general-purpose pricing: https://aws.amazon.com/ec2/pricing/on-demand/, https://aws.amazon.com/ebs/pricing/

| 2026-06-29 | Operational automation — unified CLI, dual config render, UX: (§4.3 LiteLLM config rendering — `rendered_litellm_config.yaml`); (§5.4 Compose: litellm now mounts rendered config; healthcheck start_period 60s→300s; DCGM GPU passthrough devices added); (§5.6 entrypoint: `--render-all` flag; `_render_litellm_config()` + `render_litellm_config()` public API; `_write_rendered_files()`); (§7.3 cluster.yaml: SecurityGroupIds, worker_setup_commands documented); Scripts: smoke_test.sh --wait loop; cache_models.sh syntax fix; deploy_cluster.sh multi-model output + SG_ID export; idia unified CLI; .gitignore: rendered_*.yaml excluded; Tests: TestComposeConsistency + TestRenderLiteLLMConfig (7 new); README v2.0 with sections 12 (users) + 13 (multi-model) | Operational automation review — P1 critical fix (LiteLLM env var substitution not performed natively), P3 (multi-model litellm config), A.3 unified CLI, B.x script fixes |
| 2026-06-29 | Operations guide — `docs/DEPLOY.md` created (11 sections: prerequisites, local deploy, multi-model, AWS deploy full walkthrough, user management, monitoring, client integration, maintenance, env var reference, troubleshooting); README v2.1 references DEPLOY.md | New living document: docs/DEPLOY.md (operations guide for maintainers and newcomers) |
| 2026-06-30 | Floci-based AWS test suite — 3 new test modules (test_aws_floci.py, test_aws_scripts.py, test_deploy_dry_run.py); 42 service-level tests (S3 CRUD, EC2 SG lifecycle, IAM role mgmt); 11 script-level tests (run deployment scripts against Floci emulator); 8 dry-run tests (render_config, deploy_cli validation); AGENTS.md Phase Post-5b; ARCHITECTURE.md  expanded  (test coverage table) | Floci integration for offline AWS testing (no real AWS account needed) |
| 2026-07-01 | Ray 2.55.0 → 2.56.0 migration — root cause: `ray[serve,llm]==2.55.0` had `vllm[audio]>=0.18.0` without upper bound; pip resolved `vllm==0.24.0` which requires `transformers>=5.5.3`; `transformers 5.x` broke `standardize_rope_params` for `LlamaConfig` (`AttributeError: max_position_embeddings`); ray 2.56.0 fixes via PR #62464 (`AutoConfig.from_pretrained`) and pins `vllm==0.22.0` (§5.2 Dockerfile.ray: `ray:2.56.0-py311-gpu@sha256:9e0af…`, no separate vllm pin; §7.3 cluster.yaml: `ray:2.56.0-py311-gpu` replaces deprecated `ray-ml`; §9.1 image policy updated; vllm metrics schema reference updated to 0.22.0) | Bug: `AttributeError: 'PreTrainedConfig' object has no attribute 'max_position_embeddings'` in `_infer_supports_vision` on any Llama model with custom `rope_scaling` |
| 2026-07-01 | Operational fixes — (§4.3 LiteLLM config synced: removed stale `background_health_checks`/`health_check_interval`/`enable_health_check_routing`, added `max_parallel_requests` and `require_auth_for_metrics_endpoint: false`; LiteLLM healthcheck in compose uses `/health/liveliness` to bypass auth; Grafana dashboard provider path corrected to `/etc/grafana/provisioning/dashboards`) | Prometheus scrape returned 401 (LiteLLM 1.84+ auth-default change), Docker healthcheck of LiteLLM returned 401 (`/health` requires auth when master_key is set), Grafana dashboards not provisioned (wrong path in dashboard.yml) |
| 2026-07-28 | Simplification — removed AWS deployment (§7 Ray Cluster Launcher/KubeRay/budget protection deleted; kept §7 EC2 + Compose). Deleted cluster.yaml, deploy_cluster.sh, cache_models.sh, create_security_groups.sh, create_iam_node_role.sh, instance-role-policy.json, permission-set-cluster-admin.json, test_aws_floci.py, test_aws_scripts.py. Removed idia CLI deploy aws/cache subcommands. Cleaned pyproject.toml optional deps, tests/conftest.py AWS fixtures, test_config_schemas.py TestClusterYaml, test_security.py TestClusterSecurity, test_docs.py AWS references. DEPLOY.md §5 (Deploy na AWS) removed and sections renumbered. AGENTS.md updated. | Local-only simplification: user only needs Docker Compose; GPU autoscaling works on locally available GPUs; instance upgrade handles more power. |
| 2026-07-28 | Boot-time startup — (§6.4 systemd service doc); new scripts/install_service.sh and scripts/uninstall_service.sh; idia CLI: service install/uninstall/status subcommands, --no-wait flag for deploy local. Systemd unit uses Type=oneshot + RemainAfterExit=yes to survive docker compose down. DEPLOY.md §3.7 added with persistence caveat. | Host reboot did not guarantee stack recovery — missing systemd integration. |

---

*Document version: 2.5 | Last updated: 2026-07-28 | Sections changed: 6.4, 7, Structural Change History*