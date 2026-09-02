import crypto from 'node:crypto'
import diagnosticsChannel from 'node:diagnostics_channel'
import fs from 'node:fs'
import http from 'node:http'
import path from 'node:path'

const CHAT_PATHS = new Set([
  '/chat/completions',
  '/v1/chat/completions',
  '/responses',
  '/v1/responses',
])
const MODEL_PATHS = new Set(['/models', '/v1/models'])
const DEFAULTED_GENERATION_FIELDS = new Set([
  'top_p',
  'presence_penalty',
  'frequency_penalty',
  'max_tokens',
  'max_completion_tokens',
  'reasoning_effort',
  'thinking',
  'seed',
  'logprobs',
  'top_logprobs',
  'tool_choice',
  'parallel_tool_calls',
])
const MAX_REQUEST_BYTES = 64 * 1024 * 1024
const MAX_PROBE_BYTES = 2 * 1024 * 1024
const MAX_DIAGNOSTIC_TEXT = 1000

let connectAttempts = 0
diagnosticsChannel.channel('undici:client:beforeConnect').subscribe(() => {
  connectAttempts += 1
})

function fail(message) {
  throw new Error(message)
}

function loadConfig() {
  const raw = fs.readFileSync(0, 'utf8')
  let value
  try {
    value = JSON.parse(raw)
  } catch (error) {
    throw new Error(`invalid Node model proxy configuration: ${error.message}`)
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    fail('Node model proxy configuration must be an object')
  }
  const requiredStrings = [
    'listen_host',
    'upstream_base_url',
    'upstream_api_key',
    'effective_model',
    'thinking',
    'reasoning_effort',
    'run_id',
    'system_id',
    'events_path',
    'state_path',
    'provider_credential_environment',
    'provider_credential_fingerprint',
    'provider_user_id',
  ]
  for (const field of requiredStrings) {
    if (typeof value[field] !== 'string' || !value[field]) {
      fail(`Node model proxy configuration has no ${field}`)
    }
  }
  if (value.listen_host !== '127.0.0.1') {
    fail('Node model proxy must listen on IPv4 loopback')
  }
  if (!Number.isInteger(value.listen_port) || value.listen_port < 0 || value.listen_port > 65535) {
    fail('Node model proxy has an invalid listen port')
  }
  const upstream = new URL(value.upstream_base_url)
  if (!['http:', 'https:'].includes(upstream.protocol) || !upstream.hostname) {
    fail('Node model proxy upstream must be an http(s) URL')
  }
  if (upstream.username || upstream.password || upstream.search || upstream.hash) {
    fail('Node model proxy upstream URL contains forbidden components')
  }
  if (value.effective_model !== 'deepseek-v4-flash') {
    fail('Node model proxy must freeze deepseek-v4-flash')
  }
  if (value.temperature !== 0 || value.thinking !== 'enabled' || value.reasoning_effort !== 'max') {
    fail('Node model proxy generation settings do not match the freeze')
  }
  if (value.max_requests !== 100) {
    fail('Node model proxy must freeze max_requests=100')
  }
  if (!path.isAbsolute(value.events_path) || !path.isAbsolute(value.state_path)) {
    fail('Node model proxy artifact paths must be absolute')
  }
  const fingerprint = `sha256:${sha256(value.upstream_api_key)}`
  if (fingerprint !== value.provider_credential_fingerprint) {
    fail('Node model proxy credential fingerprint mismatch')
  }
  return { ...value, upstream }
}

function stableStringify(value) {
  if (Array.isArray(value)) {
    return `[${value.map(stableStringify).join(',')}]`
  }
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) => (
      `${JSON.stringify(key)}:${stableStringify(value[key])}`
    )).join(',')}}`
  }
  return JSON.stringify(value)
}

function sha256(value) {
  return crypto.createHash('sha256').update(
    Buffer.isBuffer(value) ? value : String(value),
  ).digest('hex')
}

function canonicalSha256(value) {
  return sha256(stableStringify(value))
}

function utcNow() {
  return new Date().toISOString()
}

function missingObservation(source, reason) {
  return {
    value: null,
    source,
    reliability: 'missing',
    missing_reason: reason,
  }
}

function reportedObservation(value, source) {
  return {
    value,
    source,
    reliability: 'reported',
    missing_reason: null,
  }
}

function retryObservation() {
  return missingObservation('product_event', 'product_retry_relation_not_exposed')
}

function reportedToken(usage, paths) {
  if (usage && typeof usage === 'object' && !Array.isArray(usage)) {
    for (const keys of paths) {
      let value = usage
      for (const key of keys) {
        if (!value || typeof value !== 'object' || Array.isArray(value) || !(key in value)) {
          value = null
          break
        }
        value = value[key]
      }
      if (Number.isInteger(value) && value >= 0) {
        return reportedObservation(value, `provider_response.${keys.join('.')}`)
      }
    }
  }
  return missingObservation('provider_response', 'provider_not_reported')
}

function tokenUsageObservations(usage, missingReason = 'provider_not_reported') {
  if (usage === null && missingReason !== 'provider_not_reported') {
    const missing = missingObservation('provider_response', missingReason)
    return {
      input_tokens: { ...missing },
      output_tokens: { ...missing },
      cache_read_tokens: { ...missing },
      cache_write_tokens: { ...missing },
      total_tokens: { ...missing },
    }
  }
  return {
    input_tokens: reportedToken(usage, [['prompt_tokens'], ['input_tokens']]),
    output_tokens: reportedToken(usage, [['completion_tokens'], ['output_tokens']]),
    cache_read_tokens: reportedToken(usage, [
      ['cache_read_tokens'],
      ['prompt_cache_hit_tokens'],
      ['prompt_tokens_details', 'cached_tokens'],
    ]),
    cache_write_tokens: reportedToken(usage, [
      ['cache_write_tokens'],
      ['prompt_cache_write_tokens'],
    ]),
    total_tokens: reportedToken(usage, [['total_tokens']]),
  }
}

function finishReasonObservation(finishReasons) {
  return finishReasons.length
    ? reportedObservation(finishReasons, 'provider_response.choices.finish_reason')
    : missingObservation('provider_response', 'provider_not_reported')
}

function requestToolNames(body) {
  if (!Array.isArray(body.tools)) return []
  const names = []
  for (const tool of body.tools) {
    if (!tool || typeof tool !== 'object' || Array.isArray(tool)) continue
    const name = tool.function && typeof tool.function === 'object'
      ? tool.function.name
      : tool.name
    if (typeof name === 'string' && name) names.push(name)
  }
  return [...new Set(names)].sort()
}

function normalizeRequest(body, config) {
  const normalized = { ...body }
  const removedGeneration = []
  const removedIdentity = []
  for (const key of [...DEFAULTED_GENERATION_FIELDS].sort()) {
    if (key in normalized) {
      delete normalized[key]
      removedGeneration.push(key)
    }
  }
  if ('user_id' in normalized) {
    delete normalized.user_id
    removedIdentity.push('user_id')
  }
  if (normalized.extra_body && typeof normalized.extra_body === 'object' && !Array.isArray(normalized.extra_body)) {
    const extra = { ...normalized.extra_body }
    for (const key of [...DEFAULTED_GENERATION_FIELDS].sort()) {
      if (key in extra) {
        delete extra[key]
        removedGeneration.push(`extra_body.${key}`)
      }
    }
    if ('user_id' in extra) {
      delete extra.user_id
      removedIdentity.push('extra_body.user_id')
    }
    if (Object.keys(extra).length) normalized.extra_body = extra
    else delete normalized.extra_body
  }
  normalized.model = config.effective_model
  normalized.temperature = config.temperature
  normalized.user_id = config.provider_user_id
  normalized.thinking = { type: config.thinking }
  normalized.reasoning_effort = config.reasoning_effort
  return { normalized, removedGeneration, removedIdentity }
}

function diagnosticText(value, secret) {
  return String(value).split(secret).join('[REDACTED]').slice(0, MAX_DIAGNOSTIC_TEXT)
}

function exceptionDiagnostics(error, source, secret) {
  const cause = error && typeof error.cause === 'object' ? error.cause : null
  const result = {
    source,
    type: error?.constructor?.name || error?.name || 'Error',
    message: diagnosticText(error?.message || error, secret),
  }
  for (const field of ['code', 'errno', 'syscall']) {
    const value = cause?.[field] ?? error?.[field]
    if (typeof value === 'string' || typeof value === 'number') result[field] = value
  }
  if (cause) {
    result.cause_type = cause.constructor?.name || cause.name || 'Error'
    result.cause_message = diagnosticText(cause.message || cause, secret)
  }
  const socket = cause?.socket
  if (socket && typeof socket === 'object') {
    result.socket = {}
    for (const field of [
      'localAddress',
      'localPort',
      'remoteAddress',
      'remotePort',
      'remoteFamily',
      'bytesWritten',
      'bytesRead',
    ]) {
      if (typeof socket[field] === 'string' || typeof socket[field] === 'number') {
        result.socket[field] = socket[field]
      }
    }
  }
  return result
}

function providerErrorDiagnostics(error, secret) {
  if (!error || typeof error !== 'object' || Array.isArray(error)) return null
  const result = {}
  for (const field of ['code', 'type', 'param', 'message']) {
    const value = error[field]
    if (typeof value === 'string') result[field] = diagnosticText(value, secret)
    else if (typeof value === 'number' || typeof value === 'boolean') result[field] = value
  }
  return Object.keys(result).length ? result : null
}

class UsageProbe {
  constructor() {
    this.tail = Buffer.alloc(0)
  }

  feed(chunk) {
    const value = Buffer.from(chunk)
    this.tail = Buffer.concat([this.tail, value])
    if (this.tail.length > MAX_PROBE_BYTES) {
      this.tail = this.tail.subarray(this.tail.length - MAX_PROBE_BYTES)
    }
  }

  metadata() {
    const text = this.tail.toString('utf8')
    const candidates = []
    try {
      const value = JSON.parse(text)
      if (value && typeof value === 'object' && !Array.isArray(value)) candidates.push(value)
    } catch {
      for (const line of text.split(/\r?\n/)) {
        if (!line.startsWith('data:')) continue
        const payload = line.slice(5).trim()
        if (!payload || payload === '[DONE]') continue
        try {
          const value = JSON.parse(payload)
          if (value && typeof value === 'object' && !Array.isArray(value)) candidates.push(value)
        } catch {
          // A bounded probe may begin in the middle of an older SSE event.
        }
      }
    }
    let usage = null
    let providerResponseId = null
    let providerError = null
    const finishReasons = []
    for (const value of candidates) {
      if (typeof value.id === 'string' && value.id) providerResponseId = value.id
      if (value.error && typeof value.error === 'object' && !Array.isArray(value.error)) {
        providerError = value.error
      }
      if (Array.isArray(value.choices)) {
        for (const choice of value.choices) {
          const reason = choice && typeof choice === 'object' ? choice.finish_reason : null
          if (typeof reason === 'string' && !finishReasons.includes(reason)) finishReasons.push(reason)
        }
      }
    }
    for (let index = candidates.length - 1; index >= 0; index -= 1) {
      const candidate = candidates[index].usage
      if (candidate && typeof candidate === 'object' && !Array.isArray(candidate)) {
        usage = candidate
        break
      }
    }
    return { usage, providerResponseId, providerError, finishReasons }
  }
}

function writeJsonAtomic(destination, value) {
  fs.mkdirSync(path.dirname(destination), { recursive: true })
  const temporary = path.join(
    path.dirname(destination),
    `.${path.basename(destination)}.${process.pid}.${crypto.randomBytes(8).toString('hex')}`,
  )
  try {
    const descriptor = fs.openSync(temporary, 'wx', 0o600)
    try {
      fs.writeFileSync(descriptor, `${JSON.stringify(value, null, 2)}\n`, 'utf8')
      fs.fsyncSync(descriptor)
    } finally {
      fs.closeSync(descriptor)
    }
    fs.renameSync(temporary, destination)
  } finally {
    try {
      fs.unlinkSync(temporary)
    } catch (error) {
      if (error.code !== 'ENOENT') throw error
    }
  }
}

function createAudit(config, budget) {
  fs.mkdirSync(path.dirname(config.events_path), { recursive: true })
  try {
    if (fs.statSync(config.events_path).size) {
      fail('model usage event file must start empty')
    }
  } catch (error) {
    if (error.code !== 'ENOENT') throw error
  }
  const descriptor = fs.openSync(config.events_path, 'a', 0o600)
  const writeState = () => writeJsonAtomic(config.state_path, {
    schema_version: 1,
    run_id: config.run_id,
    system_id: config.system_id,
    updated_at: utcNow(),
    provider_identity: {
      credential_environment: config.provider_credential_environment,
      credential_fingerprint: config.provider_credential_fingerprint,
      user_id: config.provider_user_id,
    },
    budget: budget.snapshot(),
  })
  const append = (event, fields = {}) => {
    const row = {
      schema_version: 'toolathlon.model-proxy.events.v1',
      timestamp: utcNow(),
      monotonic_ns: Number(process.hrtime.bigint()),
      run_id: config.run_id,
      system_id: config.system_id,
      event,
      ...fields,
    }
    fs.writeSync(descriptor, `${stableStringify(row)}\n`, null, 'utf8')
    fs.fsyncSync(descriptor)
    writeState()
  }
  writeState()
  return { append, close: () => fs.closeSync(descriptor) }
}

function createBudget(maximum) {
  const state = {
    maximum,
    attempted: 0,
    forwarded: 0,
    completed: 0,
    failed: 0,
    limitRejections: 0,
    exceeded: false,
  }
  return {
    reserve() {
      state.attempted += 1
      if (state.forwarded >= state.maximum) {
        state.limitRejections += 1
        state.exceeded = true
        return { admitted: false, productAttempt: state.attempted, providerRequest: state.forwarded }
      }
      state.forwarded += 1
      return { admitted: true, productAttempt: state.attempted, providerRequest: state.forwarded }
    },
    finish(providerRequest, success) {
      state.completed += 1
      if (!success) state.failed += 1
      if (providerRequest >= state.maximum) state.exceeded = true
    },
    snapshot() {
      return {
        max_requests: state.maximum,
        product_attempts: state.attempted,
        provider_requests_forwarded: state.forwarded,
        provider_requests_completed: state.completed,
        provider_requests_failed: state.failed,
        limit_rejections: state.limitRejections,
        limit_exceeded: state.exceeded,
      }
    },
  }
}

function sendJson(response, status, value) {
  if (response.destroyed || response.headersSent) return
  const payload = Buffer.from(JSON.stringify(value))
  response.writeHead(status, {
    'Content-Type': 'application/json',
    'Content-Length': String(payload.length),
    Connection: 'close',
  })
  response.end(payload)
}

async function readRequestBody(request, length) {
  const chunks = []
  let total = 0
  for await (const chunk of request) {
    total += chunk.length
    if (total > length || total > MAX_REQUEST_BYTES) fail('request body exceeds Content-Length')
    chunks.push(chunk)
  }
  if (total !== length) fail('request body does not match Content-Length')
  return Buffer.concat(chunks)
}

class DownstreamDisconnectedError extends Error {}

function writeChunk(response, chunk) {
  if (response.destroyed) throw new DownstreamDisconnectedError('downstream disconnected')
  if (response.write(chunk)) return Promise.resolve()
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      response.off('drain', onDrain)
      response.off('close', onClose)
      response.off('error', onError)
    }
    const onDrain = () => {
      cleanup()
      resolve()
    }
    const onClose = () => {
      cleanup()
      reject(new DownstreamDisconnectedError('downstream disconnected'))
    }
    const onError = (error) => {
      cleanup()
      reject(error)
    }
    response.once('drain', onDrain)
    response.once('close', onClose)
    response.once('error', onError)
  })
}

function endResponse(response) {
  if (response.destroyed) throw new DownstreamDisconnectedError('downstream disconnected')
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      response.off('finish', onFinish)
      response.off('close', onClose)
      response.off('error', onError)
    }
    const onFinish = () => {
      cleanup()
      resolve()
    }
    const onClose = () => {
      cleanup()
      reject(new DownstreamDisconnectedError('downstream disconnected'))
    }
    const onError = (error) => {
      cleanup()
      reject(error)
    }
    response.once('finish', onFinish)
    response.once('close', onClose)
    response.once('error', onError)
    response.end()
  })
}

function upstreamTarget(config, requestPath) {
  let normalizedPath = requestPath
  if (normalizedPath.startsWith('/v1/')) normalizedPath = normalizedPath.slice(3)
  const prefix = config.upstream.pathname.replace(/\/$/, '')
  const target = new URL(config.upstream.href)
  target.pathname = `${prefix}${normalizedPath}` || '/'
  target.search = ''
  target.hash = ''
  return target
}

async function forward(config, requestPath, body, response, diagnostics, probe) {
  const controller = new AbortController()
  let downstreamDisconnected = false
  const onDownstreamClose = () => {
    if (!response.writableEnded) {
      downstreamDisconnected = true
      controller.abort(new DownstreamDisconnectedError('downstream disconnected'))
    }
  }
  response.once('close', onDownstreamClose)
  const connectsBefore = connectAttempts
  try {
    diagnostics.phase = 'send_upstream_request'
    const upstream = await fetch(upstreamTarget(config, requestPath), {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${config.upstream_api_key}`,
        'Content-Type': 'application/json',
        Accept: 'text/event-stream, application/json',
        'Accept-Encoding': 'identity',
        'User-Agent': 'toolathlon-dsh-evaluation/1',
      },
      body: JSON.stringify(body),
      signal: controller.signal,
    })
    diagnostics.connection_reused = connectAttempts === connectsBefore
    diagnostics.upstream_connect_attempts = connectAttempts - connectsBefore
    diagnostics.phase = 'read_upstream_headers'
    diagnostics.upstream_http_status = upstream.status
    diagnostics.upstream_content_type = upstream.headers.get('content-type') || 'application/json'
    const contentLength = upstream.headers.get('content-length')
    diagnostics.upstream_content_length = /^\d+$/.test(contentLength || '')
      ? Number(contentLength)
      : null
    diagnostics.upstream_chunked = (upstream.headers.get('transfer-encoding') || '')
      .toLowerCase().includes('chunked')
    diagnostics.upstream_will_close = (upstream.headers.get('connection') || '')
      .toLowerCase().includes('close')
    diagnostics.provider_request_id = upstream.headers.get('x-request-id')
      || upstream.headers.get('x-ds-request-id')

    diagnostics.phase = 'write_downstream_headers'
    const headers = {
      'Content-Type': diagnostics.upstream_content_type,
      'Cache-Control': 'no-store',
    }
    if (diagnostics.upstream_content_length !== null) {
      headers['Content-Length'] = String(diagnostics.upstream_content_length)
    }
    response.writeHead(upstream.status, headers)
    diagnostics.downstream_response_started = true

    diagnostics.phase = 'read_upstream_body'
    if (upstream.body) {
      const reader = upstream.body.getReader()
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        diagnostics.upstream_body_bytes_read += value.byteLength
        probe.feed(value)
        diagnostics.phase = 'write_downstream_body'
        await writeChunk(response, Buffer.from(value))
        diagnostics.phase = 'read_upstream_body'
      }
    }
    diagnostics.phase = 'write_downstream_terminator'
    await endResponse(response)
    diagnostics.phase = 'complete'
    return upstream.status
  } catch (error) {
    if (!controller.signal.aborted) controller.abort(error)
    if (downstreamDisconnected && !(error instanceof DownstreamDisconnectedError)) {
      throw new DownstreamDisconnectedError('downstream disconnected', { cause: error })
    }
    throw error
  } finally {
    response.off('close', onDownstreamClose)
  }
}

function transportDiagnostics() {
  return {
    transport: 'node_fetch_undici',
    node_version: process.version,
    undici_version: process.versions.undici || null,
    phase: 'not_started',
    connection_reused: null,
    upstream_connect_attempts: 0,
    upstream_http_status: null,
    upstream_content_type: null,
    upstream_content_length: null,
    upstream_chunked: null,
    upstream_will_close: null,
    upstream_body_bytes_read: 0,
    downstream_response_started: false,
    provider_request_id: null,
    provider_response_id: null,
    provider_error: null,
    exception: null,
  }
}

async function handlePost(request, response, requestPath, config, budget, audit) {
  const rawLength = request.headers['content-length']
  const length = typeof rawLength === 'string' && /^\d+$/.test(rawLength)
    ? Number(rawLength)
    : NaN
  if (!Number.isSafeInteger(length)) {
    sendJson(response, 400, { error: { code: 'invalid_content_length' } })
    return
  }
  if (length <= 0 || length > MAX_REQUEST_BYTES) {
    sendJson(response, 413, { error: { code: 'invalid_request_size' } })
    return
  }
  let body
  try {
    body = JSON.parse((await readRequestBody(request, length)).toString('utf8'))
  } catch {
    sendJson(response, 400, { error: { code: 'invalid_json' } })
    return
  }
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    sendJson(response, 400, { error: { code: 'request_must_be_object' } })
    return
  }

  const reservation = budget.reserve()
  const requestedModel = body.model ?? null
  if (!reservation.admitted) {
    const modelRequestId = `${config.run_id}:model-attempt:${reservation.productAttempt}`
    audit.append('model_request.rejected_limit', {
      model_request_id: modelRequestId,
      product_attempt: reservation.productAttempt,
      provider_requests_forwarded: reservation.providerRequest,
      requested_model: requestedModel,
      effective_model: config.effective_model,
      retry_of: retryObservation(),
      finish_reason: missingObservation('provider_response', 'request_not_forwarded'),
      token_usage: tokenUsageObservations(null, 'request_not_forwarded'),
      raw_provider_usage: missingObservation('provider_response', 'request_not_forwarded'),
    })
    sendJson(response, 429, {
      error: {
        type: 'benchmark_budget_exceeded',
        code: 'max_model_requests',
        message: 'frozen product model request budget exhausted',
      },
    })
    return
  }

  const { normalized, removedGeneration, removedIdentity } = normalizeRequest(body, config)
  const toolNames = requestToolNames(normalized)
  const modelRequestId = `${config.run_id}:model:${reservation.providerRequest}`
  audit.append('model_request.started', {
    model_request_id: modelRequestId,
    product_attempt: reservation.productAttempt,
    provider_request: reservation.providerRequest,
    requested_model: requestedModel,
    effective_model: config.effective_model,
    temperature_sent: config.temperature,
    temperature_effective: false,
    thinking: config.thinking,
    thinking_wire_behavior: 'sent',
    reasoning_effort: config.reasoning_effort,
    reasoning_effort_wire_behavior: 'sent',
    generation_parameter_source: 'benchmark_override',
    provider_user_id: config.provider_user_id,
    removed_generation_parameters: removedGeneration,
    removed_identity_parameters: removedIdentity,
    request_sha256: canonicalSha256(normalized),
    request_tool_count: toolNames.length,
    request_tool_names: toolNames,
    request_tool_names_sha256: canonicalSha256(toolNames),
    retry_of: retryObservation(),
    retry_relation_reliability: 'not_exposed_by_product',
    stream: Boolean(normalized.stream),
  })

  const started = process.hrtime.bigint()
  const diagnostics = transportDiagnostics()
  const probe = new UsageProbe()
  let status = null
  let success = false
  let errorType = null
  try {
    status = await forward(config, requestPath, normalized, response, diagnostics, probe)
    success = status >= 200 && status < 300
  } catch (error) {
    status = diagnostics.upstream_http_status
    const source = error instanceof DownstreamDisconnectedError ? 'downstream' : 'upstream'
    diagnostics.exception = exceptionDiagnostics(error, source, config.upstream_api_key)
    errorType = source === 'downstream'
      ? 'downstream_disconnected'
      : diagnostics.exception.code || diagnostics.exception.type
    if (source === 'upstream') {
      response.destroy(error)
    }
  } finally {
    const metadata = probe.metadata()
    diagnostics.provider_response_id = metadata.providerResponseId
    diagnostics.provider_error = providerErrorDiagnostics(
      metadata.providerError,
      config.upstream_api_key,
    )
    budget.finish(reservation.providerRequest, success)
    const usage = metadata.usage
    const providerResponseId = metadata.providerResponseId
    const providerHeaderRequestId = diagnostics.provider_request_id
    const durationSeconds = Number(process.hrtime.bigint() - started) / 1e9
    audit.append('model_request.completed', {
      model_request_id: modelRequestId,
      product_attempt: reservation.productAttempt,
      provider_request: reservation.providerRequest,
      http_status: status,
      success,
      duration_seconds: Number(durationSeconds.toFixed(6)),
      provider_response_id: providerResponseId
        ? reportedObservation(providerResponseId, 'provider_response.id')
        : missingObservation('provider_response', 'provider_not_reported'),
      provider_header_request_id: providerHeaderRequestId
        ? reportedObservation(providerHeaderRequestId, 'provider_response.headers.request_id')
        : missingObservation('provider_response', 'provider_not_reported'),
      finish_reasons: metadata.finishReasons,
      finish_reason: finishReasonObservation(metadata.finishReasons),
      usage: usage !== null
        ? reportedObservation(usage, 'provider_response.usage')
        : missingObservation('provider_response', 'provider_not_reported'),
      raw_provider_usage: usage !== null
        ? reportedObservation(usage, 'provider_response.usage')
        : missingObservation('provider_response', 'provider_not_reported'),
      usage_reliability: usage !== null ? 'reported' : 'missing',
      token_usage: tokenUsageObservations(usage),
      retry_of: retryObservation(),
      transport_diagnostics: diagnostics,
      error_type: errorType
        ? reportedObservation(errorType, 'model_proxy')
        : missingObservation('model_proxy', 'no_transport_error'),
    })
  }
}

async function main() {
  const config = loadConfig()
  const budget = createBudget(config.max_requests)
  const audit = createAudit(config, budget)
  const server = http.createServer((request, response) => {
    response.on('error', () => {})
    const requestPath = new URL(request.url || '/', 'http://127.0.0.1').pathname
    if (request.method === 'GET') {
      if (requestPath === '/health' || requestPath === '/ready') {
        sendJson(response, 200, {
          status: 'ok',
          model: config.effective_model,
          budget: budget.snapshot(),
        })
      } else if (MODEL_PATHS.has(requestPath)) {
        sendJson(response, 200, {
          object: 'list',
          data: [{ id: config.effective_model, object: 'model', owned_by: 'deepseek' }],
        })
      } else {
        sendJson(response, 404, { error: { code: 'proxy_path_not_allowed' } })
      }
      return
    }
    if (request.method !== 'POST' || !CHAT_PATHS.has(requestPath)) {
      sendJson(response, 404, { error: { code: 'proxy_path_not_allowed' } })
      return
    }
    handlePost(request, response, requestPath, config, budget, audit).catch((error) => {
      console.error(error?.stack || error)
      if (!response.headersSent) sendJson(response, 500, { error: { code: 'model_proxy_internal_error' } })
      else response.destroy(error)
    })
  })
  server.keepAliveTimeout = 60_000
  server.headersTimeout = 310_000

  await new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(config.listen_port, config.listen_host, resolve)
  })
  const address = server.address()
  if (!address || typeof address === 'string') fail('Node model proxy has no TCP address')
  const listenUrl = `http://${config.listen_host}:${address.port}/v1`
  audit.append('proxy.ready', {
    listen_url: listenUrl,
    effective_model: config.effective_model,
    upstream_origin: config.upstream.host,
    provider_credential_environment: config.provider_credential_environment,
    provider_credential_fingerprint: config.provider_credential_fingerprint,
    provider_user_id: config.provider_user_id,
    transport: 'node_fetch_undici',
    node_version: process.version,
    undici_version: process.versions.undici || null,
  })
  process.stdout.write(`${JSON.stringify({
    event: 'ready',
    url: listenUrl,
    node_version: process.version,
    undici_version: process.versions.undici || '',
  })}\n`)

  let stopping = false
  const stop = () => {
    if (stopping) return
    stopping = true
    const timer = setTimeout(() => server.closeAllConnections(), 5_000)
    timer.unref()
    server.close(() => {
      clearTimeout(timer)
      audit.append('proxy.stopped')
      audit.close()
      process.exit(0)
    })
    server.closeIdleConnections()
  }
  process.on('SIGTERM', stop)
  process.on('SIGINT', stop)
}

main().catch((error) => {
  console.error(error?.stack || error)
  process.exitCode = 1
})
