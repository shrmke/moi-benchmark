# MatrixOrigin TaaS Provider for Dify

This private Dify model-provider plugin connects Dify to MatrixOrigin TaaS.
It is intentionally a single-provider package because the pinned Dify
plugin-daemon management API exposes one model provider per plugin package.
The package is version `0.0.11`; upgrade the installed `0.0.9` package to this
archive before selecting MatrixOrigin TaaS models.

Supported predefined models:

- `deepseek-v4-flash` — chat completion
- `bge-m3` — text embedding
- `qwen3-vl-embedding` — text and image embedding
- `qwen3-rerank` — text rerank
- `qwen3-vl-rerank` — text and image rerank

## Install

Upload `matrixorigin-taas.difypkg` from **Plugins → Install Plugin → Via Local
File**. Then open **Settings → Model Provider → MatrixOrigin TaaS**, enter your
TaaS API key, and keep the default base URL unless you use another gateway.

The package never contains an API key. Credentials are stored by Dify after
installation.

## Huawei Cloud MaaS is a separate package

Huawei Cloud MaaS is shipped as `matrixorigin-huawei-maas.difypkg`, with
plugin ID `matrixorigin/matrixorigin_huawei_maas` and provider-qualified ID
`matrixorigin/matrixorigin_huawei_maas/huawei_maas`. It exposes only the
text models `glm-5.2` and `bge-m3` (1024 dimensions), using
`MAAS_BASE_URL` or the default `https://api.modelarts-maas.com/v1` and the
secret `MAAS_API_KEY`.

Run the read-only repository check before an operator installs it:

```bash
python local-rag-platforms/dify-rag-eval/dify-plugins/matrixorigin-huawei-maas/verify_huawei_maas.py --json
```

The command never contacts a provider, mutates Dify, or prints a key. An
operator must then install `matrixorigin-huawei-maas.difypkg` through the
Dify console, open **Settings → Model Provider → Huawei Cloud MaaS**, and
enter the key in Dify's secret field. Do not write a key to the package or
modify Dify's database directly.

## Why a dedicated provider is needed

The generic OpenAI-compatible plugin sends multimodal embedding input through
the vLLM `messages` extension. TaaS expects OpenAI content parts under
`input[].content` instead. This plugin performs that translation and invokes
`qwen3-vl-embedding` one item at a time to preserve TaaS response cardinality.
