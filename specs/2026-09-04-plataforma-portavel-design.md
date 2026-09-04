# IDIA Server — de servidor local a plataforma portável

**Data:** 2026-09-04
**Estado:** proposto, aguardando aprovação
**Milestones:** M0 (fechado) · M1 · M2 · M3 · M4 · M5

---

## 1. O problema

Hoje o IDIA Server é um servidor de inferência que sobe num host com GPU via Docker
Compose. Isso funciona, e depois do M0 funciona bem: emite chave virtual por pessoa,
tem interface web, tem provisionamento num comando, tem suíte verde.

O que ele não é — e a documentação afirma que é — é elástico. O README promete que
"o cluster autoscaler solicita uma nova instância EC2", e o mecanismo que faria isso
foi removido em `3b27f1b`. O que sobrou redistribui as GPUs que já estão na máquina.
Num cenário em que **não existe máquina** — que é o cenário real do instituto hoje,
com conta AWS e nenhum servidor físico — isso não redistribui nada.

A consequência prática: não há como colocar este servidor no ar para os usuários do
instituto sem alguém provisionar hardware à mão e mantê-lo ligado pagando GPU ociosa.

## 2. O que fica verdadeiro ao final

Uma pessoa do instituto abre a interface, conversa com um modelo, e o hardware que
respondeu foi provisionado sob demanda e desaparece quando a demanda passa. Quem
opera o servidor não toca em console de nuvem: sobe e desce o ambiente com um comando
versionado.

E o dia em que o instituto comprar um servidor físico, o mesmo artefato roda nele.

## 3. A decisão arquitetural

**Kubernetes com KubeRay é o alvo de produção. Docker Compose passa a ser ambiente de
desenvolvimento.**

A alternativa descartada era manter o Compose como alvo único e escrever provisionamento
de EC2 por conta própria — o que a Fase 3 original tentou e o commit `3b27f1b` removeu.
Ela é descartada de novo pelo mesmo motivo, agora explícito: provisionamento de máquina
escrito à mão é um autoscaler artesanal, e a versão pronta desse problema já existe.

### 3.1 As três camadas de elasticidade

No Kubernetes o autoscaling é empilhado em três níveis, não dois. Confundi-los é a
fonte de erro que o README de hoje já comete ao descrever dois.

| Camada | Quem decide | O que aparece/some | Portável? |
|---|---|---|---|
| Réplicas do modelo | Ray Serve, por profundidade de fila | Processos vLLM | Sim — igual em qualquer lugar |
| Pods do Ray | Autoscaler do KubeRay | Pods worker | Sim — igual em qualquer lugar |
| Nós do cluster | Karpenter (na AWS) | Instâncias EC2 com GPU | **Não** — é código de provedor |

A profundidade de fila é o sinal correto para inferência: utilização de CPU não captura
saturação de GPU, e um autoscaler que observa CPU deixa a fila crescer sem reagir.

### 3.2 A costura de portabilidade

**A fronteira fica entre a segunda e a terceira camada.** Tudo acima dela — o
`RayService`, a configuração do Serve, o LiteLLM, o Open WebUI, o provisionamento de
usuários — é o mesmo artefato em qualquer alvo.

Isso não é escolha de projeto; é imposição do mercado. O Karpenter é a opção certa na
AWS por larga margem (30–60 s para levantar um nó GPU, contra 3–5 min do Cluster
Autoscaler, e consolidação ativa em vez de timeout), e **não suporta GPU fora da AWS**:
o suporte a GCP chegou em Q1/2026 sem GPU, e o de Azure exige rede Cilium.

O ganho é que a costura cai exatamente onde o servidor físico precisa dela. Num servidor
comprado **não existe terceira camada** — o hardware é fixo, e as duas de cima fazem
todo o trabalho. É o que este repositório já faz hoje. Portar para hardware próprio não
é reescrever: é remover a camada de baixo e declarar o cluster com tamanho fixo.

### 3.3 O que isso obriga a registrar

O ADR-003 (pre-render do `cluster.yaml`) e o ADR-004 (escolha da `g5.xlarge`) continuam
marcados como *Accepted* e descrevem um caminho que não existe. Eles passam a
*Superseded* por um ADR novo que explica por que a AWS foi removida e volta diferente.
Reescrevê-los no lugar apagaria justamente a informação cara — que a decisão mudou.

## 4. A ordem do trabalho

Cada milestone destrava o seguinte. A ordem não é preferência: é dependência.

**M0 — Linha de base.** *Fechado em 2026-09-04.* A pilha de cinco PRs entrou na main,
que agora emite chave, sobe com banco e interface, e fecha a suíte em 190 passando com
zero falhas.

**M1 — Portão automático.** Nenhuma mudança entra sem verificação. Isso vem antes do
M2 porque o M2 introduz infraestrutura como código, e infraestrutura sem verificação
automática é a categoria de mudança que mais cara sai quando quebra. O portão local já
existe (`scripts/gate.sh`) e vira esteira; o que falta é o modo estrito (#28) e as
verificações que hoje não existem — segredo e tag móvel (#25).

**M2 — Fundação AWS.** O cluster nasce por código e serve um modelo. A portabilidade é
restrição desta etapa, não milestone futuro: o que entra aqui é o que rodaria no
servidor físico, e a camada de provedor fica isolada desde o primeiro commit.

**M3 — Elasticidade real.** A terceira camada entra: nó GPU aparece sob demanda e some
quando ocioso. Só aqui a promessa da documentação passa a ser medida.

**M4 — Identidade e gestão.** Os tiers valem, as chaves sobrevivem, existe revogação,
expiração e backup. Não depende do M2 tecnicamente, mas depende dele em prioridade:
gerir acesso a um servidor que ninguém alcança não entrega nada.

**M5 — Entrega contínua.** Versão publicada e implantada sem passo manual.

## 5. O que está medido e o que não está

Esta seção existe porque a documentação atual afirma no tom de fato consumado coisas
que ninguém verificou, e isso já custou.

**Medido nesta sessão:**

- A suíte na main: 190 passando, 1 pulado, zero falhas.
- Os cinco PRs formavam pilha linear e mergearam limpos, com rebase descartando os
  commits já aplicados.
- O portão local passa por inteiro na main.
- A conta AWS está configurada em `us-east-1`, perfil `default`.

**Não medido, e a spec não deve ser lida como se fosse:**

- **Cota de GPU na conta AWS.** A sessão SSO estava expirada. Um autoscaler que pede
  nó GPU numa conta sem cota aprovada pede e é recusado, e a recusa aparece como pod
  pendente para sempre. É a primeira coisa a medir antes de o M3 virar plano.
- **Se `enable_auto_tool_choice` chega ao vLLM** — registrado em #27. Só uma GPU responde.
- **Custo.** Nenhuma estimativa aqui foi calculada contra preço real de instância.
- **O comportamento do Open WebUI contra uma versão fixada** — hoje ele roda em tag
  móvel (#25), então nem a versão testada é conhecida.

Nada nesta seção bloqueia o M1. Tudo nela bloqueia o M3.

## 6. Fora de escopo, deliberadamente

- **SSO / identidade institucional.** Decidido: chaves virtuais funcionando de verdade
  primeiro. Retrofitar SSO depois custa menos do que construir sobre uma camada de
  auth que ainda não funciona.
- **Multi-tenant.** O servidor serve o instituto. Se isso mudar, a fronteira de
  isolamento precisa ser repensada antes de qualquer outra coisa — é a decisão mais
  cara de adiar, e está adiada conscientemente.
- **Knowledge bases e camadas de tratamento de dados.** São o trabalho maior que este
  servidor vai receber. Precisam de uma base que funcione e seja implantável antes de
  ter onde aterrissar.

---

## Apêndice — mecanismo

Nada abaixo é necessário para julgar o que está acima.

**Recursos do KubeRay.** Três CRDs: `RayCluster` (cluster cru), `RayJob` (lote),
`RayService` (serving com atualização sem downtime). O alvo é o `RayService`, que
encapsula o cluster Ray e a aplicação Serve num manifesto só, reconciliado pelo operador
e visível a `kubectl get rayservice`. O Ray publica um exemplo oficial dessa topologia
para Ray Serve LLM contra o **Ray 2.56.0** — o mesmo pin que o `Dockerfile.ray` já usa.

**Onde o config de hoje aterrissa.** O `serve_config.yaml` renderizado por
`scripts/render_config.py` descreve `applications` com `llm_configs`, que é a mesma
estrutura que o `RayService` carrega no campo `serveConfigV2`. O renderizador continua
sendo o gerador; muda o destino.

**Autoscaler de nó.** Karpenter usa `NodePool` para declarar famílias de instância,
tipo de capacidade, arquitetura e zonas, e chama a API do EC2 diretamente em vez de
passar por Auto Scaling Group. O EKS Auto Mode é o Karpenter embutido, sem instalação
separada. A consolidação ativa (em vez de timeout) é o que faz diferença de custo em
GPU, onde a hora ociosa é cara.

**GPU fracionada.** No Ray, a alocação fracionada de GPU é abstração de escalonamento,
não fronteira imposta por hardware. Manter o orçamento de VRAM continua sendo
responsabilidade da configuração do vLLM — que é o que o `GPU_MEMORY_UTILIZATION` e a
checagem de residentes em `render_config.py` fazem hoje.

**Fontes.**

- [Serve a Large Language Model using Ray Serve LLM on Kubernetes — Ray 2.56.0](https://docs.ray.io/en/latest/cluster/kubernetes/examples/rayserve-llm-example.html)
- [Deploy on Kubernetes — Ray Serve](https://docs.ray.io/en/latest/serve/production-guide/kubernetes.html)
- [Deploying LLMs on Kubernetes: vLLM, Ray Serve & GPU Scheduling Guide (2026)](https://blog.premai.io/deploying-llms-on-kubernetes-vllm-ray-serve-gpu-scheduling-guide-2026/)
- [Karpenter vs Cluster Autoscaler: 2026 Comparison Guide for EKS Teams](https://scaleops.com/blog/karpenter-vs-cluster-autoscaler/)
- [Karpenter vs Cluster Autoscaler vs KEDA: 2026 Comparison](https://me.aiyu.co.in/blogs/karpenter-vs-cluster-autoscaler-vs-keda-2026-kubernetes-autoscaling-comparison)
