"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { api, ApiError } from "@/lib/api";
import { MOCK_API } from "@/lib/config";
import { Button } from "@/components/ui";
import { safeNext } from "@/lib/nav";

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState(MOCK_API ? "demo" : "");
  const [password, setPassword] = useState(MOCK_API ? "demo" : "");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.login(username, password);
      const next = new URLSearchParams(window.location.search).get("next");
      router.replace(safeNext(next));
    } catch (err) {
      if (err instanceof ApiError && err.status === 429) setError("Too many attempts. Wait 15 minutes and try again.");
      else if (err instanceof ApiError && (err.status === 401 || err.status === 400)) setError("Wrong username or password.");
      else setError(err instanceof Error ? err.message : "Login failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="flex flex-1 items-center justify-center bg-slate-950 px-4 py-16">
      <div className="w-full max-w-sm">
        <div className="mb-6 text-center">
          <div className="text-lg font-semibold text-white">ads-autopilot</div>
          <div className="text-sm text-slate-400">Sign in to the dashboard</div>
        </div>
        <form onSubmit={submit} className="space-y-4 rounded-xl bg-white p-6 shadow-xl">
          <label className="block">
            <span className="text-sm font-medium text-slate-700">Username</span>
            <input
              className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-900 focus:ring-1 focus:ring-slate-900"
              autoComplete="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
            />
          </label>
          <label className="block">
            <span className="text-sm font-medium text-slate-700">Password</span>
            <input
              type="password"
              className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-900 focus:ring-1 focus:ring-slate-900"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </label>
          {error && (
            <div role="alert" className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">
              {error}
            </div>
          )}
          <Button type="submit" variant="primary" className="w-full" disabled={busy}>
            {busy ? "Signing in…" : "Sign in"}
          </Button>
          {MOCK_API && (
            <p className="text-center text-xs text-amber-700">Mock mode: any username and password works. All data is example data.</p>
          )}
        </form>
      </div>
    </main>
  );
}
