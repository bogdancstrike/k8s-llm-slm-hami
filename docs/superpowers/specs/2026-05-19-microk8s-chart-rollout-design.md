# MicroK8s Chart Rollout Design

## Goal

Deploy the QSINT AI platform from `/home/bogdan/workspace/infrastructure/k8s-ai` using reusable Helm charts, while keeping UI access behind local DNS hostnames and MicroK8s ingress.

## Scope

Platform-owned components are deployed from this repository:

- Argo CD
- cert-manager
- kube-prometheus-stack with Grafana
- HAMi
- KRO
- KServe
- GitLab
- QSINT platform services: PostgreSQL, LiteLLM, Langfuse, Open WebUI, Jaeger, OTel Collector, KServe runtimes, dashboards, ServiceMonitors
- KRO templates and example GPU/CPU model workloads

MicroK8s GPU operator, NVIDIA runtime setup, Calico, CoreDNS, hostpath provisioner, and the MicroK8s ingress addon remain cluster prerequisites.

## Deployment Shape

External upstream projects are wrapped by local charts under `charts/` with values committed in this repo. Local platform YAML is copied into local application charts:

- `charts/qsint-platform`
- `charts/qsint-kro-templates`
- `charts/qsint-workloads`

The deploy script recreates platform-owned releases in dependency order:

1. cert-manager
2. kube-prometheus-stack
3. Argo CD
4. HAMi
5. KRO
6. KServe CRDs
7. KServe controller
8. GitLab
9. QSINT platform services
10. KRO templates
11. workloads

## MicroK8s Decisions

Storage uses `microk8s-hostpath`. The shared model cache uses `ReadWriteOnce`; this is acceptable for the single-node PoC because all pods mount on the same node.

HAMi is installed in `kube-system`, uses node label `gpu=on`, and the model workloads use `nvidia.com/gpumem-percentage` plus `nvidia.com/gpu: "1"` so HAMi v2.8 injects its CUDA shim correctly.

Prometheus discovery uses the existing kube-prom-stack release label `kube-prom-stack`, and HAMi ServiceMonitors target services in `kube-system`.

## Local DNS

The following hostnames point at `127.0.0.1` in `/etc/hosts`:

- `argocd.local.ro`
- `gitlab.local.ro`
- `grafana.local.ro`
- `open-webui.local.ro`
- `langfuse.local.ro`
- `jaeger.local.ro`
- `litellm.local.ro`

