import type { IndexQuery, ListQuery } from '@/types/activity';

function appendFilters(
  params: URLSearchParams,
  filters: Readonly<Record<string, string | readonly string[]>> | undefined
): void {
  for (const [key, value] of Object.entries(filters ?? {})) {
    if (Array.isArray(value)) {
      for (const v of value) {
        if (v) params.append(key, v);
      }
    } else if (typeof value === 'string' && value) {
      params.set(key, value);
    }
  }
}

/** `path?page=&page_size=&sort_key=&sort_dir=&<filters>`; empty filter values are omitted. */
export function buildListUrl(path: string, q: ListQuery): string {
  const params = new URLSearchParams({
    page: String(q.page),
    page_size: String(q.pageSize),
    sort_key: q.sortKey,
    sort_dir: q.sortDir,
  });
  appendFilters(params, q.filters);
  return `${path}?${params.toString()}`;
}

/** `path/index?sort_key=&sort_dir=&<filters>` for a list's group index. */
export function buildIndexUrl(path: string, q: IndexQuery): string {
  const params = new URLSearchParams({ sort_key: q.sortKey, sort_dir: q.sortDir });
  appendFilters(params, q.filters);
  return `${path}/index?${params.toString()}`;
}
