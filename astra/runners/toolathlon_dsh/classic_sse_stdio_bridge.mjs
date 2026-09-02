#!/usr/bin/env node

/**
 * Toolathlon's Gateway speaks the legacy MCP Classic SSE transport while DSH's
 * built-in MCP client speaks stdio.  This process is deliberately a protocol
 * bridge only: it forwards MCP requests and responses without changing tool
 * names, descriptions, or schemas.
 */

const gatewayUrl = process.argv[2] || process.env.TOOLATHLON_MCP_GATEWAY_URL
const supportedProtocolVersions = new Set([
  '2025-11-25',
  '2025-06-18',
  '2025-03-26',
  '2024-11-05',
  '2024-10-07',
])

class RpcError extends Error {
  constructor(code, message, data) {
    super(message)
    this.name = 'RpcError'
    this.code = code
    this.data = data
  }
}

function fail(message) {
  process.stderr.write(`toolathlon-sse-stdio: ${message}\n`)
  process.exitCode = 1
}

function assertGatewayUrl(value) {
  if (!value) throw new Error('missing Gateway URL')
  const parsed = new URL(value)
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new Error('Gateway URL must use HTTP(S)')
  }
  if (!['127.0.0.1', 'localhost', '[::1]'].includes(parsed.hostname)) {
    throw new Error(`Gateway URL escaped the loopback boundary: ${parsed.hostname}`)
  }
  if (!parsed.pathname.endsWith('/sse')) {
    throw new Error(`Gateway URL must target /sse: ${parsed.pathname}`)
  }
  return parsed
}

function hasId(message) {
  return Object.prototype.hasOwnProperty.call(message, 'id')
}

function parseSseBlock(block) {
  let event = 'message'
  const data = []
  for (const line of block.replace(/\r/g, '').split('\n')) {
    if (line.startsWith(':')) continue
    if (line.startsWith('event:')) {
      event = line.slice(6).trim()
    } else if (line.startsWith('data:')) {
      data.push(line.slice(5).replace(/^ /, ''))
    }
  }
  return { event, data: data.join('\n') }
}

class ClassicSseClient {
  constructor(url) {
    this.url = assertGatewayUrl(url)
    this.abort = new AbortController()
    this.endpoint = undefined
    this.nextId = 0
    this.pending = new Map()
    this.ready = Promise.withResolvers()
    this.readerTask = undefined
    this.closed = false
  }

  async connect() {
    const response = await fetch(this.url, {
      signal: this.abort.signal,
      headers: { Accept: 'text/event-stream' },
    })
    if (!response.ok || response.body === null) {
      throw new Error(`Gateway /sse connection failed: HTTP ${response.status}`)
    }
    this.readerTask = this.readEvents(response.body)
    await this.ready.promise
    await this.request('initialize', {
      protocolVersion: '2024-11-05',
      capabilities: {},
      clientInfo: { name: 'toolathlon-dsh-bridge', version: '1' },
    })
    await this.notify('notifications/initialized', {})
  }

  async readEvents(body) {
    try {
      const reader = body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      while (!this.closed) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        buffer = buffer.replace(/\r\n/g, '\n')
        let boundary
        while ((boundary = buffer.indexOf('\n\n')) >= 0) {
          const block = buffer.slice(0, boundary)
          buffer = buffer.slice(boundary + 2)
          this.handleSse(parseSseBlock(block))
        }
      }
      if (!this.closed) throw new Error('Gateway SSE stream closed')
    } catch (error) {
      if (this.closed) return
      const failure = error instanceof Error ? error : new Error(String(error))
      this.ready.reject(failure)
      for (const waiter of this.pending.values()) waiter.reject(failure)
      this.pending.clear()
    }
  }

  handleSse(message) {
    if (!message.data) return
    if (message.event === 'endpoint') {
      const endpoint = new URL(message.data, this.url)
      if (endpoint.origin !== this.url.origin || endpoint.hostname !== this.url.hostname
        || endpoint.port !== this.url.port || endpoint.username || endpoint.password
        || endpoint.hash) {
        throw new Error('Gateway advertised a cross-origin MCP message endpoint')
      }
      this.endpoint = endpoint
      this.ready.resolve()
      return
    }
    if (message.event !== 'message' && message.event !== 'response') return
    let payload
    try {
      payload = JSON.parse(message.data)
    } catch (error) {
      throw new Error(`Gateway emitted invalid JSON over SSE: ${String(error)}`)
    }
    if (!payload || !hasId(payload)) return
    const waiter = this.pending.get(payload.id)
    if (!waiter) return
    this.pending.delete(payload.id)
    if (payload.error) {
      waiter.reject(new RpcError(
        Number(payload.error.code ?? -32000),
        String(payload.error.message ?? 'Gateway MCP request failed'),
        payload.error.data,
      ))
    } else {
      waiter.resolve(payload.result ?? {})
    }
  }

  async post(message) {
    if (!this.endpoint) throw new Error('MCP endpoint is not ready')
    const response = await fetch(this.endpoint, {
      method: 'POST',
      signal: this.abort.signal,
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify(message),
    })
    if (!response.ok && response.status !== 202) {
      throw new Error(`Gateway MCP POST failed: HTTP ${response.status}`)
    }
    if (response.body) await response.body.cancel()
  }

  async request(method, params) {
    const id = ++this.nextId
    const response = new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject })
    })
    try {
      await this.post({ jsonrpc: '2.0', id, method, params })
    } catch (error) {
      this.pending.delete(id)
      throw error
    }
    return response
  }

  async notify(method, params) {
    await this.post({ jsonrpc: '2.0', method, params })
  }

  close() {
    if (this.closed) return
    this.closed = true
    this.abort.abort()
    const error = new Error('MCP bridge closed')
    for (const waiter of this.pending.values()) waiter.reject(error)
    this.pending.clear()
  }
}

function send(message) {
  process.stdout.write(`${JSON.stringify(message)}\n`)
}

function sendError(id, error) {
  send({
    jsonrpc: '2.0',
    id: id ?? null,
    error: {
      code: error instanceof RpcError ? error.code : -32000,
      message: error instanceof Error ? error.message : String(error),
      ...(error instanceof RpcError && error.data !== undefined ? { data: error.data } : {}),
    },
  })
}

async function handleMessage(client, message) {
  if (!message || message.jsonrpc !== '2.0' || typeof message.method !== 'string') return
  const { id, method, params = {} } = message
  if (method === 'initialize') {
    const requested = params?.protocolVersion
    const protocolVersion = supportedProtocolVersions.has(requested) ? requested : '2024-11-05'
    if (hasId(message)) {
      send({
        jsonrpc: '2.0',
        id,
        result: {
          protocolVersion,
          capabilities: { tools: { listChanged: false } },
          serverInfo: { name: 'toolathlon-classic-sse-bridge', version: '1' },
        },
      })
    }
    return
  }
  if (method === 'notifications/initialized') return
  if (method === 'notifications/cancelled') return
  if (!hasId(message)) {
    try {
      await client.notify(method, params)
    } catch {
      // Notifications have no response channel; the next request will expose
      // a disconnected bridge if the Gateway has gone away.
    }
    return
  }
  try {
    if (method !== 'tools/list' && method !== 'tools/call' && method !== 'ping') {
      throw new RpcError(-32601, `Unsupported MCP method: ${method}`)
    }
    const result = await client.request(method, params)
    send({ jsonrpc: '2.0', id, result })
  } catch (error) {
    sendError(id, error)
  }
}

async function main() {
  const client = new ClassicSseClient(gatewayUrl)
  await client.connect()
  let buffer = ''
  process.stdin.on('data', chunk => {
    buffer += chunk.toString('utf8')
    let newline
    while ((newline = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, newline).replace(/\r$/, '')
      buffer = buffer.slice(newline + 1)
      if (!line.trim()) continue
      try {
        const message = JSON.parse(line)
        void handleMessage(client, message).catch(error => {
          if (message && hasId(message)) sendError(message.id, error)
        })
      } catch (error) {
        process.stderr.write(`toolathlon-sse-stdio: invalid stdio JSON: ${String(error)}\n`)
      }
    }
  })
  process.stdin.on('end', () => client.close())
  const shutdown = () => {
    client.close()
    process.stdin.pause()
    process.exit(0)
  }
  process.on('SIGTERM', shutdown)
  process.on('SIGINT', shutdown)
}

main().catch(fail)
