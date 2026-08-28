# MatrixOrigin Huawei Cloud MaaS provider

This is a separate Dify model plugin because the pinned Dify plugin manifest
contract exposes only one model provider per plugin package.

Provider identity:

- plugin ID: `matrixorigin/matrixorigin_huawei_maas`
- provider: `huawei_maas`
- provider-qualified ID: `matrixorigin/matrixorigin_huawei_maas/huawei_maas`

The predefined contract is deliberately text-only:

- chat: `glm-5.2`
- embedding: `bge-m3`, dimension `1024`
- base URL: `MAAS_BASE_URL`, defaulting to
  `https://api.modelarts-maas.com/v1`
- credential: `MAAS_API_KEY`

The runtime accepts only the official HTTPS `api.modelarts-maas.com/v1`
endpoint (without credentials, query strings, or fragments) and only serves
the two models above; arbitrary OpenAI-compatible or legacy provider URLs are
rejected before an outbound request.

GLM requests force `thinking: {"type":"disabled"}` at the Dify OpenAI-
compatible invocation seam. No credential is bundled in the package.

## Safe readiness check

The verifier is offline and read-only. It checks the source/package manifest,
provider identity, exact model names, the embedding dimension, and whether
`MAAS_API_KEY` is present without printing its value:

```bash
export MAAS_BASE_URL="${MAAS_BASE_URL:-https://api.modelarts-maas.com/v1}"
# Load MAAS_API_KEY from the local secret manager or an already-protected environment.
python3 local-rag-platforms/dify-rag-eval/dify-plugins/matrixorigin-huawei-maas/verify_huawei_maas.py --json
```

The verifier never installs a plugin, writes Dify state, or calls MaaS. Dify
console authentication is required for the operator step: install
`matrixorigin-huawei-maas.difypkg`, open Model Provider settings, configure
the `huawei_maas` provider with the protected `MAAS_API_KEY` and
`MAAS_BASE_URL`, and select `glm-5.2` / `bge-m3`. The existing
`matrixorigin_taas` package is upgraded separately to `0.0.11`.
