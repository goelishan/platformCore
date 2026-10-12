{{/*
Shared names, labels and the image reference.
*/}}

{{- define "oncall.labels" -}}
app.kubernetes.io/name: oncall
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "oncall.selector" -}}
app.kubernetes.io/name: oncall
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* tag is required; a digest, when given, is what the runtime pulls. */}}
{{- define "oncall.image" -}}
{{- $tag := required "image.tag is required (make -C oncall image prints it)" .Values.image.tag -}}
{{- if .Values.image.digest -}}
{{ .Values.image.repository }}:{{ $tag }}@{{ .Values.image.digest }}
{{- else -}}
{{ .Values.image.repository }}:{{ $tag }}
{{- end -}}
{{- end -}}

{{/* Pod security shared by every workload in the chart. */}}
{{- define "oncall.podSecurity" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
fsGroup: 10001
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "oncall.containerSecurity" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end -}}

{{/* Configuration and, on kind, the generated store password. */}}
{{- define "oncall.envFrom" -}}
- configMapRef:
    name: oncall-config
{{- if .Values.store.inCluster.enabled }}
- secretRef:
    name: oncall-store
{{- end }}
{{- end -}}
