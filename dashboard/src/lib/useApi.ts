"use client";

import { useCallback, useEffect, useEffectEvent, useState } from "react";
import { useRouter } from "next/navigation";
import { ApiError } from "./api";

export interface ApiState<T> {
  data: T | undefined;
  error: unknown;
  loading: boolean;
  reload: () => void;
}

/** Login URL that returns the user to where they were (paths are basePath-relative). */
export function loginHref(): string {
  if (typeof window === "undefined") return "/login";
  const base = process.env.NEXT_PUBLIC_BASE_PATH ?? "";
  const here = window.location.pathname.slice(base.length) + window.location.search;
  return here && here !== "/" && !here.startsWith("/login") ? `/login?next=${encodeURIComponent(here)}` : "/login";
}

/**
 * Minimal client-side query hook. All data loads in the browser because the API URL is a
 * same-origin path behind the proxy (§18) and the session cookie is httpOnly.
 */
export function useApi<T>(fn: () => Promise<T>, deps: unknown[] = [], opts: { refreshMs?: number } = {}): ApiState<T> {
  const router = useRouter();
  const [data, setData] = useState<T>();
  const [error, setError] = useState<unknown>(null);
  const [tick, setTick] = useState(0);
  const run = useEffectEvent(() => fn());

  useEffect(() => {
    let alive = true;
    run()
      .then((d) => {
        if (!alive) return;
        setData(d);
        setError(null);
      })
      .catch((e: unknown) => {
        if (!alive) return;
        if (e instanceof ApiError && e.unauthorized) {
          router.replace(loginHref());
          return;
        }
        setError(e);
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  useEffect(() => {
    if (!opts.refreshMs) return;
    const t = setInterval(() => setTick((x) => x + 1), opts.refreshMs);
    return () => clearInterval(t);
  }, [opts.refreshMs]);

  const reload = useCallback(() => setTick((x) => x + 1), []);
  // Stale-while-revalidate: previous data stays visible while a reload is in flight.
  return { data, error, loading: data === undefined && !error, reload };
}
