{{/* Chart name. */}}
{{- define "lldk.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Fully qualified app name. */}}
{{- define "lldk.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* Common labels. */}}
{{- define "lldk.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "lldk.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/* Selector labels for the gateway. */}}
{{- define "lldk.selectorLabels" -}}
app.kubernetes.io/name: {{ include "lldk.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: gateway
{{- end -}}

{{/* Selector labels for vLLM. */}}
{{- define "lldk.vllmSelectorLabels" -}}
app.kubernetes.io/name: {{ include "lldk.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: vllm
{{- end -}}

{{/* Secret holding bootstrapAdminKey / metricsToken / openaiCompatApiKey. */}}
{{- define "lldk.secretName" -}}
{{- default (printf "%s-secrets" (include "lldk.fullname" .)) .Values.secrets.existingSecret -}}
{{- end -}}

{{/* Upstream URL: the chart's vLLM service when enabled, else the configured URL. */}}
{{- define "lldk.openaiCompatBaseUrl" -}}
{{- if .Values.vllm.enabled -}}
{{- printf "http://%s-vllm:8000/v1" (include "lldk.fullname" .) -}}
{{- else -}}
{{- .Values.backend.openaiCompatBaseUrl -}}
{{- end -}}
{{- end -}}
