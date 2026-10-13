import { useEffect, useState } from 'react'
import Header from '../components/Header.jsx'
import { getAdminToken } from '../lib/demo.js'

const BASE = import.meta.env.VITE_API_BASE ?? (import.meta.env.DEV ? '/api' : '')
const PAGE_SIZES = [25, 50, 100, 500]

async function get(path, params = {}) {
  const q = new URLSearchParams({ ...params, token: getAdminToken() })
  const res = await fetch(`${BASE}/db-explorer${path}?${q}`)
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`)
  return res.json()
}

function Cell({ v }) {
  if (v === null) return <span className="text-neutral-600">NULL</span>
  return <span title={String(v)}>{String(v)}</span>
}

export default function Database() {
  const [tables, setTables] = useState(null)
  const [error, setError] = useState(null)
  const [active, setActive] = useState(null)
  const [pageSize, setPageSize] = useState(50)
  const [offset, setOffset] = useState(0)
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    get('/tables')
      .then((d) => {
        setTables(d.tables)
        setActive((a) => a ?? d.tables[0]?.name)
      })
      .catch((e) => setError(e.message))
  }, [])

  useEffect(() => {
    if (!active) return
    let alive = true
    setLoading(true)
    get(`/tables/${active}`, { limit: pageSize, offset })
      .then((d) => alive && (setData(d), setError(null)))
      .catch((e) => alive && setError(e.message))
      .finally(() => alive && setLoading(false))
    return () => {
      alive = false
    }
  }, [active, pageSize, offset])

  const pick = (name) => {
    setActive(name)
    setOffset(0)
    setData(null)
  }

  const total = data?.total ?? 0
  const from = total === 0 ? 0 : offset + 1
  const to = Math.min(offset + pageSize, total)
  const lastOffset = Math.max(0, Math.floor((total - 1) / pageSize) * pageSize)

  return (
    <div className="min-h-screen">
      <Header active="database" />
      <main className="mx-auto max-w-[1400px] px-4 py-8">
        <h1 className="text-3xl font-black tracking-tight">Database</h1>
        <p className="mt-2 max-w-2xl text-sm text-neutral-400">
          Live, read-only view of every table in PostgreSQL: all columns, all rows, a page at a time.
        </p>

        {error && (
          <div className="mt-4 rounded-xl border border-red-500/40 bg-red-500/10 p-3 text-sm text-red-300">{error}</div>
        )}

        <div className="mt-6 grid gap-4 lg:grid-cols-[240px_1fr]">
          <nav className="flex gap-2 overflow-x-auto lg:flex-col lg:overflow-visible">
            {!tables && <div className="h-40 w-full animate-pulse rounded-xl bg-neutral-800" />}
            {tables?.map((t) => (
              <button
                key={t.name}
                onClick={() => pick(t.name)}
                className={`flex shrink-0 items-center justify-between gap-3 rounded-xl border px-3 py-2 text-left text-sm transition ${
                  active === t.name
                    ? 'border-orange-500 bg-orange-500/10 text-white'
                    : 'border-neutral-800 bg-neutral-900 text-neutral-300 hover:border-neutral-600'
                }`}
              >
                <span className="font-mono text-xs">{t.name}</span>
                <span className="tabular text-[11px] text-neutral-500">
                  {t.rows.toLocaleString()} x {t.columns.length}
                </span>
              </button>
            ))}
          </nav>

          <section className="min-w-0 rounded-2xl border border-neutral-800 bg-neutral-900">
            <div className="flex flex-wrap items-center gap-3 border-b border-neutral-800 p-3 text-xs text-neutral-400">
              <span className="font-mono text-sm font-bold text-white">{active}</span>
              <span className="tabular">
                rows {from.toLocaleString()}-{to.toLocaleString()} of {total.toLocaleString()}
              </span>
              <span className="ml-auto flex items-center gap-2">
                <label>
                  per page{' '}
                  <select
                    value={pageSize}
                    onChange={(e) => {
                      setPageSize(Number(e.target.value))
                      setOffset(0)
                    }}
                    className="rounded bg-neutral-800 px-1.5 py-1 text-white"
                  >
                    {PAGE_SIZES.map((n) => (
                      <option key={n}>{n}</option>
                    ))}
                  </select>
                </label>
                {[
                  ['First', 0],
                  ['Prev', Math.max(0, offset - pageSize)],
                  ['Next', Math.min(lastOffset, offset + pageSize)],
                  ['Last', lastOffset],
                ].map(([label, target]) => (
                  <button
                    key={label}
                    disabled={loading || target === offset}
                    onClick={() => setOffset(target)}
                    className="rounded bg-neutral-800 px-2.5 py-1 font-semibold text-white transition hover:bg-neutral-700 disabled:opacity-30"
                  >
                    {label}
                  </button>
                ))}
              </span>
            </div>

            <div className={`max-h-[70vh] overflow-auto ${loading ? 'opacity-60' : ''}`}>
              {!data && <div className="m-4 h-40 animate-pulse rounded-xl bg-neutral-800" />}
              {data && (
                <table className="w-full border-collapse text-left text-xs">
                  <thead className="sticky top-0 bg-neutral-800">
                    <tr>
                      {data.columns.map((c) => (
                        <th key={c.name} className="whitespace-nowrap px-3 py-2 font-semibold text-white">
                          {c.name}
                          {c.primary_key && <span className="ml-1 text-orange-400">PK</span>}
                          <div className="text-[10px] font-normal text-neutral-500">{c.type}</div>
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {data.rows.map((row, i) => (
                      <tr key={i} className="border-t border-neutral-800">
                        {row.map((v, j) => (
                          <td key={j} className="max-w-[260px] truncate whitespace-nowrap px-3 py-1.5 font-mono text-neutral-200">
                            <Cell v={v} />
                          </td>
                        ))}
                      </tr>
                    ))}
                    {data.rows.length === 0 && (
                      <tr>
                        <td colSpan={data.columns.length} className="px-3 py-8 text-center text-neutral-500">
                          This table is empty.
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              )}
            </div>
          </section>
        </div>
      </main>
    </div>
  )
}
