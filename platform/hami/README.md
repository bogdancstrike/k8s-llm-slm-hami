# HAMi Setup

HAMi enables GPU sharing on a single physical RTX 3080 by exposing virtual GPUs (vGPUs)
to Kubernetes with **per-pod vRAM limits**.

## How pods request a vGPU slice

Standard NVIDIA device plugin gives you 1 GPU per pod. With HAMi, you instead request:

```yaml
resources:
  limits:
    nvidia.com/gpu: 1              # Number of vGPUs
    nvidia.com/gpumem: 5000        # vRAM in MB (5GB)
    nvidia.com/gpucores: 50        # Compute % of physical GPU (0-100)
```

Both inference pods (gemma-1b + smollm3-3b) request `gpumem: 5000`, totaling 10000 MB
which fits the RTX 3080's 10GB VRAM exactly. HAMi enforces these limits at runtime via
LD_PRELOAD hooks on CUDA calls — out-of-bounds allocations are rejected, not silently
overflowed.

## Node labeling (required before HAMi installs)

Label your GPU node so the device plugin daemonset can find it:

```bash
kubectl label node <your-node-name> gpu=true
```

## Verifying installation

```bash
# 1. Check device plugin pod is running on GPU node
kubectl -n hami-system get pods -l app.kubernetes.io/name=hami-device-plugin -o wide

# 2. Check the node now advertises vGPU resources
kubectl describe node <your-node-name> | grep -A 3 "Capacity:"
# Should show: nvidia.com/gpu: 10  (deviceSplitCount × 1 physical GPU)

# 3. Check HAMi scheduler is running
kubectl -n hami-system get pods -l app.kubernetes.io/name=hami-scheduler
```

## Pods must opt into the HAMi scheduler

Pods that want vRAM partitioning **must set `schedulerName: hami-scheduler`** in their
podSpec. Otherwise the default scheduler will schedule them and HAMi's resource accounting
won't apply. This is set in the vLLM ServingRuntime in `platform/kserve/`.

## Monitoring

HAMi exposes Prometheus metrics on the scheduler service. A ServiceMonitor is in
`platform/hami/servicemonitor.yaml`. Key metrics:

- `hami_vgpu_memory_used_bytes{pod="..."}` — actual vRAM usage per pod
- `hami_vgpu_core_util_percent{pod="..."}` — compute util per pod
- `hami_vgpu_count` — total vGPUs allocated

Grafana dashboard JSON in `platform/hami/grafana-dashboard.json`.
