"use client";

import type { ReactNode } from "react";
import { ApiError } from "@/lib/api";
import { Button, Card, EmptyState, Skeleton } from "./ui";

export function NotEnabled({ feature, phase, children }: { feature: string; phase: number; children?: ReactNode }) {
  return (
    <Card className="border-dashed">
      <div className="flex flex-col items-center px-6 py-14 text-center" data-testid="not-enabled">
        <span className="rounded-full bg-slate-100 px-2.5 py-0.5 text-xs font-medium text-slate-600">Phase {phase}</span>
        <h2 className="mt-3 text-base font-semibold text-slate-900">{feature} is not enabled yet</h2>
        <p className="mt-1 max-w-lg text-sm text-slate-500">
          {children ??
            `This page lights up when the Phase ${phase} backend is deployed. Nothing is wrong — the API simply does not serve this data yet.`}
        </p>
      </div>
    </Card>
  );
}

export function ErrorPanel({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const msg =
    error instanceof ApiError
      ? error.forbidden
        ? "Your role does not allow this view."
        : error.message
      : error instanceof Error
        ? error.message
        : "Something went wrong.";
  return (
    <Card className="border-red-200 bg-red-50/40">
      <div className="flex items-center justify-between gap-3 px-5 py-4" role="alert">
        <div>
          <div className="text-sm font-medium text-red-800">Could not load data</div>
          <div className="text-sm text-red-700">{msg}</div>
        </div>
        {onRetry && (
          <Button size="sm" onClick={onRetry}>
            Retry
          </Button>
        )}
      </div>
    </Card>
  );
}

/**
 * Standard loading / not-enabled / error handling for a query.
 * `phase` + `feature`: a 404/501 means that phase's API is not deployed yet.
 */
export function QueryState<T>({
  state,
  feature,
  phase,
  skeleton,
  children,
}: {
  state: { data: T | undefined; error: unknown; loading: boolean; reload: () => void };
  feature?: string;
  phase?: number;
  skeleton?: ReactNode;
  children: (data: T) => ReactNode;
}) {
  if (state.error) {
    if (state.error instanceof ApiError && state.error.notEnabled && feature && phase) {
      return <NotEnabled feature={feature} phase={phase} />;
    }
    return <ErrorPanel error={state.error} onRetry={state.reload} />;
  }
  if (state.data === undefined) {
    return <>{skeleton ?? <Skeleton className="h-64 w-full" />}</>;
  }
  return <>{children(state.data)}</>;
}

export { EmptyState };
