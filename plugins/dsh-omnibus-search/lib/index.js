/**
 * dsh-omnibus-search — host half.
 *
 * 注册一条 fenced 的 `/omnia/api` 前缀路由，把浏览器的搜索请求转发到本机的
 * melquiades HTTP 桥（`melquiades-http`，默认 127.0.0.1:9010）。
 *
 * ── 为什么是「转发」而不是「插件自己查库」──
 * 检索逻辑只有一份：bigram 全文 / LIKE / 向量三路候选 + RRF 融合 + 归一化排序，
 * 全在 melquiades 的 omnibus.py。让 Node 直连 MariaDB 就等于把它重写一遍，
 * 两份实现必然漂移（本仓真实踩过「数据巴别塔」：同一字段两种序列化共存两年、
 * 15 万条数据要迁移）。所以这里只做三件事：**校验来源、转发、透传错误**。
 *
 * ── 安全边界 ──
 * 和 dsh-fulltext-search 同一套 fence：Host 必须是回环或信任域，`sec-fetch-site`
 * 不能是 cross-site，Origin（若有）必须同源。桥本身也只绑 127.0.0.1 并校验 Host，
 * 两层都挡住 DNS rebinding。
 *
 * ── 三层开关 ──
 * ① cordis.patch.yml 里整行注释/删除 → 插件不加载
 * ② config.enabled=false → 本文件不注册任何路由
 * ③ better-sidebar 缺失 → client 侧不注册 tab（host 路由仍在，可直接 curl 用）
 */

const DEFAULT_ENDPOINT = 'http://127.0.0.1:9010'
const DEFAULT_TIMEOUT_MS = 15000
const DEFAULT_LIMIT = 5
const DEFAULT_MODES = 'vector,fulltext,like'
const MAX_BODY_BYTES = 1 << 20

export const name = 'dsh-omnibus-search'
export const inject = ['webServer', 'webRuntime']

/** 读请求体（带上限，避免被本地大 payload 打爆内存）。 */
function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = []
    let size = 0
    req.on('data', (chunk) => {
      size += chunk.length
      if (size > MAX_BODY_BYTES) {
        reject(new Error(`请求体超过 ${MAX_BODY_BYTES} 字节`))
        req.destroy()
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')))
    req.on('error', reject)
  })
}

function sendJson(res, code, obj) {
  const body = JSON.stringify(obj)
  res.writeHead(code, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': Buffer.byteLength(body),
    'cache-control': 'no-store',
  })
  res.end(body)
}

function isLoopbackName(hostname) {
  return hostname === '127.0.0.1' || hostname === 'localhost' || hostname === '::1' || hostname === '[::1]'
}

/**
 * 来源栅栏：只接受回环/信任域发起的同源请求。
 * 逻辑与 dsh-fulltext-search 的 isTrustedApiRequest 一致（服务端看不到「谁点的」，
 * 只能靠 Host / Origin / Sec-Fetch-Site 三个头推断）。
 */
function isTrustedRequest(req, trustedHosts) {
  const hostHeader = req.headers.host
  if (!hostHeader) return false
  const [hostName, hostPort] = hostHeader.split(':')
  const isLoopback = isLoopbackName(hostName)
  const isTrusted = isLoopback || (Array.isArray(trustedHosts) && trustedHosts.includes(hostHeader))
  if (!isTrusted) return false

  if (req.headers['sec-fetch-site'] === 'cross-site') return false

  const origin = req.headers.origin
  if (origin === undefined) return true
  try {
    const originUrl = new URL(origin)
    return originUrl.host === (hostPort ? `${hostName}:${hostPort}` : hostName)
  } catch {
    return false
  }
}

/**
 * 配置校验：非法配置**加载期就响亮失败**，不做静默降级。
 *
 * 这里手写而不是用 Schemastery：本插件是无构建的纯 ESM（`dsh plugin add link:`
 * 直接吃源码），多一个 peer 依赖就多一处「装不上」的可能，而这份配置只有 6 个字段、
 * 校验规则一屏能看完。校验强度与 Schemastery 等价：类型不对就 throw。
 */
function normalizeConfig(raw) {
  const cfg = raw ?? {}
  if (typeof cfg !== 'object' || Array.isArray(cfg)) {
    throw new Error('[omnibus-search] config 必须是对象')
  }

  const enabled = cfg.enabled === undefined ? true : cfg.enabled
  if (typeof enabled !== 'boolean') {
    throw new Error('[omnibus-search] config.enabled 必须是 boolean')
  }

  const endpoint = cfg.endpoint === undefined ? DEFAULT_ENDPOINT : String(cfg.endpoint)
  let parsed
  try {
    parsed = new URL(endpoint)
  } catch {
    throw new Error(`[omnibus-search] config.endpoint 不是合法 URL: ${endpoint}`)
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new Error('[omnibus-search] config.endpoint 只支持 http/https')
  }
  // 只允许本机：这个桥是只读的，但也别把 DSH 变成一个对外打洞的代理
  if (!isLoopbackName(parsed.hostname)) {
    throw new Error(`[omnibus-search] config.endpoint 必须指向本机回环，当前是 ${parsed.hostname}`)
  }

  const timeoutMs = cfg.timeoutMs === undefined ? DEFAULT_TIMEOUT_MS : Number(cfg.timeoutMs)
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
    throw new Error('[omnibus-search] config.timeoutMs 必须是正数')
  }

  const defaultLimit = cfg.defaultLimit === undefined ? DEFAULT_LIMIT : Number(cfg.defaultLimit)
  if (!Number.isInteger(defaultLimit) || defaultLimit < 1 || defaultLimit > 50) {
    throw new Error('[omnibus-search] config.defaultLimit 必须是 1..50 的整数')
  }

  const defaultModes = cfg.defaultModes === undefined ? DEFAULT_MODES : String(cfg.defaultModes)
  const validModes = new Set(['vector', 'fulltext', 'like'])
  const modes = String(defaultModes)
    .split(',')
    .map((m) => m.trim())
    .filter(Boolean)
  if (modes.length === 0 || modes.some((m) => !validModes.has(m))) {
    throw new Error(`[omnibus-search] config.defaultModes 只接受 vector/fulltext/like，当前是 ${defaultModes}`)
  }

  return {
    enabled,
    endpoint: parsed.origin + parsed.pathname.replace(/\/$/, ''),
    timeoutMs,
    defaultLimit,
    defaultModes: modes.join(','),
  }
}

/** 把浏览器的请求转发给桥；桥的 JSON 原样透传（错误也是）。 */
async function forward(cfg, path, payload) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), cfg.timeoutMs)
  try {
    const resp = await fetch(`${cfg.endpoint}${path}`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload),
      signal: controller.signal,
    })
    const text = await resp.text()
    let data
    try {
      data = JSON.parse(text)
    } catch {
      // 桥返回了非 JSON：原样报出来，不要伪装成「没有结果」
      return { code: 502, body: { ok: false, error: `桥返回非 JSON（HTTP ${resp.status}）: ${text.slice(0, 300)}` } }
    }
    return { code: resp.status, body: data }
  } catch (err) {
    if (err && err.name === 'AbortError') {
      return {
        code: 504,
        body: { ok: false, error: `搜索桥超时（${cfg.timeoutMs}ms）—— 桥在 ${cfg.endpoint} 上吗？` },
      }
    }
    const msg = err instanceof Error ? err.message : String(err)
    return {
      code: 502,
      body: {
        ok: false,
        error: `连不上搜索桥 ${cfg.endpoint}: ${msg}`,
        hint: '先起桥：melquiades-http --port 9010（见 README）',
      },
    }
  } finally {
    clearTimeout(timer)
  }
}

export function apply(ctx, rawConfig) {
  const cfg = normalizeConfig(rawConfig)

  if (!cfg.enabled) {
    // ② 层开关：明确记录「关掉了」，而不是让用户猜「为什么没有搜索框」
    ctx.logger?.info?.('[omnibus-search] config.enabled=false —— 不注册任何路由与 tab')
    return
  }

  const trustedHosts = ctx.webRuntime?.trustedHosts ?? []

  const handler = async (req, res) => {
    if (!isTrustedRequest(req, trustedHosts)) {
      sendJson(res, 403, { ok: false, error: 'forbidden' })
      return
    }

    const url = new URL(req.url ?? '/', 'http://dsh.internal')
    const method = url.pathname.startsWith('/omnia/api/')
      ? url.pathname.slice('/omnia/api/'.length)
      : ''

    try {
      if (method === 'health' && req.method === 'GET') {
        const controller = new AbortController()
        const timer = setTimeout(() => controller.abort(), 3000)
        try {
          const resp = await fetch(`${cfg.endpoint}/health`, { signal: controller.signal })
          const data = await resp.json().catch(() => ({ ok: false }))
          sendJson(res, resp.status, { ...data, bridge: cfg.endpoint })
        } catch (err) {
          sendJson(res, 503, {
            ok: false,
            error: `桥不可达: ${err instanceof Error ? err.message : String(err)}`,
            bridge: cfg.endpoint,
          })
        } finally {
          clearTimeout(timer)
        }
        return
      }

      if (req.method !== 'POST') {
        sendJson(res, 405, { ok: false, error: 'method not allowed' })
        return
      }

      const raw = await readBody(req)
      let payload
      try {
        payload = raw ? JSON.parse(raw) : {}
      } catch {
        sendJson(res, 400, { ok: false, error: 'body 不是合法 JSON' })
        return
      }

      if (method === 'search') {
        const query = typeof payload.query === 'string' ? payload.query : ''
        if (!query.trim()) {
          sendJson(res, 400, { ok: false, error: 'query 不能为空' })
          return
        }
        const forwarded = {
          query,
          limit: payload.limit ?? cfg.defaultLimit,
          modes: payload.modes ?? cfg.defaultModes,
        }
        if (payload.sources !== undefined) forwarded.sources = payload.sources
        if (payload.per_source_limit !== undefined) forwarded.per_source_limit = payload.per_source_limit

        const out = await forward(cfg, '/search', forwarded)
        sendJson(res, out.code, out.body)
        return
      }

      if (method === 'context') {
        const guid = typeof payload.guid === 'string' ? payload.guid.trim() : ''
        if (!guid) {
          sendJson(res, 400, { ok: false, error: 'guid 不能为空' })
          return
        }
        const out = await forward(cfg, '/context', {
          guid,
          before: payload.before ?? 3,
          after: payload.after ?? 3,
          max_chars: payload.max_chars ?? 1200,
        })
        sendJson(res, out.code, out.body)
        return
      }

      sendJson(res, 404, { ok: false, error: `未知端点: ${method || '(空)'}` })
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      ctx.logger?.warn?.(`[omnibus-search] ${method || '(空)'} 失败: ${msg}`)
      sendJson(res, 500, { ok: false, error: msg })
    }
  }

  ctx.effect(
    () =>
      ctx.webServer.register({
        kind: 'prefix',
        path: '/omnia/api',
        handler,
      }),
    'dsh-omnibus-search: /omnia/api routes',
  )

  ctx.logger?.info?.(`[omnibus-search] /omnia/api → ${cfg.endpoint}（modes=${cfg.defaultModes}, limit=${cfg.defaultLimit}）`)
}
