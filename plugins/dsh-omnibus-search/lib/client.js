/**
 * dsh-omnibus-search — client half.
 *
 * 在 dsh-better-sidebar 的侧栏里注册一个「统一检索」tab，搜的是 melquiades 账本
 * （草稿纸 / 会话正文 / 压缩摘要），不是当前目录的文件。
 *
 * ── 结果在 tab 内自包含展开，不碰 chat 区 ──
 * 会话命中的展示方式是：点击 → 调 /omnia/api/context 拿「这一条前后 N 条」→
 * 就地渲染。**不去跳转聊天记录**——DSH 客户端没有「定位到某条消息」的公开 API
 * （grep `anchorSeq` 零命中），硬做要改核心；而自包含展开的代价小一个数量级。
 *
 * ── 打包格式 ──
 * DSH client 插件是纯 `window.__ModuleLoader__.load` 定义：无 JSX、无 TS、
 * 除 loader 表提供的外部模块（`react`）外不 import 任何东西。
 * `apply` / `inject` 是导出面；`inject` 声明它读的 cordis 服务（`betterSidebar`）。
 */
window.__ModuleLoader__.load({
	id: 'dsh-omnibus-search',
	factory: (require) => {
		const { createElement: h, useCallback, useEffect, useMemo, useRef, useState } = require('react')

		// ── 样式（注入一次；全部走主题 token，换肤自动跟随）──
		const CSS_ID = 'dsh-omnibus-search/css'
		if (typeof document !== 'undefined' && !document.querySelector(`style[data-plugin-css="${CSS_ID}"]`)) {
			const tag = document.createElement('style')
			tag.dataset.plugin = 'dsh-omnibus-search'
			tag.dataset.pluginCss = CSS_ID
			tag.textContent = [
				'.osm-root{display:flex;flex-direction:column;height:100%;min-height:0;font-size:13px;color:var(--dsw-alias-label-primary,#e8e8ea)}',
				'.osm-bar{display:flex;gap:6px;padding:8px 10px;border-bottom:1px solid var(--dsw-alias-border-l2,#33333a)}',
				'.osm-input{flex:1;min-width:0;height:30px;padding:0 10px;border-radius:6px;border:1px solid var(--dsw-alias-border-l2,#44444c);background:var(--dsw-alias-bg-layer-3,#1b1b22);color:inherit;font-size:13px;outline:none}',
				'.osm-input:focus{border-color:var(--dsw-alias-brand-primary,#4d6bfe)}',
				'.osm-go{height:30px;padding:0 12px;border:none;border-radius:6px;background:var(--dsw-alias-brand-primary,#4d6bfe);color:#fff;font-size:13px;cursor:pointer;flex:none}',
				'.osm-go:disabled{opacity:.5;cursor:default}',
				'.osm-meta{padding:6px 10px;font-size:11.5px;color:var(--dsw-alias-label-secondary,#9a9aa5);display:flex;gap:10px;flex-wrap:wrap}',
				'.osm-err{padding:8px 10px;color:var(--dsw-alias-state-error-primary,#e5484d);font-size:12px;white-space:pre-wrap;word-break:break-word}',
				'.osm-list{flex:1;min-height:0;overflow:auto;padding:4px 0}',
				'.osm-group{padding:8px 10px 2px;font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:var(--dsw-alias-label-secondary,#8a8a96)}',
				'.osm-hit{padding:7px 10px;cursor:pointer;border-left:2px solid transparent;display:flex;flex-direction:column;gap:3px}',
				'.osm-hit:hover{background:var(--dsw-alias-bg-layer-2,#242429)}',
				'.osm-hit-active{border-left-color:var(--dsw-alias-brand-primary,#4d6bfe);background:var(--dsw-alias-bg-layer-2,#242429)}',
				'.osm-hit-head{display:flex;gap:8px;align-items:baseline;font-size:11px;color:var(--dsw-alias-label-secondary,#8a8a96)}',
				'.osm-badge{padding:0 5px;border-radius:4px;background:var(--dsw-alias-bg-layer-3,#2c2c34);font-size:10.5px}',
				'.osm-score{font-variant-numeric:tabular-nums}',
				'.osm-snip{font-size:12px;line-height:1.5;color:var(--dsw-alias-label-primary,#dcdce2);word-break:break-word}',
				'.osm-mark{background:var(--dsw-alias-state-warning-bg,rgba(255,193,7,.28));color:inherit;border-radius:2px}',
				'.osm-ctx{margin:4px 0 2px;padding:8px 10px;border-radius:6px;background:var(--dsw-alias-bg-layer-3,#1d1d24);font-family:var(--dsw-font-mono,ui-monospace,monospace);font-size:11.5px;line-height:1.55;white-space:pre-wrap;word-break:break-word;max-height:320px;overflow:auto}',
				'.osm-empty{padding:24px 14px;color:var(--dsw-alias-label-secondary,#8a8a96);font-size:12px;line-height:1.7}',
				'.osm-foot{padding:6px 10px;border-top:1px solid var(--dsw-alias-border-l2,#33333a);font-size:11px;color:var(--dsw-alias-label-secondary,#8a8a96)}',
			].join('')
			document.head.appendChild(tag)
		}

		const SOURCE_LABEL = { draftpad: '草稿纸', messages: '会话正文', compactions: '压缩摘要' }

		/** 把 query 在 snippet 里切段渲染，命中部分包 <mark>。不用 innerHTML（防注入）。 */
		function renderSnippet(text, query) {
			const raw = String(text || '')
			const q = String(query || '').trim()
			if (!q || raw.length === 0) return raw
			const lower = raw.toLowerCase()
			const needle = q.toLowerCase()
			const parts = []
			let from = 0
			let at = lower.indexOf(needle)
			let guard = 0
			while (at >= 0 && guard < 200) {
				if (at > from) parts.push(raw.slice(from, at))
				parts.push(h('mark', { className: 'osm-mark', key: `m${guard}` }, raw.slice(at, at + needle.length)))
				from = at + needle.length
				at = lower.indexOf(needle, from)
				guard += 1
			}
			if (from < raw.length) parts.push(raw.slice(from))
			return parts
		}

		function apiFetch(path, payload, signal) {
			return fetch(path, {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify(payload),
				signal,
			}).then(async (resp) => {
				const data = await resp.json().catch(() => ({ ok: false, error: `HTTP ${resp.status}` }))
				return data
			})
		}

		function SearchTab() {
			const [query, setQuery] = useState('')
			const [hits, setHits] = useState([])
			const [notes, setNotes] = useState([])
			const [stats, setStats] = useState(null)
			const [tookMs, setTookMs] = useState(null)
			const [loading, setLoading] = useState(false)
			const [error, setError] = useState('')
			const [activeIdx, setActiveIdx] = useState(-1)
			const [expanded, setExpanded] = useState(null) // { key, text, loading }
			// 竞态：只认最后一次请求的响应。序号比 AbortController 更省事——
			// 桥是本地回环，前一个请求留着跑完也无害，但**渲染**必须只来自最新一次。
			const seqRef = useRef(0)
			const inputRef = useRef(null)

			useEffect(() => {
				inputRef.current?.focus()
			}, [])

			const runSearch = useCallback(async (q) => {
				const text = String(q || '').trim()
				if (!text) return
				const seq = ++seqRef.current
				setLoading(true)
				setError('')
				setExpanded(null)
				setActiveIdx(-1)
				try {
					const data = await apiFetch('/omnia/api/search', { query: text })
					if (seq !== seqRef.current) return // 有更新的请求了，丢弃
					if (!data.ok) {
						setError(data.error || '搜索失败')
						setHits([])
						setNotes([])
						setStats(null)
						return
					}
					setHits(data.hits || [])
					setNotes(data.notes || [])
					setStats(data.stats || null)
					setTookMs(typeof data.took_ms === 'number' ? data.took_ms : null)
				} catch (err) {
					if (seq !== seqRef.current) return
					setError(err instanceof Error ? err.message : String(err))
					setHits([])
				} finally {
					if (seq === seqRef.current) setLoading(false)
				}
			}, [])

			const toggleContext = useCallback(async (hit) => {
				const key = `${hit.source}:${hit.record_id}`
				if (expanded && expanded.key === key) {
					setExpanded(null)
					return
				}
				if (hit.source !== 'messages') {
					setExpanded({
						key,
						text:
							'（这一类结果没有「会话上下文」可展开）\n\n' +
							`来源：${SOURCE_LABEL[hit.source] || hit.source}\n记录：${hit.record_id}\n\n` +
							String(hit.snippet || ''),
						loading: false,
					})
					return
				}
				setExpanded({ key, text: '', loading: true })
				try {
					const data = await apiFetch('/omnia/api/context', { guid: hit.record_id })
					setExpanded({ key, text: data.ok ? data.text || '（空）' : `展开失败：${data.error || '未知错误'}`, loading: false })
				} catch (err) {
					setExpanded({ key, text: `展开失败：${err instanceof Error ? err.message : String(err)}`, loading: false })
				}
			}, [expanded])

			const groups = useMemo(() => {
				const order = ['draftpad', 'messages', 'compactions']
				const buckets = new Map()
				hits.forEach((hit) => {
					if (!buckets.has(hit.source)) buckets.set(hit.source, [])
					buckets.get(hit.source).push(hit)
				})
				return order.filter((s) => buckets.has(s)).map((s) => [s, buckets.get(s)])
			}, [hits])

			const flatHits = useMemo(() => groups.flatMap(([, items]) => items), [groups])

			function onKeyDown(ev) {
				if (ev.key === 'Enter' && !ev.shiftKey) {
					ev.preventDefault()
					runSearch(query)
					return
				}
				if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
					if (flatHits.length === 0) return
					ev.preventDefault()
					const delta = ev.key === 'ArrowDown' ? 1 : -1
					setActiveIdx((prev) => {
						const next = prev + delta
						if (next < 0) return flatHits.length - 1
						if (next >= flatHits.length) return 0
						return next
					})
					return
				}
				if (ev.key === 'Escape') {
					if (expanded) setExpanded(null)
					return
				}
				if (ev.key === 'Enter' && ev.shiftKey) {
					const hit = flatHits[activeIdx]
					if (hit) {
						ev.preventDefault()
						toggleContext(hit)
					}
				}
			}

			const children = []
			children.push(
				h(
					'div',
					{ className: 'osm-bar', key: 'bar' },
					h('input', {
						ref: inputRef,
						className: 'osm-input',
						placeholder: '搜草稿纸 / 会话正文 / 压缩摘要…',
						value: query,
						onChange: (e) => setQuery(e.target.value),
						onKeyDown,
						spellCheck: false,
					}),
					h(
						'button',
						{ className: 'osm-go', onClick: () => runSearch(query), disabled: loading || !query.trim() },
						loading ? '…' : '搜索',
					),
				),
			)

			if (error) children.push(h('div', { className: 'osm-err', key: 'err' }, error))

			if (hits.length > 0 || notes.length > 0) {
				const metaBits = []
				if (hits.length) metaBits.push(h('span', { key: 'n' }, `命中 ${hits.length}`))
				if (tookMs !== null) metaBits.push(h('span', { key: 't' }, `${tookMs} ms`))
				if (stats && stats.vector_available === false) metaBits.push(h('span', { key: 'v' }, '向量路不可用'))
				if (notes.length) {
					metaBits.push(
						h('span', { key: 'note', title: notes.join('\n') }, `⚠ ${notes.length} 条提示`),
					)
				}
				children.push(h('div', { className: 'osm-meta', key: 'meta' }, metaBits))
			}

			if (hits.length === 0 && !loading && !error) {
				children.push(
					h(
						'div',
						{ className: 'osm-empty', key: 'empty' },
						'回车搜索。↑↓ 选择，Shift+Enter 展开上下文，Esc 收起。',
						h('br'),
						h('br'),
						'检索走本机 melquiades 桥（' + '127.0.0.1:9010' + '）——三路融合：向量（跨语言语义）、全文（单语言精确）、LIKE（字面兜底）。',
					),
				)
			}

			let flatIdx = -1
			const listChildren = []
			groups.forEach(([source, items]) => {
				listChildren.push(h('div', { className: 'osm-group', key: `g:${source}` }, `${SOURCE_LABEL[source] || source} · ${items.length}`))
				items.forEach((hit) => {
					flatIdx += 1
					const key = `${hit.source}:${hit.record_id}`
					const isActive = flatIdx === activeIdx
					const isOpen = expanded && expanded.key === key
					listChildren.push(
						h(
							'div',
							{
								key,
								className: 'osm-hit' + (isActive ? ' osm-hit-active' : ''),
								onClick: () => {
									setActiveIdx(flatIdx)
									toggleContext(hit)
								},
							},
							h(
								'div',
								{ className: 'osm-hit-head' },
								h('span', { className: 'osm-badge' }, (hit.matched_by || []).join('+') || '—'),
								h('span', { className: 'osm-score' }, Number(hit.score || 0).toFixed(3)),
								h('span', {}, String(hit.record_id).slice(0, 16)),
							),
							h('div', { className: 'osm-snip' }, renderSnippet(hit.snippet, query)),
							isOpen
								? h(
										'div',
										{ className: 'osm-ctx' },
										expanded.loading ? '加载中…' : expanded.text,
								  )
								: null,
						),
					)
				})
			})
			if (listChildren.length) children.push(h('div', { className: 'osm-list', key: 'list' }, listChildren))

			children.push(
				h(
					'div',
					{ className: 'osm-foot', key: 'foot' },
					'结果在 tab 内展开，不跳转聊天记录',
				),
			)

			return h('div', { className: 'osm-root' }, children)
		}

		const inject = ['betterSidebar']

		function apply(ctx) {
			const registry = ctx && ctx.betterSidebar
			if (!registry || typeof registry.registerTab !== 'function') {
				// ③ 层开关：没有 better-sidebar 就只警告，不崩（host 路由仍可用 curl 打）
				console.warn(
					'[dsh-omnibus-search] 未检测到 dsh-better-sidebar 的 registerTab 服务，检索 tab 未注册。' +
						'请先安装/升级 dsh-better-sidebar，再重启 DSH 并硬刷新浏览器。',
				)
				return
			}
			ctx.effect(() =>
				ctx.betterSidebar.registerTab({
					id: 'omnibus-search',
					title: '统一检索',
					description: '搜草稿纸、会话正文与压缩摘要（跨语言语义 + 精确文本）',
					icon: (size) =>
						h(
							'svg',
							{
								width: size,
								height: size,
								viewBox: '0 0 24 24',
								fill: 'none',
								stroke: 'currentColor',
								strokeWidth: 2,
								strokeLinecap: 'round',
							},
							h('circle', { cx: 10.5, cy: 10.5, r: 6.5 }),
							h('line', { x1: 20, y1: 20, x2: 15.2, y2: 15.2 }),
							h('line', { x1: 7.5, y1: 10.5, x2: 13.5, y2: 10.5 }),
						),
					order: 24,
					single: true,
					component: SearchTab,
				}),
			)
		}

		return { apply, inject }
	},
})
