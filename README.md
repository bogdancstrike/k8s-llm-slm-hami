# QSINT AI Platform PoC — Documentație Tehnică

> **Versiune:** 1.0
> **Data:** 2026-05-18
> **Autor:** Bogdan
> **Scop:** PoC GitOps-driven AI platform on-prem, inspirat din AWS re:Invent 2026
> "Building an Internal AI Platform with KRO" — adaptat pentru kubeadm + RTX 3080.

---

## Cuprins

1. [Scop și context](#1-scop-și-context)
2. [Decizii arhitecturale](#2-decizii-arhitecturale)
3. [High-Level Design (HLD)](#3-high-level-design-hld)
4. [Low-Level Design (LLD)](#4-low-level-design-lld)
5. [Componente — detaliat](#5-componente--detaliat)
6. [Observability stack](#6-observability-stack)
7. [Prerequisites](#7-prerequisites)
8. [Tutorial: instalare pas cu pas](#8-tutorial-instalare-pas-cu-pas)
9. [Tutorial: cum adaugi un model nou](#9-tutorial-cum-adaugi-un-model-nou)
10. [Tutorial: cum verifici GPU sharing real](#10-tutorial-cum-verifici-gpu-sharing-real)
11. [Tutorial: cum citești distributed traces în Jaeger](#11-tutorial-cum-citești-distributed-traces-în-jaeger)
12. [Troubleshooting](#12-troubleshooting)
13. [Production hardening checklist](#13-production-hardening-checklist)
14. [Path către prod cu L40S](#14-path-către-prod-cu-l40s)
15. [CPU backend cu llama.cpp](#15-cpu-backend-cu-llamacpp)

---

## Local UI access and credentials

Run the host setup helper once on the workstation where the browser runs:

```bash
sudo ./scripts/update-local-hosts.sh
```

This maps the local MicroK8s ingress hosts to `127.0.0.1`.

| UI / endpoint | URI | Username / email | Password / key |
|---|---|---|---|
| Argo CD | http://argocd.local.ro | `admin` | `XexABNTzC9IMf5S8` |
| GitLab | http://gitlab.local.ro | `root` | `Uq6jF9veWolrYF3svwqyrJwS8cCYp9oeIOJEBrlMD5iiOaq7bh0j5aSZ6gfvz9Am` |
| GitLab MinIO | http://minio.local.ro | `CVoEGEWWNmV6DtFyqCX4ij55w2WPhXuV3HSo5Eu9WqvhFDG1oPUOqQUecFesSd1B` | `NfiiFDSg9vOpVL4YYNrcf0esoxPh5y9dVS3jy11jSTgd37U518BykrLp4KWriJ0l` |
| GitLab KAS | http://kas.local.ro | N/A | N/A |
| Grafana | http://grafana.local.ro | `admin` | `admin` |
| Open WebUI | http://open-webui.local.ro | Create first account in UI | Chosen during first signup |
| Langfuse | http://langfuse.local.ro | Create first account in UI | Chosen during first signup |
| Jaeger | http://jaeger.local.ro | N/A | N/A |
| LiteLLM API / admin | http://litellm.local.ro | N/A | Bearer `sk-litellm-master-change-me` |

Credential retrieval commands:

```bash
# Argo CD admin password
microk8s kubectl -n argocd get secret argocd-initial-admin-secret \
  -o go-template='{{index .data "password" | base64decode}}{{"\n"}}'

# GitLab root password
microk8s kubectl -n gitlab get secret gitlab-initial-root-password \
  -o go-template='{{index .data "password" | base64decode}}{{"\n"}}'

# GitLab MinIO access key and secret key
microk8s kubectl -n gitlab get secret gitlab-minio-secret \
  -o go-template='{{"accesskey="}}{{index .data "accesskey" | base64decode}}{{"\nsecretkey="}}{{index .data "secretkey" | base64decode}}{{"\n"}}'

# Grafana admin password
microk8s kubectl -n observability get secret kube-prom-stack-grafana \
  -o go-template='{{index .data "admin-password" | base64decode}}{{"\n"}}'

# LiteLLM bearer key
microk8s kubectl -n ai-platform get secret litellm-secrets \
  -o go-template='{{index .data "LITELLM_MASTER_KEY" | base64decode}}{{"\n"}}'
```

Notes:

- Open WebUI and Langfuse do not have static chart-defined UI users. The first account created in each UI becomes the initial admin.
- LiteLLM uses bearer-token auth: `Authorization: Bearer <LITELLM_MASTER_KEY>`.
- Jaeger and GitLab KAS have no browser login in this PoC. KAS is the GitLab agent server endpoint, not a human UI.
- PoC credentials are intentionally simple and stored in local Kubernetes secrets. Rotate every value before using this outside the local lab.

---

## 1. Scop și context

### 1.1 Ce face acest PoC

Reproduce experiența de developer din slide-urile AWS pentru on-prem cu un singur GPU consumer (RTX 3080 10GB). Un developer face `git push workloads/new-model.yaml` și în câteva minute modelul e live, scalabil, observabil, accesibil prin chat UI.

Workflow complet:

```
git push                                  ← developer action
   ↓
ArgoCD detectează modificare              ← GitOps sync
   ↓
KRO expandează InferenceEndpoint CR       ← abstraction layer
   ↓
KServe creează Deployment + Service       ← serving infrastructure
   ↓
HAMi scheduler plasează pod pe vGPU       ← GPU sharing
   ↓
vLLM descarcă model din HuggingFace       ← model loading
   ↓
Job înregistrează model în LiteLLM        ← gateway registration
   ↓
Model apare în Open WebUI                 ← user access
```

### 1.2 Ce NU face

- **Producție-ready.** Master keys hardcodate, fără mTLS, single replica pentru servicii critice. Vezi secțiunea [Production hardening checklist](#13-production-hardening-checklist).
- **Distributed inference (multi-GPU tensor parallelism).** PoC-ul rulează single-GPU. Pentru TP=4 pe H200 cluster, vezi [Path către prod cu L40S](#14-path-către-prod-cu-l40s).
- **Custom model training/fine-tuning.** Doar serving.

### 1.3 De ce există

Trei motive principale:

**Învățare/PoC tehnologic.** Validarea că KRO + KServe + HAMi + LiteLLM + Open WebUI + Langfuse + Jaeger funcționează coerent ca stack on-prem. Slide-urile AWS sunt EKS-centric (Karpenter, ACK, etc.) — trebuie demonstrat că logica e portabilă on-prem cu echivalente open-source.

**Foundation pentru QSINT.** Stack-ul ăsta e direct aplicabil pentru QSINT RAG/NER/IOC workloads când ajungi la L40S/H200 hardware. Decizia arhitecturală (KServe vs Ray, HAMi vs MIG, single gateway vs direct routing) trebuie validată empiric înainte de prod.

**Self-service developer experience.** O dată setup-ul ridicat, echipa adaugă modele noi prin commit la `workloads/`, fără să atingă infra. Asta scalează team-ul.

---

## 2. Decizii arhitecturale

Fiecare decizie e justificată cu trade-offs și alternative respinse.

### 2.1 KServe (RawDeployment mode) în loc de Ray Serve

| Aspect | Alegere | Motivare |
|---|---|---|
| Serving runtime | KServe RawDeployment | Single-GPU = no need for Ray's distributed inference. KServe oferă HPA standard + custom ServingRuntime pattern curat. |
| Mode | RawDeployment (no Knative) | vLLM cold-start 60-300s face scale-to-zero impractic. Knative adaugă queue-proxy + Istio dependency. |
| vLLM ServingRuntime | Custom | KServe nu vine cu vLLM built-in (are TGI, Triton, MLServer). Definim noi un ClusterServingRuntime. |

**Alternative respinse:**

- **Ray Serve + KubeRay.** Overkill pentru single-GPU. Necesar doar când faci TP across multiple GPUs sau ai pipeline-uri complexe de modele. Re-evaluabil când treci la H200 multi-GPU.
- **vLLM direct ca Deployment, fără KServe.** Pierzi abstraction layer + autoscaling integration + standard inference protocol.
- **Triton Inference Server.** Mai mature, dar mai complex de configurat pentru OpenAI-compatible API. vLLM e mai simplu pentru LLM-specific use cases.

### 2.2 HAMi pentru GPU sharing

| Aspect | Alegere | Motivare |
|---|---|---|
| GPU sharing | HAMi vRAM partitioning | Singura opțiune cu izolare reală de vRAM pe RTX 3080 (consumer GPU). |

**Alternative respinse:**

- **NVIDIA time-slicing.** Zero izolare de memorie — un pod care alocă 9GB OOM-uiește restul. Nepotrivit pentru inference servere persistente.
- **MIG.** Nu funcționează pe RTX 3080 (A100/H100/L40S+ only). Pentru viitorul L40S, MIG + HAMi hybrid e setup-ul recomandat (vezi secțiunea 14).
- **MPS (Multi-Process Service).** Bună pentru latență dar fără izolare vRAM. Complex de setup în K8s.

### 2.3 KRO pentru abstracție

| Aspect | Alegere | Motivare |
|---|---|---|
| Abstraction layer | KRO ResourceGraphDefinition | CRD first-class. Status aggregation automată. Schema typed. |

**Alternative respinse:**

- **Helm chart cu values.yaml.** Templating, nu CRD. Lipsește controller-loop reconciliation.
- **Crossplane v2.** Mai mature, dar overhead semnificativ pentru un PoC. Re-evaluabil pentru prod.
- **Custom operator în Go (Kubebuilder).** Maximum control, dar săptămâni de dezvoltare în plus.

**Risk acceptat:** KRO e alpha (v1alpha1). API-ul poate schimba. Pentru prod, re-evaluează în 6 luni.

### 2.4 PostgreSQL shared pentru LiteLLM + Langfuse

Un singur instance PostgreSQL cu două baze de date logice. Pattern "Platform DB" din slide-urile AWS.

**Alternative respinse:**

- **PostgreSQL separat per serviciu.** Resource overhead pe homelab. OK pentru prod.
- **SQLite în-process.** Nu suportă multi-replica. LiteLLM HPA-scaled cere shared DB.

### 2.5 LiteLLM ca single gateway

Toate request-urile trec prin LiteLLM, indiferent de backend (vLLM local, OpenAI, Anthropic). Avantaje: budget per team, virtual keys, unified callbacks (Langfuse + OTel), A/B testing prin alias swap.

**Risk acceptat:** SPOF + latency overhead (~10-20ms). Mitigation prin HPA + 2+ replicas în prod.

### 2.6 OTel Collector vs direct-to-Jaeger

OTel Collector centralizat. Best practice OpenTelemetry — clienții emit OTLP la collector, collector routeaza la backend(s).

**Avantaje:**
- Schimbi backend (Tempo, Datadog, Honeycomb) fără să atingi clienții
- `k8sattributes` processor enrich spans cu pod/namespace/node
- Memory limiter previne OOM la spike
- Tail sampling possible (ex: keep 100% errors, 10% successes)

**Alternative respinse:**

- **Direct OTLP la Jaeger.** Tight coupling. Schimbare backend = redeploy peste tot.
- **Jaeger Agent (deprecated).** Modelul vechi pre-OTel.

### 2.7 Jaeger all-in-one pentru PoC

Single pod cu collector + query UI + in-memory storage. Ușor de demonstrat, trace-uri se pierd la restart.

**Pentru prod (QSINT):** Jaeger Production cu Elasticsearch backend, reused ES-ul tău existent QSINT. Sau migrate la Grafana Tempo (mai modern, dar nu există încă în stack-ul tău).

### 2.8 Monorepo GitOps cu două ArgoCD Applications

Repo unic cu `platform/`, `kro-templates/`, `workloads/`. Două Applications: `platform` (infra), `workloads` (modele). Sync waves asigură ordinea (`platform` primul, apoi `workloads`).

**Alternative respinse:**

- **Două repo-uri separate.** Strict separation of concerns, dar overhead de cross-repo coord.
- **App-of-Apps cu ApplicationSet.** Mai elegant la scale, overkill pentru 2 modele PoC. Evolve to this when 10+ models.

---

## 3. High-Level Design (HLD)

### 3.1 Diagrama de ansamblu

```
┌───────────────────────────────────────────────────────────────────────┐
│                      User Plane                                       │
│                                                                       │
│   ┌──────────────────┐     ┌──────────────────┐                       │
│   │   Developer      │     │   End User       │                       │
│   │   git push       │     │   Browser        │                       │
│   └────────┬─────────┘     └────────┬─────────┘                       │
└────────────┼───────────────────────┼──────────────────────────────────┘
             │                       │
             ▼                       ▼
┌───────────────────────────────────────────────────────────────────────┐
│                      GitOps Plane                                     │
│                                                                       │
│   ┌──────────────┐         ┌──────────────────────────────────────┐   │
│   │  GitHub      │◄────────┤  ArgoCD                              │   │
│   │  Repo        │  sync   │  - Application "platform"            │   │
│   │              │         │  - Application "kro-templates"       │   │
│   │  platform/   │         │  - Application "workloads"           │   │
│   │  workloads/  │         └──────────────────────────────────────┘   │
│   └──────────────┘                          │                         │
└─────────────────────────────────────────────┼─────────────────────────┘
                                              │
                                              ▼
┌───────────────────────────────────────────────────────────────────────┐
│                      Control Plane                                    │
│                                                                       │
│   ┌──────────────┐  ┌──────────────┐  ┌──────────────┐                │
│   │  KRO         │  │  KServe      │  │  HAMi        │                │
│   │  Controller  │  │  Controller  │  │  Scheduler   │                │
│   └──────────────┘  └──────────────┘  └──────────────┘                │
│                                                                       │
│                              expands / schedules                      │
└───────────────────────────────────────────────┬───────────────────────┘
                                                │
                                                ▼
┌───────────────────────────────────────────────────────────────────────┐
│                      Data Plane                                       │
│                                                                       │
│   ┌────────────────┐   ┌────────────────┐   ┌──────────────────┐      │
│   │ vLLM Pod       │   │ vLLM Pod       │   │ LiteLLM Proxy    │      │
│   │ gemma-1b       │   │ smollm3-3b     │   │  /v1/models      │      │
│   │ vGPU 5GB       │   │ vGPU 5GB       │   │  /v1/chat/...    │      │
│   └────────────────┘   └────────────────┘   └──────────────────┘      │
│            └───────────────┬───────┘                  │               │
│                            │                          │               │
│                  Physical GPU: RTX 3080 10GB          │               │
│                                                       ▼               │
│   ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐    │
│   │  Open WebUI      │  │  Langfuse        │  │  Jaeger UI       │    │
│   │  (chat)          │  │  (LLM traces)    │  │  (dist. traces)  │    │
│   └──────────────────┘  └──────────────────┘  └──────────────────┘    │
│                                                                       │
│   Supporting:                                                         │
│   ┌──────────────┐  ┌──────────────┐  ┌──────────────────────────┐    │
│   │  PostgreSQL  │  │  Prometheus  │  │  OTel Collector          │    │
│   │  (shared)    │  │  + Grafana   │  │  (OTLP fan-out)          │    │
│   └──────────────┘  └──────────────┘  └──────────────────────────┘    │
└───────────────────────────────────────────────────────────────────────┘
```

### 3.2 Mapping pe namespace-uri Kubernetes

```
┌─────────────────────────────────────────────────────────┐
│  namespace: argocd                                      │
│  └─ ArgoCD itself + Applications                        │
├─────────────────────────────────────────────────────────┤
│  namespace: kserve                                      │
│  └─ KServe controller + webhook                         │
├─────────────────────────────────────────────────────────┤
│  namespace: kro-system                                  │
│  └─ KRO controller + ResourceGraphDefinitions           │
├─────────────────────────────────────────────────────────┤
│  namespace: hami-system                                 │
│  └─ HAMi scheduler + device plugin DaemonSet            │
├─────────────────────────────────────────────────────────┤
│  namespace: inference                                   │
│  ├─ InferenceEndpoint CRs (gemma-1b, smollm3-3b)        │
│  ├─ KServe InferenceServices (auto-created by KRO)      │
│  ├─ vLLM pods (auto-created by KServe)                  │
│  ├─ Registration Jobs (auto-created by KRO)             │
│  └─ Shared model-cache PVC                              │
├─────────────────────────────────────────────────────────┤
│  namespace: ai-platform                                 │
│  ├─ LiteLLM proxy                                       │
│  ├─ Open WebUI                                          │
│  ├─ Langfuse                                            │
│  ├─ PostgreSQL (shared)                                 │
│  ├─ OTel Collector                                      │
│  └─ Jaeger all-in-one                                   │
└─────────────────────────────────────────────────────────┘
```

### 3.3 Data flow — un request de inference complet

```
1. User în Open WebUI: "Explain Kubernetes"
   │
   │  POST /v1/chat/completions
   │  Authorization: Bearer sk-litellm-master-...
   │  { "model": "gemma-1b-fast", "messages": [...] }
   ▼
2. LiteLLM Proxy (ai-platform/litellm:4000)
   │  - Lookup "gemma-1b-fast" în PostgreSQL → găsește vLLM endpoint
   │  - Inject traceparent header (W3C Trace Context)
   │  - Forward la backend
   ▼
3. vLLM Pod (inference/gemma-1b-predictor:8000)
   │  - Extract traceparent → continue span
   │  - Tokenize input
   │  - Inference pe vGPU (5GB slice din RTX 3080)
   │  - Stream tokens înapoi
   ▼
4. LiteLLM Proxy
   │  - Async: emit OTLP span la otel-collector:4317
   │  - Async: emit Langfuse trace
   │  - Return response la Open WebUI
   ▼
5. Open WebUI render în UI

Parallel:
6. OTel Collector
   │  - Receive spans de la LiteLLM + vLLM
   │  - Enrich cu k8sattributes (pod, namespace, node)
   │  - Batch (5s window)
   │  - Forward la Jaeger
   ▼
7. Jaeger
   │  - Index spans
   │  - Available în UI :30686
```

---

## 4. Low-Level Design (LLD)

### 4.1 InferenceEndpoint CRD — expandare KRO

Când developer face `kubectl apply` la:

```yaml
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: gemma-1b
  namespace: inference
spec:
  model: "google/gemma-3-1b-it"
  gpuMemMb: 5000
  ...
```

KRO controller-ul detectează CR-ul și aplică `ResourceGraphDefinition inference-endpoint`. Rezultatul:

```
┌─────────────────────────────────────────────────────────────────┐
│  ResourceGraphDefinition: inference-endpoint                    │
│  generates 2 child resources:                                   │
└─────────────────────────────────────────────────────────────────┘
        │
        ├─ Resource #1: KServe InferenceService
        │    apiVersion: serving.kserve.io/v1beta1
        │    metadata:
        │      name: gemma-1b
        │      annotations:
        │        serving.kserve.io/deploymentMode: RawDeployment
        │        serving.kserve.io/autoscalerClass: hpa
        │    spec:
        │      predictor:
        │        minReplicas: 1
        │        maxReplicas: 1
        │        model:
        │          modelFormat: { name: vllm }
        │          runtime: vllm-runtime
        │          env:
        │            - MODEL_ID: google/gemma-3-1b-it
        │            - SERVED_NAME: gemma-1b
        │            - QUANTIZATION: awq
        │            - MAX_MODEL_LEN: "2048"
        │            - GPU_MEM_UTIL: "0.85"
        │          resources:
        │            limits:
        │              nvidia.com/gpu: 1
        │              nvidia.com/gpumem: "5000"
        │              nvidia.com/gpucores: "50"
        │
        └─ Resource #2: Kubernetes Job (litellm register)
             apiVersion: batch/v1
             kind: Job
             metadata:
               name: gemma-1b-litellm-register
             spec:
               template:
                 spec:
                   containers:
                     - image: curlimages/curl:8.10.1
                       command:
                         - sh
                         - -c
                         - |
                           # Wait for vLLM endpoint readiness
                           # POST /model/new on LiteLLM
```

KServe controller-ul preia InferenceService și creează:

```
InferenceService gemma-1b
   │
   ├─ Deployment: gemma-1b-predictor
   │    spec:
   │      replicas: 1
   │      template:
   │        spec:
   │          schedulerName: hami-scheduler   ← critical pentru HAMi
   │          containers:
   │            - name: kserve-container
   │              image: vllm/vllm-openai:v0.6.3
   │              args: [--model=..., --quantization=awq, ...]
   │              resources:
   │                limits:
   │                  nvidia.com/gpu: 1
   │                  nvidia.com/gpumem: 5000
   │
   ├─ Service: gemma-1b-predictor (ClusterIP :80 → :8000)
   │
   └─ HPA: gemma-1b-predictor
        scaleTargetRef → Deployment gemma-1b-predictor
        minReplicas: 1, maxReplicas: 1
```

### 4.2 HAMi vGPU allocation — flow detaliat

```
1. Pod-ul gemma-1b-predictor e creat cu:
   schedulerName: hami-scheduler
   resources.limits.nvidia.com/gpumem: 5000

2. kube-scheduler vede schedulerName != default → skip

3. hami-scheduler:
   - Listează GPU nodes (label gpu=true)
   - Pentru fiecare GPU pe fiecare node:
     - Calculează vRAM disponibilă (capacity - already allocated)
     - Verifică dacă 5000MB încape
   - Selectează nodul + GPU UUID + slot

4. hami-scheduler annotează pod-ul:
   metadata.annotations:
     hami.io/vgpu-devices-allocated: "GPU-abc-1:5000:50"

5. Kubelet pe nodul ales:
   - Cere nvidia.com/gpu de la HAMi device plugin
   - Device plugin injectează:
     - NVIDIA_VISIBLE_DEVICES=GPU-abc-1
     - LD_PRELOAD=/usr/local/vgpu/libvgpu.so
     - CUDA_DEVICE_MEMORY_LIMIT=5368709120  (5GB)

6. Container pornește vLLM:
   - libvgpu.so se attaching la CUDA driver calls
   - cuMemAlloc(size) → check against CUDA_DEVICE_MEMORY_LIMIT
   - Dacă size + already_alloc > limit → return CUDA_ERROR_OUT_OF_MEMORY
   - Altfel → forward la real CUDA driver
```

### 4.3 Observability flow — un trace end-to-end

```
1. Client request → LiteLLM
   │  HTTP POST /v1/chat/completions
   │
   ▼
2. LiteLLM Python middleware:
   │  - opentelemetry-instrumentation-fastapi creates root span
   │  - span: "POST /v1/chat/completions"
   │    attributes:
   │      http.method=POST
   │      http.target=/v1/chat/completions
   │      llm.model=gemma-1b-fast
   │
   ▼
3. LiteLLM router determine target:
   │  - child span: "litellm.routing"
   │    attributes:
   │      target_url=http://gemma-1b-predictor.inference.svc:80
   │
   ▼
4. LiteLLM HTTP call → vLLM:
   │  - child span: "POST gemma-1b-predictor"
   │  - INJECT W3C traceparent header:
   │    traceparent: 00-{trace_id}-{span_id}-01
   │
   ▼
5. vLLM Python middleware:
   │  - EXTRACT traceparent → become child of LiteLLM span
   │  - root span: "vllm.chat_completion"
   │    attributes:
   │      llm.model=gemma-1b
   │      llm.prompt_tokens=42
   │      llm.completion_tokens=128
   │      llm.gen.first_token_ms=120
   │
   ▼
6. Both LiteLLM and vLLM async export to:
   │  http://otel-collector.ai-platform.svc:4317 (OTLP/gRPC)
   │
   ▼
7. OTel Collector:
   │  - receivers.otlp ← receives spans
   │  - processors.k8sattributes ← enrich:
   │    k8s.pod.name=gemma-1b-predictor-abc
   │    k8s.namespace.name=inference
   │    k8s.node.name=node-gpu-01
   │  - processors.batch ← batches 512 spans / 5s
   │  - exporters.otlp/jaeger → forward
   │
   ▼
8. Jaeger Collector:
   │  - stores spans in-memory
   │  - indexes by service.name, operation, trace_id
   │
   ▼
9. Jaeger Query UI (http://node:30686):
   │  - Search by service "litellm-proxy"
   │  - View trace tree:
   │    litellm-proxy / POST /v1/chat/completions  [340ms]
   │      ├─ litellm-proxy / litellm.routing      [2ms]
   │      └─ litellm-proxy / POST gemma-1b-pred   [336ms]
   │          └─ vllm-inference / vllm.chat_comp  [330ms]
```

### 4.4 LiteLLM model registration sequence

Când KRO Job rulează după ce vLLM e ready:

```
Job: gemma-1b-litellm-register
   │
   1. Wait loop:
   │    for i in 1..120:
   │      curl -sf http://gemma-1b-predictor.inference.svc/v1/models
   │      if success: break
   │      sleep 10
   │
   2. POST către LiteLLM:
   │    curl -X POST http://litellm.ai-platform.svc:4000/model/new \
   │      -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
   │      -d '{
   │        "model_name": "gemma-1b-fast",
   │        "litellm_params": {
   │          "model": "openai/gemma-1b",
   │          "api_base": "http://gemma-1b-predictor.inference.svc/v1",
   │          "api_key": "dummy"
   │        },
   │        "model_info": {
   │          "id": "gemma-1b",
   │          "description": "google/gemma-3-1b-it via KServe + vLLM"
   │        }
   │      }'
   │
   3. LiteLLM:
   │    - Validate request (master key)
   │    - INSERT INTO model_table ...
   │    - Reload internal router config
   │    - Return 200 OK
   │
   4. Job exits 0 → completed
```

---

## 5. Componente — detaliat

### 5.1 ArgoCD

**Rol:** GitOps engine. Watch GitHub repo, reconciliază cluster state cu manifest state.

**Topologie:**

```
ArgoCD Applications:
  1. platform          → sync platform/ subdirectory
                        Sync wave 0 (after CRDs)
                        Auto-sync, prune, self-heal

  2. kro-templates     → sync kro-templates/ subdirectory
                        Sync wave 0 (parallel cu platform)
                        Conține RGDs

  3. workloads         → sync workloads/ subdirectory
                        Sync wave 1 (after platform ready)
                        Conține InferenceEndpoint CRs
```

**Sync waves:** Asigură că InferenceEndpoint CR-urile (wave 1) sunt aplicate doar după ce CRD-urile sunt înregistrate (wave 0).

### 5.2 KRO (Kube Resource Orchestrator)

**Rol:** Definește CRD-uri high-level care expandează în multiple K8s objects.

**Componente:**
- Controller manager (Deployment în `kro-system`)
- CRDs: `ResourceGraphDefinition`

**Cum funcționează:**
1. Aplici un `ResourceGraphDefinition` care declară:
   - Schema CRD-ului nou (ex: `InferenceEndpoint`)
   - Lista de resurse pe care le expandează
   - Mapping între câmpurile CRD-ului și fiecare resursă
2. KRO controller înregistrează noua CRD via Kubernetes API
3. Când cineva creează o instanță (ex: `InferenceEndpoint gemma-1b`), KRO controller-ul detectează și creează toate resursele copil

**Concepte cheie:**
- **Schema** — declararea tipului CRD-ului expus utilizatorului
- **Resources** — lista de K8s objects care vor fi create
- **CEL expressions** (`${schema.spec.foo}`) — substituție de valori din CR la resursele expandate

### 5.3 KServe

**Rol:** Abstracție pentru model serving. Un `InferenceService` = un model deployat.

**Componente:**
- Controller manager
- Webhook (validation + defaulting)
- `ServingRuntime` și `ClusterServingRuntime` CRDs
- `InferenceService` CRD

**Mode RawDeployment:**

```
InferenceService ←─── creates ─── KServe Controller
       │
       ├─ Deployment (managed)
       │    └─ Pods
       ├─ Service (ClusterIP)
       └─ HPA (if autoscaling enabled)
```

**Custom ServingRuntime pentru vLLM:**

Definește cum se rulează un model `modelFormat: vllm`. Containerul, args, env vars, resource defaults, probes. Toate InferenceServices care folosesc `runtime: vllm-runtime` moștenesc această configurație, suprascrise selectiv prin spec-ul fiecărui IS.

### 5.4 HAMi

**Rol:** GPU virtualization software-based. Permite multiple pod-uri să share același GPU fizic cu izolare de vRAM și compute.

**Componente:**

```
hami-system namespace:
  - hami-scheduler (Deployment)
      → custom Kubernetes scheduler
      → only schedules pods cu schedulerName: hami-scheduler

  - hami-scheduler-extender (Deployment)
      → webhook pentru kube-scheduler integration
      → optional, depinde de installation mode

  - hami-device-plugin (DaemonSet)
      → runs pe orice node cu label gpu=true
      → advertises nvidia.com/gpu, gpumem, gpucores
      → injects libvgpu.so în pods via environment

  - hami-webhook (Deployment, optional)
      → mutating webhook
      → auto-adds schedulerName: hami-scheduler la pods care
        cer resurse nvidia.com/gpumem
```

**Resource model:**

| Resource | Unit | Description |
|---|---|---|
| `nvidia.com/gpu` | count | Număr de vGPU logice (1 per pod, default) |
| `nvidia.com/gpumem` | MB | Hard limit pe vRAM |
| `nvidia.com/gpucores` | % | Compute share garantat (soft limit) |

### 5.5 vLLM ClusterServingRuntime

**Definit în** `platform/kserve/vllm-servingruntime.yaml`.

**Caracteristici:**
- Image: `vllm/vllm-openai:v0.6.3`
- OpenAI-compatible API pe port 8000
- AWQ quantization support
- `--enable-prefix-caching` (RadixAttention)
- `--otlp-traces-endpoint` pentru distributed tracing
- `schedulerName: hami-scheduler` (critical!)
- HuggingFace token via Secret reference (pentru gated models)
- Shared PVC pentru model cache

**Args parametrizate** (suprascrise per IS):
```
--model=$(MODEL_ID)
--served-model-name=$(SERVED_NAME)
--quantization=$(QUANTIZATION)
--max-model-len=$(MAX_MODEL_LEN)
--gpu-memory-utilization=$(GPU_MEM_UTIL)
```

### 5.6 LiteLLM Proxy

**Rol:** Unified OpenAI-compatible gateway peste orice backend (local vLLM, OpenAI, Anthropic, Bedrock, Azure).

**Capabilități folosite:**
- Model routing prin alias (`gemma-1b-fast` → `openai/gemma-1b` la endpoint local)
- Master key authentication (`/model/new` admin API)
- PostgreSQL backend (persistent model list)
- Callbacks: Langfuse + OTel
- Prometheus metrics

**Înregistrare model:** Prin POST `/model/new` cu master key. Făcut automat de KRO Job după ce vLLM e ready.

### 5.7 Open WebUI

**Rol:** Chat UI. Conectat la LiteLLM ca backend OpenAI-compatible.

**Configurare cheie:**
```yaml
OPENAI_API_BASE_URL: http://litellm.ai-platform.svc.cluster.local:4000/v1
OPENAI_API_KEY: <litellm-master-key>
```

Open WebUI listează automat modelele din `/v1/models` și le afișează în dropdown.

### 5.8 Langfuse

**Rol:** LLM-specific observability. Spre deosebire de Jaeger (generic distributed tracing), Langfuse e specializat: prompt/completion logging, token usage, cost, evaluation runs.

**Componente:**
- Web UI + API server (Next.js)
- PostgreSQL backend (shared cu LiteLLM)

**Wire-up:** LiteLLM are `langfuse` în `success_callback`. Trimite async (non-blocking) detalii despre fiecare completion.

### 5.9 PostgreSQL (shared)

**Rol:** Storage shared pentru LiteLLM și Langfuse. Pattern "Platform DB".

**Setup:**
- StatefulSet single-replica
- Init script creează baze: `litellm`, `langfuse`
- Credentials în K8s Secret

---

## 6. Observability stack

### 6.1 Triada observabilității

| Pillar | Tool | Ce captează | Acces |
|---|---|---|---|
| **Metrics** | Prometheus + Grafana | Throughput, latency, vRAM, cost | Grafana :3000 (în monitoring ns) |
| **Traces** | OTel Collector + Jaeger | Request flow LiteLLM → vLLM | Jaeger UI :30686 |
| **Logs** | (your existing Loki) | Stdout/stderr containere | Loki/Kibana |
| **LLM-specific** | Langfuse | Prompt/completion details, eval | :30030 |

### 6.2 Metrics — surse și dashboards

**Surse de metrici:**

| Sursă | Endpoint | Cum se scrapează |
|---|---|---|
| vLLM pods | `:8000/metrics` | PodMonitor `vllm-inference-pods` |
| LiteLLM | `litellm:4000/metrics` | ServiceMonitor `litellm` |
| HAMi scheduler | `hami-scheduler:metrics` | ServiceMonitor `hami-scheduler` |
| HAMi device plugin | `hami-device-plugin:metrics` | ServiceMonitor `hami-device-plugin` |
| OTel Collector | `:8888/metrics` + `:8889/metrics` | ServiceMonitor `otel-collector` |
| Jaeger | `:14269/metrics` | ServiceMonitor `jaeger` |

**Grafana dashboards (auto-imported via ConfigMap sidecar):**

1. **QSINT — HAMi vGPU Per-Pod**
   - Total vRAM utilization (RTX 3080)
   - Pods active using vGPU
   - GPU temperature, power
   - vRAM usage per pod (stacked timeseries)
   - Compute % per pod
   - Tabel allocation snapshot

2. **QSINT — vLLM Inference**
   - Active requests, queued requests, KV cache %
   - Token throughput per model
   - TTFT (Time To First Token) p50/p95/p99
   - TPOT (Time Per Output Token) p50/p95
   - KV cache + CPU cache utilization

3. **QSINT — LiteLLM Gateway**
   - Total request rate
   - Error rate (5m)
   - p95 latency
   - Request rate per model
   - Latency percentiles per model
   - Token usage (input + output stacked)
   - Per-model summary table

### 6.3 Distributed tracing flow

```
┌──────────────┐ OTLP/gRPC  ┌──────────────────┐ OTLP/gRPC  ┌─────────┐
│ LiteLLM      │───────────►│ OTel Collector   │───────────►│ Jaeger  │
│ Proxy        │            │                  │            │ all-in- │
└──────────────┘            │  pipelines:      │            │ one     │
                            │   traces:        │            └─────────┘
┌──────────────┐ OTLP/gRPC  │    receivers     │
│ vLLM Pods    │───────────►│      [otlp]      │
│ (gemma-1b)   │            │    processors    │            ┌─────────┐
└──────────────┘            │      memory_lim  │            │ Grafana │
                            │      k8sattribs  │            │  scrape │
┌──────────────┐ OTLP/gRPC  │      resource    │◄───────────┤  via    │
│ vLLM Pods    │───────────►│      batch       │            │  /:8889 │
│ (smollm3-3b) │            │    exporters     │            └─────────┘
└──────────────┘            │      [otlp/jaeg] │
                            │      [prometheus]│
                            │      [debug]     │
                            └──────────────────┘
```

**Span attributes (după k8sattributes processor):**

Fiecare span e îmbogățit cu:
- `k8s.namespace.name`
- `k8s.pod.name`
- `k8s.pod.uid`
- `k8s.deployment.name`
- `k8s.node.name`
- Pod labels: `app`, `serving.kserve.io/inferenceservice`
- Resource: `service.name`, `service.namespace`, `deployment.environment=poc`

### 6.4 Langfuse vs Jaeger — când care

| Use case | Tool |
|---|---|
| "Care a fost prompt-ul exact pe care l-a primit modelul X la 14:32?" | Langfuse |
| "Cum se distribuie latența între network, LiteLLM routing, vLLM prefill, vLLM decode?" | Jaeger |
| "Care request-uri au costat cei mai mulți tokens săptămâna asta?" | Langfuse |
| "De ce request-ul ăsta a durat 5 secunde când majoritatea durează 1s?" | Jaeger (flame graph) |
| "Compară output-ul Gemma vs SmolLM3 pe același prompt." | Langfuse (evaluation runs) |
| "Care e bottleneck-ul: LiteLLM, vLLM, sau networking?" | Jaeger |

Complementare, nu redundante.

---

## 7. Prerequisites

### 7.1 Hardware

- Kubernetes node cu GPU NVIDIA (Ampere+, minim 10GB VRAM)
- Recomandat: 32GB+ RAM pe nodul GPU
- 100GB+ storage pentru model cache

### 7.2 Software pe node-uri

| Component | Versiune testată |
|---|---|
| Kubernetes | 1.28+ |
| NVIDIA Driver | 535+ |
| NVIDIA Container Toolkit | latest |
| containerd | 1.7+ (cu nvidia runtime ca default) |

### 7.3 Cluster components (instalate înainte)

```bash
# 1. ArgoCD
kubectl create namespace argocd
kubectl apply -n argocd -f \
  https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml

# 2. cert-manager (KServe dependency)
helm repo add jetstack https://charts.jetstack.io
helm install cert-manager jetstack/cert-manager \
  --namespace cert-manager --create-namespace \
  --set installCRDs=true

# 3. kube-prometheus-stack (Prometheus + Grafana + operators)
helm repo add prometheus-community \
  https://prometheus-community.github.io/helm-charts
helm install prometheus prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace \
  --set grafana.sidecar.dashboards.enabled=true \
  --set grafana.sidecar.dashboards.searchNamespace=ALL

# 4. NFS provisioner (or any RWX storage)
helm repo add nfs-subdir-external-provisioner \
  https://kubernetes-sigs.github.io/nfs-subdir-external-provisioner/
helm install nfs-client nfs-subdir-external-provisioner/nfs-subdir-external-provisioner \
  --namespace nfs-system --create-namespace \
  --set nfs.server=<your-nfs-server> \
  --set nfs.path=/path/to/nfs/share
```

---

## 8. Tutorial: instalare pas cu pas

### Pas 1: Fork repo & customize

```bash
git clone https://github.com/YOUR_USER/qsint-ai-platform
cd qsint-ai-platform

# Update repo URL în Application manifests
sed -i 's|github.com/bogdancstrike|github.com/YOUR_USER|g' \
  platform/argocd/*.yaml
```

### Pas 2: Schimbă credentials (IMPORTANT)

Edit aceste fișiere cu valori unice:

```bash
# 1. PostgreSQL password
vim platform/postgresql/postgresql.yaml
# Schimbă: POSTGRES_PASSWORD

# 2. LiteLLM master key (TREBUIE să fie identic în 3 locuri)
vim platform/litellm/litellm.yaml          # LITELLM_MASTER_KEY
vim platform/kserve/litellm-secret-mirror.yaml  # LITELLM_MASTER_KEY
vim platform/open-webui/open-webui.yaml    # OPENAI_API_KEY

# 3. Langfuse secrets
vim platform/langfuse/langfuse.yaml
# Schimbă: NEXTAUTH_SECRET, ENCRYPTION_KEY (64 hex chars), SALT

# 4. Open WebUI session key
vim platform/open-webui/open-webui.yaml
# Schimbă: WEBUI_SECRET_KEY

git commit -am "customize credentials"
git push
```

### Pas 3: Pre-create HuggingFace secret

```bash
# Get token din https://huggingface.co/settings/tokens
# Accept Gemma license: https://huggingface.co/google/gemma-3-1b-it

kubectl create namespace inference
kubectl create secret generic huggingface-token \
  -n inference \
  --from-literal=token=hf_YOUR_TOKEN
```

### Pas 4: Label nodul GPU

```bash
kubectl label node <your-gpu-node> gpu=true
```

### Pas 5: Bootstrap ArgoCD Applications

```bash
kubectl apply -k platform/argocd/

# Watch sync
argocd app list
argocd app sync platform
argocd app sync kro-templates
argocd app sync workloads
```

### Pas 6: Verifică stack-ul

```bash
# HAMi
kubectl -n hami-system get pods
kubectl describe node <gpu-node> | grep nvidia.com

# KServe
kubectl -n kserve get pods
kubectl get clusterservingruntime vllm-runtime

# KRO
kubectl -n kro-system get pods
kubectl get resourcegraphdefinitions
kubectl get crd inferenceendpoints.kro.run

# Models
kubectl -n inference get inferenceendpoints
kubectl -n inference get inferenceservices
kubectl -n inference get pods

# Wait pentru pod-uri să fie Ready (poate dura 10-15 min la cold start
# pentru download model + warmup vLLM)
kubectl -n inference wait --for=condition=Ready pod \
  -l serving.kserve.io/inferenceservice=gemma-1b \
  --timeout=900s

# Platform services
kubectl -n ai-platform get pods
```

### Pas 7: Wire-up Langfuse keys

```bash
# 1. Open Langfuse
xdg-open http://<node-ip>:30030

# 2. Sign up (devine admin)
# 3. Create project: qsint-poc
# 4. Settings → API Keys → Create
# 5. Copy public key (pk-lf-...) și secret key (sk-lf-...)

# 6. Patch LiteLLM secret
kubectl -n ai-platform edit secret litellm-secrets
# Update LANGFUSE_PUBLIC_KEY și LANGFUSE_SECRET_KEY (base64-encoded)

# 7. Restart LiteLLM
kubectl -n ai-platform rollout restart deploy/litellm
```

### Pas 8: Test end-to-end

```bash
# Port-forward LiteLLM
kubectl -n ai-platform port-forward svc/litellm 4000:4000 &

# List models
curl http://localhost:4000/v1/models \
  -H "Authorization: Bearer <LITELLM_MASTER_KEY>"

# Send chat completion
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer <LITELLM_MASTER_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemma-1b-fast",
    "messages": [{"role": "user", "content": "Hello!"}],
    "max_tokens": 100
  }'

# Open chat UI
xdg-open http://<node-ip>:30080

# View distributed traces
xdg-open http://<node-ip>:30686

# View LLM-specific traces
xdg-open http://<node-ip>:30030

# View Grafana dashboards
xdg-open http://<node-ip>:<grafana-nodeport>
# Login admin/admin
# Browse Dashboards → QSINT folder
```

---

## 9. Tutorial: cum adaugi un model nou

Exemplu: adaug `Qwen2.5-7B-Instruct-AWQ`.

```bash
# 1. Crează fișier nou
cat > workloads/qwen25-7b.yaml <<'EOF'
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: qwen25-7b
  namespace: inference
spec:
  model: "Qwen/Qwen2.5-7B-Instruct-AWQ"
  servedName: "qwen25-7b"

  # Note: 7B AWQ ≈ 4.2GB weights + ~1.5GB KV cache + 1.5GB overhead = ~7GB
  # Won't fit alongside both existing models on 10GB GPU.
  # Either scale down existing models or use separate GPU node.
  gpuMemMb: 8000
  gpuCorePercent: 80

  quantization: "awq"
  maxModelLen: 4096
  gpuMemUtilization: "0.85"

  cpuRequest: "2"
  cpuLimit: "4"
  memoryRequest: "8Gi"
  memoryLimit: "16Gi"

  minReplicas: 1
  maxReplicas: 1

  litellmAlias: "qwen25-7b-coder"
EOF

# 2. Commit + push
git add workloads/qwen25-7b.yaml
git commit -m "feat: add Qwen2.5-7B model"
git push

# 3. ArgoCD detectează (auto-sync within 3 min, sau force):
argocd app sync workloads

# 4. KRO expandează automat:
kubectl -n inference get inferenceendpoint qwen25-7b -w

# 5. Verifică în LiteLLM după ce KServe IS e Ready:
curl http://localhost:4000/v1/models -H "Authorization: Bearer ..."
# Should list qwen25-7b-coder

# 6. Open WebUI vede modelul automat în dropdown (refresh).
```

---

## 10. Tutorial: cum verifici GPU sharing real

Whole point of HAMi e că două pod-uri share același GPU fizic cu izolare reală.

```bash
# 1. Confirmă ambele pod-uri pe același nod
kubectl -n inference get pods -o wide \
  -l 'serving.kserve.io/inferenceservice in (gemma-1b, smollm3-3b)'

# Output expectat:
# NAME                              READY   STATUS    NODE
# gemma-1b-predictor-xyz            1/1     Running   gpu-node-01
# smollm3-3b-predictor-abc          1/1     Running   gpu-node-01    ← acelasi nod

# 2. Verifică schedulerName
kubectl -n inference get pod gemma-1b-predictor-xyz \
  -o jsonpath='{.spec.schedulerName}'
# Output: hami-scheduler   ← critic!

# 3. Inside pod, nvidia-smi arată DOAR vRAM-ul alocat
kubectl -n inference exec deploy/gemma-1b-predictor -- nvidia-smi
# Memory-Usage: ar trebui < 5000 MiB

# 4. Pe nodul fizic (sau via DCGM exporter), vezi DOUĂ procese
ssh <gpu-node>
nvidia-smi
# Process list ar trebui să arate 2x python (vllm) pe GPU 0
# Total memory used: ~8-10GB (suma celor două vGPU slices)

# 5. În Grafana, deschide "QSINT — HAMi vGPU Per-Pod" dashboard
# Vezi DOUĂ linii distincte în "vRAM Usage per Pod (stacked)"
# Fiecare cu vRAM-ul lor independent

# 6. Stress test pentru a confirma izolarea
# Trimite request-uri concurente la ambele modele:
for i in {1..10}; do
  curl -s http://localhost:4000/v1/chat/completions \
    -H "Authorization: Bearer ..." \
    -d '{"model":"gemma-1b-fast","messages":[{"role":"user","content":"Count to 100"}],"max_tokens":500}' &
  curl -s http://localhost:4000/v1/chat/completions \
    -H "Authorization: Bearer ..." \
    -d '{"model":"smollm3-3b-quality","messages":[{"role":"user","content":"Count to 100"}],"max_tokens":500}' &
done
wait

# Grafana arată ambele modele răspunzând concurent, fără să-și fure resurse
```

---

## 11. Tutorial: cum citești distributed traces în Jaeger

```bash
# 1. Generează un request
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer ..." \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemma-1b-fast",
    "messages": [{"role": "user", "content": "Hello"}],
    "max_tokens": 50
  }'

# 2. Open Jaeger UI
xdg-open http://<node-ip>:30686

# 3. Search:
#    Service: litellm-proxy
#    Operation: POST /v1/chat/completions
#    Lookback: Last 15 minutes
#    Click "Find Traces"

# 4. Click pe un trace → vezi waterfall:
#    litellm-proxy: POST /v1/chat/completions          [340ms total]
#    ├── litellm-proxy: litellm.routing                [2ms]
#    └── litellm-proxy: HTTP POST gemma-1b-predictor   [336ms]
#        └── vllm-inference: vllm.chat_completion      [330ms]
#            ├── vllm.tokenize                          [3ms]
#            ├── vllm.prefill                          [120ms]
#            └── vllm.decode (50 tokens)                [205ms]

# 5. Click pe orice span → vezi attributes:
#    k8s.pod.name=gemma-1b-predictor-xyz
#    k8s.namespace.name=inference
#    k8s.node.name=gpu-node-01
#    llm.model=gemma-1b
#    llm.prompt_tokens=12
#    llm.completion_tokens=50

# 6. Compare traces pentru a identifica outliers:
#    Click "Compare" cu 2 trace IDs
#    Vezi differences în structura span tree
```

---

## 12. Troubleshooting

### Pod-ul nu pornește — `Insufficient nvidia.com/gpumem`

```bash
# Check HAMi allocatable
kubectl describe node <gpu-node> | grep -A 5 Allocatable

# Dacă vezi 0 gpumem, HAMi device plugin nu rulează:
kubectl -n hami-system get pods -o wide
kubectl -n hami-system logs -l app.kubernetes.io/name=hami-device-plugin

# Probleme comune:
# - Nodul nu are label gpu=true
# - NVIDIA driver nu e instalat
# - containerd nu are nvidia runtime
```

### vLLM hangs at startup

```bash
kubectl -n inference logs -f deploy/gemma-1b-predictor

# "OSError: model is gated" → HF token lipsește/invalid
kubectl -n inference get secret huggingface-token -o yaml
# Re-create cu token valid

# "CUDA out of memory" → reduce gpu-memory-utilization
# Edit InferenceEndpoint:
kubectl -n inference edit inferenceendpoint gemma-1b
# spec.gpuMemUtilization: "0.75"

# "Connection timeout HuggingFace" → network issue
# Check egress NetworkPolicy / firewall
```

### Pod e Running dar nu apare în LiteLLM

```bash
# Check registration job
kubectl -n inference get jobs
kubectl -n inference logs job/gemma-1b-litellm-register

# Probleme comune:
# - LITELLM_MASTER_KEY mismatch între litellm-secrets și mirror
# - LiteLLM pod not ready when job ran
# - Network policy blocking

# Re-run job manual:
kubectl -n inference delete job gemma-1b-litellm-register
# Apoi trigger reconcile pe InferenceEndpoint
kubectl -n inference annotate inferenceendpoint gemma-1b \
  kro.run/force-reconcile="$(date +%s)" --overwrite
```

### Jaeger nu arată traces

```bash
# 1. Verifică OTel Collector primește
kubectl -n ai-platform logs deploy/otel-collector | grep -i "received\|trace"

# 2. Verifică LiteLLM emite
kubectl -n ai-platform logs deploy/litellm | grep -i "otel\|trace"

# 3. Test direct cu trace-cli
kubectl -n ai-platform run -it --rm trace-test --image=alpine -- sh
# inside:
# wget -q -O- http://otel-collector:4317/  (should hang — gRPC)

# 4. Check OTel Collector pipeline
kubectl -n ai-platform exec deploy/otel-collector -- \
  cat /etc/otel/collector.yaml | grep -A 5 pipelines
```

### Grafana dashboards lipsesc

```bash
# 1. Verifică ConfigMaps existente
kubectl -n monitoring get cm -l grafana_dashboard=1

# 2. Verifică Grafana sidecar logs
kubectl -n monitoring logs <grafana-pod> -c grafana-sc-dashboard

# 3. Verifică label/namespace correct
# Sidecar caută ConfigMaps cu label `grafana_dashboard: "1"`
# În toate namespace-urile dacă sidecar.searchNamespace=ALL

# 4. Manual import (fallback):
# Copy JSON content din platform/observability/grafana-dashboard-*.json
# Grafana UI → Dashboards → Import → Paste JSON
```

---

## 13. Production hardening checklist

Asta e PoC. Pentru QSINT prod, trebuie addressed:

### Security
- [ ] **Secrets management** — External Secrets Operator + Vault. Eliminate hardcoded keys.
- [ ] **mTLS între servicii** — Istio service mesh strict mode.
- [ ] **NetworkPolicies** — restrictiv. Inference pods accessible doar de la LiteLLM.
- [ ] **RBAC** — minim necesar pentru ServiceAccounts.
- [ ] **Pod Security Standards** — `restricted` nivel pentru workloads (modificat din `privileged` din PoC).
- [ ] **Image scanning** — Trivy/Snyk pe CI.
- [ ] **Egress firewall** — whitelist doar HuggingFace + Anthropic + necessary domains.
- [ ] **Virtual API keys per team** — în loc de master key.
- [ ] **Master key rotation** — automated, plus coordonarea restart-urilor.

### Reliability
- [ ] **HA pentru toate componentele critice:**
  - LiteLLM: HPA min 2, multi-AZ
  - PostgreSQL: CloudNativePG cluster, replication
  - Langfuse: 2+ replicas
  - OTel Collector: HPA
- [ ] **Backup/restore PostgreSQL** — pgBackRest, off-site backups
- [ ] **PodDisruptionBudgets** — pe toate Deployments
- [ ] **Resource requests/limits** — measured, nu guessed
- [ ] **Liveness/readiness/startup probes** — tuned (PoC are valori conservative)

### Observability
- [ ] **Persistent traces** — Jaeger Production cu ES backend (sau Tempo)
- [ ] **Long-term metrics** — Thanos sau Mimir pentru retention >2 săpt
- [ ] **Logs centralized** — Loki cu pipeline-uri pentru LLM-specific log parsing
- [ ] **Alerting** — Alertmanager rules pe error rate, latency, GPU temp, cost
- [ ] **SLO tracking** — TTFT p95 < 500ms, error rate < 1%, etc.

### Model management
- [ ] **Internal model registry** — Harbor OCI artifacts, no HuggingFace runtime dependency
- [ ] **Modelcar pattern** — modelele pre-baked în OCI images, pulled by KServe storage initializer
- [ ] **Canary deploys** — `canaryTrafficPercent` în InferenceService
- [ ] **Model versioning** — semantic versioning, rollback strategy
- [ ] **Re-quantization pipeline** — CI pentru AWQ quantization cu calibration dataset propriu

### GitOps
- [ ] **Sealed Secrets** sau ExternalSecrets pentru secret management în Git
- [ ] **Branch protection** — main branch required reviews
- [ ] **Pre-commit hooks** — yamllint, kubeval, conftest (OPA policies)
- [ ] **Promotion environments** — dev → staging → prod separate ArgoCD instances
- [ ] **ApplicationSets** când număr modele > 10

### Cost
- [ ] **Cost attribution per team** — LiteLLM virtual keys + Langfuse cost tracking
- [ ] **Budget alerts** — Alertmanager rules pe LiteLLM budget metrics
- [ ] **GPU utilization SLOs** — vRAM utilization > 70%, otherwise consolidate
- [ ] **Idle scale-down** — pentru modele cu trafic sporadic

---

## 14. Path către prod cu L40S

### 14.1 Hardware target

Per architectura QSINT plan: cluster de 3-4 noduri, fiecare cu 2-4× L40S 48GB.

### 14.2 Schimbări vs acest PoC

| Componentă | PoC (RTX 3080) | Prod (L40S) |
|---|---|---|
| GPU sharing | HAMi 5GB/pod | MIG `2g.24gb` partitions + HAMi fallback |
| Models | 2× tiny (1B + 3B AWQ) | Real workloads: Qwen 32B, Llama 70B AWQ, embedders |
| Replicas | 1 per model | HPA `min=1, max=N` pe vLLM metrics |
| Storage | Local NFS | Ceph RBD / Longhorn pentru model cache |
| Postgres | Single replica | CloudNativePG HA cluster |
| Jaeger | All-in-one | Production cu ES backend (reuse QSINT ES) |
| ArgoCD | Manual sync | Auto-sync, ApplicationSet pe ` workloads/` dir |
| Secrets | Hardcoded | External Secrets Operator + Vault |
| LiteLLM | 1 replica | HPA 2-5 replicas, Redis cache |

### 14.3 Hybrid MIG + HAMi setup pentru L40S

```
Node: gpu-node-01 (2× L40S)
  ├─ L40S #0 — MIG mode enabled
  │    ├─ MIG 4g.48gb instance #1 → Qwen 32B FP16 (full)
  │    └─ MIG 4g.48gb instance #2 → Llama 70B AWQ (full)
  │
  └─ L40S #1 — non-MIG mode + HAMi
       ├─ HAMi vGPU 12GB → embedder model
       ├─ HAMi vGPU 12GB → reranker model
       ├─ HAMi vGPU 12GB → NER model
       └─ HAMi vGPU 12GB → classification model
```

InferenceServices alege resource type:

```yaml
# Pentru modele mari, MIG partition
spec:
  predictor:
    model:
      resources:
        limits:
          nvidia.com/mig-4g.48gb: 1

# Pentru modele mici, HAMi
spec:
  predictor:
    model:
      resources:
        limits:
          nvidia.com/gpu: 1
          nvidia.com/gpumem: 12000
```

### 14.4 HPA pe vLLM metrics

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: qwen25-7b-hpa
  namespace: inference
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: qwen25-7b-predictor
  minReplicas: 1
  maxReplicas: 5
  metrics:
    # Scale când request queue depth > 5
    - type: Pods
      pods:
        metric:
          name: vllm:num_requests_waiting
        target:
          type: AverageValue
          averageValue: "5"
    # Sau când TTFT p95 > 800ms
    - type: Pods
      pods:
        metric:
          name: vllm:time_to_first_token_seconds_p95
        target:
          type: AverageValue
          averageValue: "800m"
  behavior:
    scaleUp:
      stabilizationWindowSeconds: 30
      policies:
        - type: Percent
          value: 100      # double max per minute
          periodSeconds: 60
    scaleDown:
      stabilizationWindowSeconds: 600  # cooldown 10 min
      policies:
        - type: Percent
          value: 25
          periodSeconds: 60
```

Necesită **Prometheus Adapter** instalat pentru a expune metrice vLLM la K8s metrics API.

---

## 15. CPU backend cu llama.cpp

PoC-ul suportă două backend-uri de inference, alese per workload prin câmpul `spec.backend` din `InferenceEndpoint`:

- **`backend: vllm`** (default) — GPU inference via vLLM ClusterServingRuntime, cu HAMi vGPU partitioning
- **`backend: llamacpp`** — CPU inference via llama.cpp ClusterServingRuntime, fără GPU

### 15.1 De ce un al doilea backend?

| Motiv | Detaliu |
|---|---|
| **Resource efficiency** | Pe noduri fără GPU (sau când GPU-ul e saturated), CPU rămâne o opțiune viabilă pentru modele mici (1B-7B). |
| **Cost optimization** | În prod cu cloud GPU pricing (~$1-3/h pentru L40S), un model cu trafic redus poate rula pe CPU la o fracțiune din cost. |
| **Fallback availability** | Dacă pod-urile GPU sunt down (driver crash, OOM, node failure), LiteLLM poate route-a la model CPU echivalent. |
| **Dev/staging environments** | Echipa de dezvoltare nu mai are nevoie de GPU pentru a testa flows end-to-end. |
| **Background async workloads** | Summarization, batch enrichment, low-priority queues — toate tolerează latențe mai mari. |
| **Energy efficiency** | RTX 3080 consumă ~250W full load. Un Xeon făcând aceeași treabă consumă ~80W. |

### 15.2 Performance așteptat

Pe CPU modern (Xeon Gold, EPYC Rome+, sau Apple Silicon — dacă cumva ai noduri Mac Mini):

| Metric | Qwen2.5-3B Q4_K_M | Comentariu |
|---|---|---|
| Throughput generation | 15-30 tokens/sec | Depinde de AVX-512 / NEON support |
| Throughput prompt processing | 50-100 tokens/sec | Mai lent decât generation paradoxal — overhead per-token |
| TTFT | 100-300ms | Pentru prompt-uri 50-200 tokens |
| RAM | ~3GB resident | weights + KV cache + buffers |
| Cold start | 30-60s | Prima dată (download GGUF). Ulterior 5-10s (mmap din PVC). |
| Optimal threads | 4-8 physical cores | Memory bandwidth bound peste 8 cores |

Pentru comparație:
- **vLLM pe RTX 3080**: ~80-120 tokens/sec generation (3-5x mai rapid)
- **vLLM pe L40S**: ~200-300 tokens/sec generation (10x mai rapid)

### 15.3 Când folosești llama.cpp în loc de vLLM?

**Folosește llama.cpp:**
- Trafic sub 1 req/sec persistent
- Modele sub 7B (peste, performance CPU devine inacceptabilă)
- Workloads tolerante la latență (background jobs, async pipelines)
- Cluster fără GPU disponibil în acel moment
- Cost matters mai mult decât latency
- Modele cu cerințe de quantizare exotice (GGUF Q2_K, Q3_K_S etc — vLLM nu suportă)

**Folosește vLLM:**
- Trafic peste 1 req/sec
- Modele peste 7B (CPU prea lent)
- Workloads user-facing (chat interactive)
- TTFT critic (sub 500ms)
- Throughput maxim necesar
- Quantizări standard (AWQ INT4, GPTQ, FP16, FP8)

### 15.4 Cum funcționează — sub capotă

```
InferenceEndpoint qwen25-3b-cpu (backend: llamacpp)
        │
        │  KRO ResourceGraphDefinition expandează:
        ▼
KServe InferenceService
   ├─ modelFormat.name: gguf       ← determinant pentru runtime match
   ├─ runtime: llamacpp-runtime    ← ClusterServingRuntime
   └─ resources:
        nvidia.com/gpu: "0"        ← KServe ignoră (admission strip dacă "0")
        cpu: 4 (request), 8 (limit)
        memory: 4Gi (request), 8Gi (limit)
        │
        │  KServe controller creează Deployment:
        ▼
Pod qwen25-3b-cpu-predictor-XXX
   │  schedulerName: <none>   ← default kube-scheduler, NU hami-scheduler
   │
   ├─ Container kserve-container (image llama.cpp:server-b4404)
   │   │
   │   ├─ Shell wrapper:
   │   │   1. Check if /models/qwen2.5-3b-instruct-q4_k_m.gguf exists
   │   │   2. If not, wget from MODEL_URL → save to PVC
   │   │   3. exec /llama-server with --model /models/$MODEL_FILE
   │   │
   │   └─ llama-server runs:
   │        --host 0.0.0.0 --port 8080
   │        --threads 6 --ctx-size 2048
   │        --cont-batching --n-gpu-layers 0
   │        → exposes OpenAI-compatible API on :8080
   │
   └─ Volume: model-cache-pvc (shared cu vLLM pods)
        │
        ▼ contains GGUF files cached across pods
```

### 15.5 Concurența vLLM + llama.cpp pe același cluster

Cele două backend-uri rulează **fără să se interfere**:

| Aspect | vLLM pods | llama.cpp pods |
|---|---|---|
| Scheduler | `hami-scheduler` | `kube-scheduler` (default) |
| GPU resources | `nvidia.com/gpu`, `gpumem`, `gpucores` | Niciunul |
| CPU resources | 1-4 cores | 4-8 cores |
| Memory | 4-16GB | 4-8GB |
| Pod placement | Doar pe noduri cu label `gpu=true` | Orice nod în cluster |
| PVC model-cache | Shared (read GGUF + HF cache) | Shared (read GGUF) |
| LiteLLM registration | Identică (POST /model/new) | Identică |
| Open WebUI dropdown | Apar amestecate | Apar amestecate |

În Open WebUI, user-ul vede:
```
Models:
  ▼ gemma-1b-fast          (GPU, vLLM)
  ▼ smollm3-3b-quality     (GPU, vLLM)
  ▼ qwen-3b-cpu            (CPU, llama.cpp)
```

Schimbarea modelului din dropdown e transparent — LiteLLM routes la backend-ul corespunzător.

### 15.6 Routing inteligent în LiteLLM — fallback GPU → CPU

Pentru prod, configurezi LiteLLM cu **router fallbacks**:

```yaml
# litellm-config.yaml
model_list:
  - model_name: "qwen-chat"           # alias virtual
    litellm_params:
      model: "openai/qwen25-3b-gpu"   # primary: GPU vLLM
      api_base: "http://qwen25-3b-predictor.inference.svc.cluster.local/v1"
      api_key: "dummy"
  - model_name: "qwen-chat-fallback"
    litellm_params:
      model: "openai/qwen25-3b-cpu"   # backup: CPU llama.cpp
      api_base: "http://qwen25-3b-cpu-predictor.inference.svc.cluster.local/v1"
      api_key: "dummy"

router_settings:
  fallbacks:
    - {"qwen-chat": ["qwen-chat-fallback"]}
  # Try GPU first. If it fails (timeout, 5xx, OOM), auto-retry on CPU.
  num_retries: 1
  request_timeout: 30
```

Astfel, user-ul cere `qwen-chat`, LiteLLM încearcă GPU pod; dacă răspunde în 30s ok, altfel cade automat pe CPU pod. **Transparent pentru client.**

### 15.7 Workload definition complet

Vezi `workloads/qwen25-3b-cpu.yaml`. Câmpurile cheie:

```yaml
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: qwen25-3b-cpu
  namespace: inference
spec:
  backend: llamacpp                 # ← cheia care selectează runtime-ul
  modelFile: "qwen2.5-3b-instruct-q4_k_m.gguf"
  modelUrl: "https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf"
  servedName: "qwen25-3b-cpu"
  ctxSize: 2048
  threads: 6
  batchSize: 512
  cpuRequest: "4"
  cpuLimit: "8"
  memoryRequest: "4Gi"
  memoryLimit: "8Gi"
  litellmAlias: "qwen-3b-cpu"
```

### 15.8 Tutorial: deploy Qwen2.5-3B CPU

```bash
# 1. Commit workload nou
git add workloads/qwen25-3b-cpu.yaml
git commit -m "feat: add Qwen2.5-3B CPU inference"
git push

# 2. ArgoCD sync (auto sau manual)
argocd app sync workloads

# 3. Watch progres
kubectl -n inference get inferenceendpoint qwen25-3b-cpu -w
kubectl -n inference get inferenceservices

# 4. Watch download progres
kubectl -n inference logs -l serving.kserve.io/inferenceservice=qwen25-3b-cpu -f
# Output expectat:
# Model file /models/qwen2.5-3b-instruct-q4_k_m.gguf not found. Downloading...
# Source: https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/...
# ...progress...
# Download complete: 1.9G  /models/qwen2.5-3b-instruct-q4_k_m.gguf
# Starting llama-server with 6 threads, context 2048
# llama-server: starting on 0.0.0.0:8080

# 5. Verifică pod plasat pe nod fără GPU (sau orice nod, nu contează)
kubectl -n inference get pod -l serving.kserve.io/inferenceservice=qwen25-3b-cpu -o wide

# 6. Verifică LiteLLM are modelul înregistrat
kubectl -n ai-platform port-forward svc/litellm 4000:4000 &
curl http://localhost:4000/v1/models \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" | jq '.data[].id'
# Output: "qwen-3b-cpu", alături de "gemma-1b-fast", "smollm3-3b-quality"

# 7. Test inference
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen-3b-cpu",
    "messages": [{"role": "user", "content": "Salut! Vorbești română?"}],
    "max_tokens": 100
  }'

# 8. Open WebUI — modelul apare în dropdown
xdg-open http://<node-ip>:30080
```

### 15.9 Production hardening pentru CPU backend

| Item | Action |
|---|---|
| **Model pre-download** | Pre-populează PVC cu GGUF înainte de deploy; elimini cold-start network dependency |
| **OCI model packaging** | Push GGUF ca OCI artifact în Harbor, schimbi shell wrapper să folosească `oras pull` |
| **HPA pe CPU utilization** | `kind: HorizontalPodAutoscaler` cu `targetCPUUtilizationPercentage: 70` |
| **Node affinity** | `nodeSelector: cpu-only=true` pe pod-uri llama.cpp ca să nu ocupe noduri GPU degeaba |
| **Resource quotas** | `LimitRange` pe namespace pentru a împiedica modele CPU să acapareze tot cluster-ul |
| **mlock pentru latency** | Adaugă `--mlock` în args + `securityContext.capabilities.add: [IPC_LOCK]` pentru a împiedica swap |
| **Multi-replica** | `minReplicas: 2` cu PDB pentru HA |
| **Distinct PVC pentru GGUF** | Separate de model-cache HF pentru lifecycle management diferit |

### 15.10 Observabilitate CPU backend

llama.cpp server expune **Prometheus metrics** pe `/metrics` (flag `--metrics`):

```
llamacpp_n_prompt_tokens_processed_total
llamacpp_n_tokens_predicted_total
llamacpp_prompt_tokens_seconds
llamacpp_predicted_tokens_seconds
llamacpp_kv_cache_usage_ratio
llamacpp_kv_cache_tokens
llamacpp_requests_processing
llamacpp_requests_deferred
```

PodMonitor pentru scraping (similar cu vLLM):

```yaml
# Add to platform/observability/llamacpp-podmonitor.yaml
apiVersion: monitoring.coreos.com/v1
kind: PodMonitor
metadata:
  name: llamacpp-inference-pods
  namespace: inference
spec:
  namespaceSelector:
    matchNames: [inference]
  selector:
    matchExpressions:
      - key: serving.kserve.io/inferenceservice
        operator: Exists
  podMetricsEndpoints:
    - port: http
      path: /metrics
      interval: 15s
```

**Limitare actuală**: llama.cpp server **nu suportă încă OTLP nativ**. Tracing distribuit acoperă doar până la LiteLLM → pod (request-ul intră în pod, dar pod-ul nu emite span-uri proprii). Acceptabil pentru PoC.

Pentru tracing complet, ai două opțiuni de viitor:
1. Aștepți suport OTLP nativ în llama.cpp (urmărit aici: github.com/ggerganov/llama.cpp issues)
2. Folosești un **sidecar proxy cu OTel** (Envoy/nginx + OTel collector instrumentation) între LiteLLM și llama.cpp pod

### 15.11 Critici onești ale acestei integrări

**Compromisuri reale acceptate în PoC:**

1. **Download la cold start** — primul boot al pod-ului ia 30-60s pentru 2GB GGUF. Pe slow networks (sub 50Mbps), poate fi minute. Pentru prod: pre-bake.

2. **`apt-get install wget` în shell wrapper** — fragil, depinde de imaginea de bază. Dacă llama.cpp upstream schimbă la imagine slim/distroless, breakage. Fix prod: image custom cu wget pre-installed.

3. **No OTLP tracing din llama.cpp** — gap real în observabilitate end-to-end. Documentat.

4. **CEL ternary pentru resources** — pattern fragil în KRO v1alpha1. Funcționează acum, dar testare extinsă recomandată pe versiuni viitoare KRO.

5. **`nvidia.com/gpu: "0"` în resources** — Kubernetes admission *poate* respinge în versiuni stricte. Soluție alternativă mai sigură: două RGD-uri separate (deși fragmentează schema). Validare runtime necesară.

6. **Shared PVC pentru ambele backends** — funcționează dar nu e ideal. Vlmm cache (`.cache/huggingface`) și GGUF files au lifecycle diferit. Pentru prod: PVC-uri separate.

7. **Threading hardcoded la 6** — ar trebui dinamic în funcție de CPU-ul nodului. Fix: Downward API pentru a citi `requests.cpu` și pasa ca `--threads`.

---

## Anexe

### A. Lista completă a fișierelor

```
qsint-ai-platform/
├── README.md
├── bootstrap.sh
├── docs/
│   ├── deployment-guide.md
│   ├── design-doc.md
│   └── docs.md                          ← acest document
├── kro-templates/
│   └── inference-endpoint-rgd.yaml       ← ResourceGraphDefinition KRO
├── platform/
│   ├── argocd/                           ← 5 fișiere: AppProject + 3 Applications + kustomization
│   ├── namespaces/
│   │   └── namespaces.yaml
│   ├── hami/
│   │   ├── README.md
│   │   ├── hami-application.yaml
│   │   └── servicemonitor.yaml
│   ├── kro/
│   │   └── kro-application.yaml
│   ├── kserve/
│   │   ├── huggingface-secret.yaml
│   │   ├── kserve-application.yaml
│   │   ├── litellm-secret-mirror.yaml
│   │   ├── llamacpp-servingruntime.yaml ← CPU runtime via llama.cpp server
│   │   ├── model-cache-pvc.yaml
│   │   └── vllm-servingruntime.yaml     ← GPU runtime via vLLM
│   ├── postgresql/
│   │   └── postgresql.yaml
│   ├── litellm/
│   │   └── litellm.yaml                  ← cu OTel callbacks
│   ├── langfuse/
│   │   └── langfuse.yaml
│   ├── open-webui/
│   │   └── open-webui.yaml
│   └── observability/                    ← stack-ul de observabilitate
│       ├── jaeger.yaml
│       ├── otel-collector.yaml
│       ├── vllm-podmonitor.yaml
│       ├── grafana-dashboard-hami.json
│       ├── grafana-dashboard-vllm.json
│       ├── grafana-dashboard-litellm.json
│       └── grafana-dashboards-configmap.yaml  ← auto-import în Grafana
└── workloads/
    ├── gemma-1b.yaml                     ← GPU (vLLM, Gemma)
    ├── qwen25-3b-cpu.yaml                ← CPU (llama.cpp, Qwen)
    └── smollm3-3b.yaml                   ← GPU (vLLM, SmolLM3)
```

### B. Endpoint-uri și porturi expuse

Toate UI-urile sunt expuse prin MicroK8s ingress pe hostnames locale. Rulează `sudo ./scripts/update-local-hosts.sh`, apoi folosește tabelul [Local UI access and credentials](#local-ui-access-and-credentials).

| Serviciu | Namespace | Port intern | Ingress local |
|---|---|---|---|
| Argo CD | `argocd` | 8080/443 | http://argocd.local.ro |
| GitLab | `gitlab` | 8181 | http://gitlab.local.ro |
| GitLab MinIO | `gitlab` | 9000 | http://minio.local.ro |
| GitLab KAS | `gitlab` | 8150 | http://kas.local.ro |
| Grafana | `observability` | 3000 | http://grafana.local.ro |
| Open WebUI | `ai-platform` | 8080 | http://open-webui.local.ro |
| Langfuse | `ai-platform` | 3000 | http://langfuse.local.ro |
| Jaeger UI | `ai-platform` | 16686 | http://jaeger.local.ro |
| LiteLLM API / admin | `ai-platform` | 4000 | http://litellm.local.ro |

### C. Tools comands cheat-sheet

```bash
# ArgoCD
argocd app list
argocd app sync <name>
argocd app diff <name>

# KRO
kubectl get resourcegraphdefinitions
kubectl get inferenceendpoints -A

# KServe
kubectl get inferenceservices -A
kubectl get servingruntime,clusterservingruntime -A

# HAMi
kubectl -n hami-system logs -l app.kubernetes.io/name=hami-scheduler
kubectl describe node <gpu-node> | grep nvidia.com

# Force-reconcile InferenceEndpoint
kubectl annotate inferenceendpoint <name> -n inference \
  kro.run/force-reconcile="$(date +%s)" --overwrite
```

### D. Referințe externe

- AWS re:Invent 2026: "Building an Internal AI Platform with KRO" (slide-urile sursă)
- KServe docs: https://kserve.github.io/website/
- KRO docs: https://kro.run
- HAMi docs: https://github.com/Project-HAMi/HAMi
- vLLM docs: https://docs.vllm.ai
- llama.cpp server: https://github.com/ggerganov/llama.cpp/tree/master/examples/server
- Qwen2.5 GGUF models: https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF
- LiteLLM docs: https://docs.litellm.ai
- Langfuse docs: https://langfuse.com/docs
- OpenTelemetry Collector: https://opentelemetry.io/docs/collector/
- Jaeger: https://www.jaegertracing.io/docs/
