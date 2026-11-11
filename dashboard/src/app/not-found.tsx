import Link from "next/link";

export default function NotFound() {
  return (
    <main className="flex flex-1 flex-col items-center justify-center gap-2 py-24">
      <h1 className="text-lg font-semibold">Page not found</h1>
      <Link href="/" className="text-sm text-slate-600 underline">Back to overview</Link>
    </main>
  );
}
