import { useEffect, useState } from 'react';
import type { TableRef } from '../lib/api';

// Persists selection in URL + localStorage so bookmarks work.
const KEY = 'po-monitor-selection';

type Selection = {
  catalog: string | null;
  schema: string | null;
  tables: TableRef[];
};

const EMPTY: Selection = { catalog: null, schema: null, tables: [] };

function load(): Selection {
  // URL params take precedence
  const url = new URL(window.location.href);
  const params = url.searchParams;
  const tablesParam = params.get('tables');
  if (tablesParam) {
    try {
      const tables = JSON.parse(decodeURIComponent(tablesParam));
      return {
        catalog: params.get('catalog'),
        schema: params.get('schema'),
        tables,
      };
    } catch {}
  }
  try {
    const stored = localStorage.getItem(KEY);
    if (stored) return JSON.parse(stored);
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
  window.history.replaceState({}, '', url.toString());
}

export function useSelection() {
  const [sel, setSel] = useState<Selection>(() => load());

  useEffect(() => {
    save(sel);
  }, [sel]);

  return {
    ...sel,
    // Preserve `tables[]` when switching catalog/schema so users can build
    // cross-catalog / cross-schema dashboards. Each TableRef already carries
    // its own catalog+schema, so the list is meaningful independent of the
    // current browser selection. Only `clearTables` or explicit toggles clear.
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
            : [...p.tables, t].slice(0, 20), // cap at 20
        };
      }),
    clearTables: () => setSel((p) => ({ ...p, tables: [] })),
    setTables: (ts: TableRef[]) => setSel((p) => ({ ...p, tables: ts.slice(0, 20) })),
  };
}
