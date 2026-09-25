{{- define "webapp.podSecurity" -}}
automountServiceAccountToken: false
terminationGracePeriodSeconds: 10
securityContext:
  fsGroup: 10001
  runAsGroup: 10001
  runAsNonRoot: true
  runAsUser: 10001
  seccompProfile:
    type: RuntimeDefault
{{- end }}

{{- define "webapp.containerSecurity" -}}
securityContext:
  allowPrivilegeEscalation: false
  capabilities:
    drop: [ALL]
  readOnlyRootFilesystem: true
{{- end }}

{{- define "webapp.server" -}}
image: {{ .Values.image }}
command: [httpd, -f, -p, "8080", -h, /srv]
ports:
  - containerPort: 8080
readinessProbe:
  httpGet:
    path: /healthz
    port: 8080
volumeMounts:
  - name: site
    mountPath: /srv
{{ include "webapp.containerSecurity" . }}
{{- end }}
