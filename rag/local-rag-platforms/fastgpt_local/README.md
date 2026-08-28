# FastGPT local

Pinned source: FastGPT `v4.15.6` from
<https://github.com/labring/FastGPT>. The official self-host Docker flow is
documented at <https://doc.fastgpt.io/en/self-host/deploy/docker>.

## Prepare and start

```bash
python3 local-rag-platforms/scripts/deployment/prepare_local_services.py prepare fastgpt_local
```

Use the pinned release’s official PgVector compose variant under the prepared
checkout at
`document/public/deploy/docker/v4.15/global/docker-compose.pg.yml`. Keep the
FastGPT web port on `3000`; do not start it alongside a second competitor
stack. The pinned source checkout is `v4.15.6`, while the release’s checked-in
deployment file currently references FastGPT and code-sandbox images at
`v4.15.4`; this is recorded as a version divergence in the runtime manifest.
Configure at least one Language Model and one Index Model through the local UI.
The historical Qianfan pair is documented below as a legacy condition. The
current MOI text-only serial benchmark uses only Huawei MaaS `glm-5.2` plus
`bge-m3` (effective upstream dimension 1024); the product and its data services
remain local. TaaS, Qianfan, and MaaS remain independent provider options and
must not share an embedding index.

### Current MOI MaaS contract

For the current benchmark, the helper materializes exactly two FastGPT
`system_models` rows before dataset creation:

- `glm-5.2`: active `llm`, OpenAI-compatible provider, `thinking={"type":"disabled"}`;
- `bge-m3`: active `embedding`, OpenAI-compatible provider, weight `100`.

`fastgpt_local.py provider --provider maas --execute` performs this idempotent
metadata update, then verifies the exact AIProxy channel. The generic saved
channel aggregate may return an empty result on AIProxy `v0.6.5`; for MaaS this
is recorded as non-authoritative, while the direct MaaS probe and isolated
FastGPT dataset/native-QA smoke are authoritative. The model update only
touches these two exact model names and never deletes unrelated rows.

### Qianfan embedding dimension contract

`qianfan-models.example.json` registers `qwen3-embedding-8b` as an active
`embedding` model with a positive training weight. `QIANFAN_EMBEDDING_DIMENSION=4096`
records the upstream Qianfan vector width. The pinned FastGPT v4.15.x runtime,
however, creates `VECTOR(1536)`/`HALFVEC(1536)` stores and truncates wider
embedding responses to 1536 before insertion. Therefore the local adapter can
create a Qianfan dataset and use the OpenAI-compatible embedding endpoint, but
the effective FastGPT index is 1536-dimensional, not a lossless 4096-dimensional
index. Smoke artifacts record `source_dimension`, `effective_dimension`, and
`dimension_action` so this is visible in evaluation metadata.

The model configuration must be imported/enabled in FastGPT before the runner
creates a dataset; creating an AIProxy channel alone is insufficient. The
runner's dataset request must use `vectorModel=qwen3-embedding-8b` and the
registered Qianfan chat model as `agentModel`.

```bash
docker compose -p moi_fastgpt_local \
  -f .local-services/fastgpt_local/compose/docker-compose.pg.yml \
  up -d
curl -fsS http://127.0.0.1:3000
```

The runtime compose copy is based on the official file and supplies the local
values required by the current release (`FE_DOMAIN`, `FILE_DOMAIN`, and
`AGENT_ENGINE`). The source compose file remains unchanged under the pinned
checkout.

Create a local API key and a local app. The smoke adapter uses the documented
contracts:

- `POST /api/core/dataset/create`
- `POST /api/core/dataset/collection/create/localFile`
- `POST /api/core/dataset/searchTest`
- `POST /api/v1/chat/completions` with `detail=true`

```bash
python3 -m dify_rag_eval local-smoke \
  --system fastgpt_local \
  --api-key-env FASTGPT_API_KEY \
  --app-id-env FASTGPT_APP_ID \
  --output .local-services/fastgpt_local/logs/smoke
```

Use a new `chatId` for each question and repeat. Save `responseData` and
usage from the raw response; do not turn an answer string into a citation.
If an image manifest lacks ARM64 support, the only permitted fallback is the
current Colima amd64 emulation. Record the resulting platform as
`linux/amd64-emulated`.

The 2026-08-06 run completed provider initialization, root API model setup,
local API-key/app creation, and the three-document ingest/retrieval smoke. Both
`qwen3.6-flash` and `bge-m3` passed FastGPT model tests; all three collections
became ready and `searchTest` returned three hits. Native QA produced no HTTP
response after starting at `2026-08-06T03:44:03Z`, so the final result is
`partial`, with blocked reason
`NATIVE_QA_TIMEOUT_NO_RESPONSE_SINCE_2026-08-06T03:44:03Z`.

Evidence: `.local-services/fastgpt_local/logs/smoke-partial-20260806-114747/`
(`smoke-result.json` SHA-256
`2e66b8fcbdf3da075fabd7ac8532e430f594749391872566f0039679cbb09a96`).
The Compose stack was stopped with `down`; named volumes were retained.

## Prepared local contract (do not start while another competitor is running)

Run the read-only verification at any time. It validates the pinned source,
expanded Compose, every recorded `linux/arm64` image manifest, and confirms no
FastGPT container is running. It does not pull images or mutate containers:

```bash
python3 local-rag-platforms/fastgpt_local/fastgpt_local.py preflight
python3 local-rag-platforms/fastgpt_local/fastgpt_local.py provider
python3 local-rag-platforms/fastgpt_local/fastgpt_local.py smoke
```

The second command is also a dry run: it prints the selected provider's
versioned contract. MaaS uses the active, provider-specific
`maas_provider_contract.json` and prints its effective redacted channel;
the older TaaS/Qianfan template is retained separately as
`legacy_provider_contracts.json` and is never used for MaaS output. The
prepared smoke uses the v4.15.6 source contract, notably:

- local file upload sends a binary `file` plus a JSON-serialized multipart
  `data` field;
- readiness polls `collection/listV2` until all training counters are zero;
- `searchTest` sends `text` and reads matches from `data.list`;
- native QA sends `detail=true` and preserves `choices` and `responseData`.

Copy `.env.example` only into the ignored runtime directory. The populated file
must never be committed:

```bash
cp local-rag-platforms/fastgpt_local/.env.example \
  .local-services/fastgpt_local/fastgpt.env
chmod 600 .local-services/fastgpt_local/fastgpt.env
```

## Provider save failure diagnosis

The two authentication layers have different paths and must not be mixed:

- FastGPT v4.15.4/v4.15.6 UI: root browser session; create
  `POST /api/aiproxy/api/createChannel`, list
  `GET /api/aiproxy/api/channels/all`, and model test
  `GET /api/core/ai/model/test?model=...&channelId=...`.
- AIProxy v0.6.5 management API: `Authorization: Bearer <ADMIN_KEY>`;
  create `POST /api/channel/` (including the trailing slash), list
  `GET /api/channels/all`, and saved-channel test
  `GET /api/channel/{id}/test`.

`TAAS_API_KEY` belongs only in the channel payload's `key`; it does not
authenticate AIProxy management calls. The previous direct
`POST /api/createChannel` attempt therefore exercised no valid AIProxy route.
The v4.15.4 UI and v4.15.6 source contracts agree, so version drift is not the
cause. No backend create failure was observed from the UI attempt because no
request reached the handler.

For Qianfan V2, AIProxy v0.6.5 requires channel type `49`
(`ChannelTypeQianfan`). Type `13` is the legacy Baidu V2 adapter whose key
format is `ak|sk`; type `1` is the generic OpenAI adapter. The runner maps
`QIANFAN_API_KEY` to channel `key` and the optional `QIANFAN_APPID` to
`configs.appid`. On a unique same-name Qianfan channel, `provider --execute`
uses `PUT /api/channel/{id}` to repair the old type/config and refresh the key;
the key is never included in output.
FastGPT also requires model metadata types `llm`, `embedding`, and `rerank`
(plus embedding `weight`). Missing types explain the empty/Unknown model test:
the saved-channel aggregate test cannot supply FastGPT's model type. The
authoritative checks are the three type-aware UI model tests.
For Qianfan and the generic OpenAI-compatible Huawei MaaS channel, the CLI
records an empty saved-channel aggregate as non-authoritative because this
AIProxy release does not return per-model results for those saved channels.
Qianfan records `empty_non_authoritative_use_fastgpt_type_aware_tests`; MaaS
records `empty_non_authoritative_use_fastgpt_native_smoke`. Explicit failed
model results remain errors. For MaaS, the subsequent isolated FastGPT
dataset/native-QA smoke and the direct MaaS provider probe are authoritative.

After MaxKB has been stopped by its owner, execute exactly:

```bash
cd /Users/muuushroom/gitrepos/moi-benchmark/rag
test -z "$(docker ps -q --filter name=maxkb)"
python3 local-rag-platforms/fastgpt_local/fastgpt_local.py preflight
docker compose -p moi_fastgpt_local \
  -f .local-services/fastgpt_local/compose/docker-compose.pg.yml up -d
for attempt in {1..90}; do
  curl -fsS http://127.0.0.1:3000 >/dev/null && break
  sleep 2
done
curl -fsS http://127.0.0.1:3000 >/dev/null
set -a
. .env
set +a
python3 local-rag-platforms/fastgpt_local/fastgpt_local.py provider --execute
```

The provider verifier runs inside `fastgpt-app`, obtains the AIProxy admin key
from the running container without printing it, creates the channel only when
its name is absent, re-lists it, validates type/base URL/models, and tests all
saved models. It refuses to overwrite a mismatched or duplicate channel.

For the Huawei MaaS condition, use the provider-specific contract and keep the
serial competitor window closed until the owner is ready:

```bash
python3 local-rag-platforms/fastgpt_local/fastgpt_local.py provider --provider maas
python3 local-rag-platforms/fastgpt_local/fastgpt_local.py provider --provider maas --execute
```

The MaaS channel contains exactly `glm-5.2` and `bge-m3`; reranking is omitted
unless both `MAAS_ENABLE_RERANKER=1` and an explicit reranker model are set.
The execute path reads only `MAAS_API_KEY`, repairs one exact-name MaaS
channel when needed, and verifies its saved type, base URL, model set, and
model tests without printing credentials.

Sign in locally at `http://127.0.0.1:3000`. In **Account → Model Providers**,
confirm the Huawei MaaS channel and ensure `glm-5.2`/`bge-m3` are enabled.
`maas-models.example.json` is the source contract; do not import the historical
`taas-models.example.json` for this benchmark. Create a local knowledge-base
app wired to the smoke dataset, publish it, and create a local API key. Record
only its app ID and key in the repository-root `.env`.

Add Qianfan as a second AIProxy channel without replacing TaaS. First merge
the three entries from `qianfan-models.example.json` into the existing model
configuration (the JSON update API replaces the whole model configuration),
then create the independent channel:

```bash
python3 local-rag-platforms/fastgpt_local/fastgpt_local.py \
  provider --provider qianfan --execute
```

The requested Qianfan condition uses `deepseek-v4-flash`,
`qwen3-embedding-8b` (4096 dimensions), and `qwen3-reranker-8b`. Treat these
strings as candidate endpoint IDs until runtime type-aware tests confirm the
current account exposes them. Do not reuse a TaaS index. With
`FASTGPT_MODEL_PROVIDER=qianfan`, smoke creates both a timestamped Qianfan
dataset and a new app bound only to that dataset; it ignores generic
`FASTGPT_APP_ID` for native QA.

Then run the prepared three-document contract:

```bash
set -a
. .env
set +a
python3 local-rag-platforms/fastgpt_local/fastgpt_local.py smoke --execute
```

This command refuses non-loopback FastGPT URLs. It creates a timestamped local
dataset, uploads the fixture documents, waits for indexing, runs direct
retrieval and native QA, and writes raw local evidence only below the ignored
`.local-services/fastgpt_local/logs/` directory.

Finally release the serial window while preserving all volumes:

```bash
docker compose -p moi_fastgpt_local \
  -f .local-services/fastgpt_local/compose/docker-compose.pg.yml down
```

## 2026-08-10 PDF full-chain acceptance

- Qianfan channel type `49`; type-aware chat and embedding probes both passed.
- A new dataset was verified as `vectorModel=qwen3-embedding-8b` before upload.
- `qianfan-sentinel.pdf` reached index ready; `searchTest` returned one hit.
- A new app and fresh `chatId` returned one `quoteList` entry and a grounded
  answer containing `FASTGPT_QIANFAN_SENTINEL_20260810_CEDAR_7319`.
- Qianfan reranker returned upstream-load HTTP 500 and remains optional.
- MaaS `/models` and `bge-m3` embedding passed, but all tested chat candidates
  returned `403 ModelArts.81004`; MaaS full-chain was therefore not run.

The redacted manifest is stored at
`.local-services/fastgpt_local/logs/acceptance-qianfan-20260810-165918/manifest.json`.
The stack was stopped without `-v`; five named volumes remain available.
