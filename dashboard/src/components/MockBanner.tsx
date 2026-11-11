import { MOCK_API } from "@/lib/config";

/** Always visible in mock mode so example numbers are never mistaken for real ones. */
export function MockBanner() {
  if (!MOCK_API) return null;
  return (
    <div
      role="status"
      data-testid="mock-banner"
      className="sticky top-0 z-50 border-b border-amber-300 bg-[repeating-linear-gradient(135deg,#fef3c7_0,#fef3c7_12px,#fde68a_12px,#fde68a_24px)] px-4 py-1.5 text-center text-xs font-semibold tracking-wide text-amber-900"
    >
      EXAMPLE DATA — mock mode (NEXT_PUBLIC_MOCK_API). No backend is connected; nothing shown here is real.
    </div>
  );
}

export function ExampleTag() {
  if (!MOCK_API) return null;
  return (
    <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-amber-800 ring-1 ring-amber-300">
      Example data
    </span>
  );
}
