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
postgres://redevops:$(RDO_PGVECTOR_PASSWORD)@{{ include "rr.fullname" . }}-pgvector:5432/redevops_rag?sslmode=disable
{{- else -}}
{{ .Values.retriever.pgvector.dsn }}
{{- end -}}
{{- end -}}

{{/*
rr.imageRef: the ONLY way a container image reference is built in this chart.
  args: dict "image" (map: repository, digest, tag) "what" (value path, for messages)
There is deliberately NO default image: the previously published tags (0.3.0, 0.3.1, v6) are affected by an
unauthenticated-control-plane defect and no fixed public image exists yet. Rules:
  * digest (preferred, immutable): `sha256:` plus 64 lowercase hex characters -> repository@digest
  * otherwise a version tag (MAJOR.MINOR.PATCH[-suffix]); never latest/stable/main/v6/0.3.0/0.3.1 -> repository:tag
  * neither -> the render fails
DENY-LIST (vulnerable public images 0.3.0, 0.3.1 and v6; index and amd64-manifest digests): see the lists below.
*/}}
{{- define "rr.imageRef" -}}
{{- $img := .image -}}
{{- $what := .what -}}
{{- $deniedTags := list "latest" "stable" "main" "master" "dev" "edge" "nightly" "v6" "0.3.0" "0.3.1" -}}
{{- $deniedDigests := list "sha256:9d0315d8f49711ee2f41e027252fb5d0b3cdaeea91b9bed57bd6d10cffef6cf6" "sha256:86f91365bab7ee8e604ed5854aa0d56881ba984ec834deab3b4d6d5ec22d7e56" "sha256:a134be71553621861aee2b56dd434aa5d19432c6df7dcdeb8360b23146a22fe1" "sha256:b7e6dd3b53e8bc4ddfbc7b977113e4198448833a74318498bb17c41f841e05f6" "sha256:cabbdd8d1c5acdc9ba7c2e371ea2dfdc49241d36a7a7ada1af9cfe31974fd7ab" "sha256:f43aeb9f3deaeb97c9615b2794ac3d640eaf66959c6d367028e6c755408f62c7" -}}
{{- $repo := required (printf "%s.repository is required" $what) $img.repository -}}
{{- if $img.digest -}}
{{- if not (regexMatch "^sha256:[a-f0-9]{64}$" ($img.digest | toString)) -}}
{{- fail (printf "%s.digest must be sha256: followed by 64 lowercase hex characters (got %q)" $what ($img.digest | toString)) -}}
{{- end -}}
{{- if has ($img.digest | toString) $deniedDigests -}}
{{- fail (printf "%s.digest is a known vulnerable image (published 0.3.0, 0.3.1 or v6: unauthenticated control plane, runs as root); supply the digest of the fixed image instead" $what) -}}
{{- end -}}
{{- printf "%s@%s" $repo ($img.digest | toString) -}}
{{- else if $img.tag -}}
{{- $tag := $img.tag | toString -}}
{{- if has $tag $deniedTags -}}
{{- fail (printf "%s.tag %q is refused: floating tags and the vulnerable published tags (0.3.0, 0.3.1, v6) are not allowed; use %s.digest (sha256:...) or a fixed version tag" $what $tag $what) -}}
{{- end -}}
{{- if not (regexMatch "^[0-9]+\\.[0-9]+\\.[0-9]+([-+][0-9A-Za-z.-]+)?$" $tag) -}}
{{- fail (printf "%s.tag %q is refused: use a MAJOR.MINOR.PATCH version tag, or better %s.digest (sha256:...)" $what $tag $what) -}}
{{- end -}}
{{- printf "%s:%s" $repo $tag -}}
{{- else -}}
{{- fail (printf "%s.digest (preferred: sha256:<64 hex>, an immutable reference) or %s.tag is required: this chart has NO default image because no fixed public image has been published yet (the old 0.3.0, 0.3.1 and v6 tags are affected, see docs/release/CONTAINER_ACCEPTANCE_CRITERIA.md). Set it with --set %s.digest=sha256:..." $what $what $what) -}}
{{- end -}}
{{- end -}}

{{/* Third-party images (bundled pgvector, ROCm vLLM): a digest-pinned reference string, `repo@sha256:<64 hex>`, no tag-only form. */}}
{{- define "rr.pinnedImage" -}}
{{- $ref := .ref | toString -}}
{{- if not $ref -}}
{{- fail (printf "%s is required (an image reference pinned by digest, repo@sha256:<64 hex>)" .what) -}}
{{- end -}}
{{- if not (regexMatch "^[A-Za-z0-9./_-]+(:[A-Za-z0-9._-]+)?@sha256:[a-f0-9]{64}$" $ref) -}}
{{- fail (printf "%s must be pinned by digest as repo@sha256:<64 lowercase hex> (got %q); a tag such as :latest is mutable and a placeholder digest is not a digest" .what $ref) -}}
{{- end -}}
{{- $ref -}}
{{- end -}}

{{/* The runtime image: the enterprise image when the security plane is enabled with one, else the public runtime image. */}}
{{- define "rr.runtimeImage" -}}
{{- if and .Values.security.enabled .Values.security.image.repository -}}
{{- include "rr.imageRef" (dict "image" .Values.security.image "what" "security.image") -}}
{{- else -}}
{{- include "rr.imageRef" (dict "image" .Values.runtime.image "what" "runtime.image") -}}
{{- end -}}
{{- end -}}

{{/* Selector labels of a component of this release. */}}
{{- define "rr.selector" -}}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}
