{{/* Chart name / fullname helpers */}}
{{- define "rr.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "rr.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "rr.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "rr.labels" -}}
app.kubernetes.io/name: {{ include "rr.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: redevops-runtime
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "rr.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "rr.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* Effective model endpoint: the bundled ROCm server if enabled, else the external endpoint. */}}
{{- define "rr.modelEndpoint" -}}
{{- if .Values.model.server.enabled -}}
http://{{ include "rr.fullname" . }}-model:8000/v1
{{- else -}}
{{ .Values.model.endpoint }}
{{- end -}}
{{- end -}}

{{/* Effective pgvector DSN: the bundled Postgres if enabled, else the provided DSN. */}}
{{- define "rr.pgvectorDsn" -}}
{{- if and (eq .Values.retriever.backend "pgvector") .Values.retriever.pgvector.bundled.enabled -}}
postgres://redevops:redevops@{{ include "rr.fullname" . }}-pgvector:5432/redevops_rag?sslmode=disable
{{- else -}}
{{ .Values.retriever.pgvector.dsn }}
{{- end -}}
{{- end -}}
