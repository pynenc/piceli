# Frequently Asked Questions

## Why Python instead of YAML, Kustomize or Helm?

YAML has no types, functions or tests. Kustomize and Helm add reuse by patching or
templating text. Piceli builds manifests from typed Python objects, so you get
autocompletion, validation before anything reaches the cluster, loops and
functions for reuse, and ordinary unit tests for your infrastructure. It still
accepts plain YAML/JSON, so you can migrate gradually.

## Can I keep my existing YAML files?

Yes. Point `--folder-path` (or `PICELI__FOLDER_PATH`) at a directory of YAML or
JSON manifests. They are loaded alongside Python-defined objects. See
{doc}`kubernetes_model/yaml_json_definitions`.

## Does `piceli deploy run` patch my resources?

No. The CLI engine deletes and recreates objects that already exist, which briefly
interrupts them. Use `piceli deploy detail` to review the changes first. The
recoverable engine (`DeploymentSession`) uses server-side apply. See
{doc}`overview` for a comparison of the two engines.

## Which cluster does Piceli talk to?

The CLI engine uses your current kubeconfig context. The recoverable engine and
the `observe`/`operator` commands never pick up credentials implicitly: you pass
an `ApiClient` or `--kubeconfig`/`--context` explicitly.

## Does importing Piceli contact my cluster?

No. Importing modules, loading models and building plans are all side-effect
free. Only explicit execution and observation commands make network calls.

## Will Piceli delete resources I did not create?

The recoverable engine only deletes objects that carry its owner annotation, and
only when pruning is explicitly enabled and discovery coverage is complete.
Namespaces, PersistentVolumes, PersistentVolumeClaims and Secrets are never
deleted. Objects present in the namespace but not declared by a release are
reported as *undeclared*; they are never adopted or removed automatically.

## Does Piceli support custom resources (CRDs)?

The recoverable engine accepts any `apiVersion`/`kind` as a raw manifest in a
`ResourceIntent`. Typed templates for custom resources are on the {doc}`roadmap`.

## Does Piceli push images to a registry?

No. `piceli artifacts` builds deterministic OCI layouts locally and only imports
them into a local engine when you explicitly ask it to. Nothing is pushed
automatically.

## Is the web UI safe to expose?

No. `piceli observe serve` and `piceli operator serve` bind to `127.0.0.1` only
and are meant for the operator's own machine. Do not put them behind a public
proxy.

## How does Piceli relate to Pynenc?

Both are developed within the Pynenc project, but Piceli does not depend on
Pynenc and can deploy any workload.
