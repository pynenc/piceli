"""Client bundles: an app packaged for a cluster Piceli never accesses.

``piceli bundle`` renders an app (usually its client environment) as a
kustomize base and overlay, a ``prepare.sh`` that creates its generated
Secrets in the client's cluster, OCI image archives with their SBOMs, install
and uninstall guides and a checksums file, after a safety gate for foreign
clusters. ``piceli support-bundle`` collects a read-only, redacted snapshot of
an installed app. See ``docs/client_delivery.md``.

Importing this package is side-effect free.
"""
