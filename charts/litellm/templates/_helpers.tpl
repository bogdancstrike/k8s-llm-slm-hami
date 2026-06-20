{{- define "litellm.name" -}}
{{- .Values.nameOverride | default "litellm" -}}
{{- end -}}

{{- define "litellm.labels" -}}
app: {{ include "litellm.name" . }}
app.kubernetes.io/name: {{ include "litellm.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "litellm.databaseUrl" -}}
postgresql://{{ .Values.database.user }}:{{ .Values.database.password }}@{{ .Values.database.host }}:{{ .Values.database.port }}/{{ .Values.database.name }}
{{- end -}}
