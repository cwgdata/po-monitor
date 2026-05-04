import { useEffect, useState } from 'react';
import type { TableRef, GroupRef } from '../lib/api';

// Persists selection in URL + localStorage so bookmarks work.
const KEY = 'po-monitor-selection';

type Selection = {
  catalog: string | null;
  schema: string | null;
  tables: TableRef[];
  groups: GroupRef[];
};

const EMPTY: Selection = { catalog: null, schema: null, tables: [], groups: [] };

// Mirrors the server's identifier validator (server/sql_client.py::validate_ident).
// Reject anything that could break out of a backtick-quoted SQL identifier when
// later forwarded to the backend.
const IDENT_RE = /^[A-Za-z0-9_][A-Za-z0-9_-]{0,254}$/;

function safeIdent(v: unknown): string | null {
  return typeof v === 'string' && IDENT_RE.test(v) ? v : null;
}

function sanitizeTables(input: unknown): TableRef[] {
  if (!Array.isArray(input)) return [];
  const out: TableRef[] = [];
  for (const x of input) {
    if (!x || typeof x !== 'object') continue;
    const c = safeIdent((x as any).catalog);
    const s = safeIdent((x as any).schema);
    const t = safeIdent((x as any).table);
    if (c && s && t) out.push({ catalog: c, schema: s, table: t });
  }
  return out;
}

function sanitizeGroups(input: unknown): GroupRef[] {
  if (!Array.isArray(input)) return [];
  const out: GroupRef[] = [];
  for (const x of input) {
    if (!x || typeof x !== 'object') continue;
    const kind = (x as any).kind;
    if (kind !== 'schema' && kind !== 'catalog') continue;
    const c = safeIdent((x as any).catalog);
    if (!c) continue;
    if (kind === 'schema') {
      const s = safeIdent((x as any).schema);
      if (!s) continue;
      out.push({ kind: 'schema', catalog: c, schema: s });
    } else {
      out.push({ kind: 'catalog', catalog: c });
    }
  }
  return out;
}

function load(): Selection {
  // URL params take precedence
  const url = new URL(window.location.href);
  const params = url.searchParams;
  const tablesParam = params.get('tables');
  const groupsParam = params.get('groups');
  if (tablesParam || groupsParam) {
    try {
      const rawTables = tablesParam ? JSON.parse(decodeURIComponent(tablesParam)) : [];
      const rawGroups = groupsParam ? JSON.parse(decodeURIComponent(groupsParam)) : [];
      return {
        catalog: safeIdent(params.get('catalog')),
        schema: safeIdent(params.get('schema')),
        tables: sanitizeTables(rawTables),
        groups: sanitizeGroups(rawGroups),
      };
    } catch {}
  }
  try {
    const stored = localStorage.getItem(KEY);
    if (stored) {
      const parsed = JSON.parse(stored) ?? {};
      return {
        catalog: safeIdent(parsed.catalog),
        schema: safeIdent(parsed.schema),
        tables: sanitizeTables(parsed.tables),
        groups: sanitizeGroups(parsed.groups),
      };
    }
  } catch {}
  return EMPTY;
}

function save(sel: Selection) {
  try {
    localStorage.setItem(KEY, JSON.stringify(sel));
  } catch {}
  const url = new URL(window.location.href);
  if (sel.catalog) url.searchParams.set('catalog', sel.catalog);
  else url.searchParams.delete('catalog');
  if (sel.schema) url.searchParams.set('schema', sel.schema);
  else url.searchParams.delete('schema');
  if (sel.tables.length) {
    url.searchParams.set('tables', encodeURIComponent(JSON.stringify(sel.tables)));
  } else {
    url.searchParams.delete('tables');
  }
  if (sel.groups.length) {
    url.searchParams.set('groups', encodeURIComponent(JSON.stringify(sel.groups)));
  } else {
    url.searchParams.delete('groups');
  }
  window.history.replaceState({}, '', url.toString());
}

function groupKey(g: GroupRef): string {
  return g.kind === 'schema' ? `s:${g.catalog}.${g.schema}` : `c:${g.catalog}`;
}

const TABLE_CAP = 20;
const GROUP_CAP = 10;

export function useSelection() {
  const [sel, setSel] = useState<Selection>(() => load());

  useEffect(() => {
    save(sel);
  }, [sel]);

  return {
    ...sel,
    setCatalog: (c: string | null) =>
      setSel((p) => ({ ...p, catalog: c, schema: null })),
    setSchema: (s: string | null) =>
      setSel((p) => ({ ...p, schema: s })),
    toggleTable: (t: TableRef) =>
      setSel((p) => {
        const exists = p.tables.some(
          (x) => x.catalog === t.catalog && x.schema === t.schema && x.table === t.table,
        );
        return {
          ...p,
          tables: exists
            ? p.tables.filter(
                (x) => !(x.catalog === t.catalog && x.schema === t.schema && x.table === t.table),
              )
            : [...p.tables, t].slice(0, TABLE_CAP),
        };
      }),
    clearTables: () => setSel((p) => ({ ...p, tables: [], groups: [] })),
    setTables: (ts: TableRef[]) => setSel((p) => ({ ...p, tables: ts.slice(0, TABLE_CAP) })),

    toggleGroup: (g: GroupRef) =>
      setSel((p) => {
        const k = groupKey(g);
        const exists = p.groups.some((x) => groupKey(x) === k);
        return {
          ...p,
          groups: exists
            ? p.groups.filter((x) => groupKey(x) !== k)
            : [...p.groups, g].slice(0, GROUP_CAP),
        };
      }),
    removeGroup: (g: GroupRef) =>
      setSel((p) => {
        const k = groupKey(g);
        return { ...p, groups: p.groups.filter((x) => groupKey(x) !== k) };
      }),
    isGroupSelected: (g: GroupRef): boolean => {
      const k = groupKey(g);
      return sel.groups.some((x) => groupKey(x) === k);
    },
  };
}

export function groupRefKey(g: GroupRef): string {
  return groupKey(g);
}
