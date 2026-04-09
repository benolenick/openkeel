/**
 * Tiny HTTP helper. Uses global fetch (Node 18+). Times out per config.
 * Returns parsed JSON on 2xx, throws on anything else.
 */

export async function postJson<T = unknown>(
  url: string,
  body: unknown,
  timeoutMs: number
): Promise<T> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: ctrl.signal,
    });
    if (!res.ok) {
      throw new Error(`LLMOS HTTP ${res.status} on ${url}: ${await res.text()}`);
    }
    return (await res.json()) as T;
  } finally {
    clearTimeout(timer);
  }
}

export async function getJson<T = unknown>(
  url: string,
  timeoutMs: number
): Promise<T> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(url, { signal: ctrl.signal });
    if (!res.ok) {
      throw new Error(`LLMOS HTTP ${res.status} on ${url}`);
    }
    return (await res.json()) as T;
  } finally {
    clearTimeout(timer);
  }
}
