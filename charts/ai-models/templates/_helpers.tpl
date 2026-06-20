{{/*
ai-models.registerJob — renders a LiteLLM registration Job for one model.

Usage (from a per-model file, after the InferenceEndpoint):
  {{- include "ai-models.registerJob" (dict
        "name" "gemma-1b" "alias" "gemma-1b-fast" "served" "gemma-1b"
        "backend" "vllm" "desc" "TinyLlama 1.1B AWQ via vLLM" "root" $) }}

Shared config (namespace, litellmUrl, registerImage) comes from values.yaml via
.root; the per-model fields are passed inline. The Job waits for the model's
predictor to answer /v1/models, then POSTs it to LiteLLM (idempotent on id).
*/}}
{{- define "ai-models.registerJob" -}}
{{- $ := .root }}
---
apiVersion: batch/v1
kind: Job
metadata:
  name: {{ .name }}-litellm-register
  namespace: {{ $.Values.namespace }}
  labels:
    app.kubernetes.io/name: ai-models
    app.kubernetes.io/component: litellm-registration
    ai-platform/model: {{ .name }}
spec:
  backoffLimit: 30
  ttlSecondsAfterFinished: 3600
  template:
    metadata:
      labels:
        app.kubernetes.io/name: ai-models
        app.kubernetes.io/component: litellm-registration
        ai-platform/model: {{ .name }}
    spec:
      restartPolicy: OnFailure
      containers:
        - name: register
          image: {{ $.Values.registerImage }}
          env:
            - name: MODEL_URL
              value: "http://{{ .name }}-predictor.{{ $.Values.namespace }}.svc.cluster.local"
            - name: LITELLM_URL
              value: {{ $.Values.litellmUrl | quote }}
            - name: LITELLM_ALIAS
              value: {{ .alias | quote }}
            - name: SERVED_NAME
              value: {{ .served | quote }}
            - name: MODEL_ID_TAG
              value: {{ .name | quote }}
            - name: BACKEND
              value: {{ .backend | quote }}
            - name: MODEL_DESC
              value: {{ .desc | quote }}
            - name: LITELLM_MASTER_KEY
              valueFrom:
                secretKeyRef:
                  name: litellm-secrets
                  key: LITELLM_MASTER_KEY
          command:
            - sh
            - -c
            - |
              set -eu
              echo "Waiting for $MODEL_URL/v1/models..."
              i=0
              while [ "$i" -lt 180 ]; do
                if curl -sf -o /tmp/models.json "$MODEL_URL/v1/models"; then
                  cat /tmp/models.json; break
                fi
                i=$((i + 1)); sleep 10
              done
              if [ "$i" -eq 180 ]; then
                echo "ERROR: model endpoint did not become ready: $MODEL_URL"; exit 1
              fi
              ADDITIONAL_PARAMS=""
              if [ "$BACKEND" = "llamacpp" ]; then
                ADDITIONAL_PARAMS=', "drop_params": true, "additional_drop_params": ["tools", "tool_choice"]'
              fi
              PAYLOAD=$(cat <<JSON
              {
                "model_name": "$LITELLM_ALIAS",
                "litellm_params": {
                  "model": "openai/$SERVED_NAME",
                  "api_base": "$MODEL_URL/v1",
                  "api_key": "dummy-not-required-for-self-hosted"$ADDITIONAL_PARAMS
                },
                "model_info": {"id": "$MODEL_ID_TAG", "description": "$MODEL_DESC"}
              }
              JSON
              )
              # Idempotent on id: delete then re-create so changed params apply.
              curl -s -X POST "$LITELLM_URL/model/delete" \
                -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
                -H "Content-Type: application/json" \
                -d "{\"id\":\"$MODEL_ID_TAG\"}" >/dev/null || true
              curl -sf -X POST "$LITELLM_URL/model/new" \
                -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
                -H "Content-Type: application/json" \
                -d "$PAYLOAD"
              echo; echo "Registered $LITELLM_ALIAS"
{{- end -}}
