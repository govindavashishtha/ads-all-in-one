import { AppShell } from "@/components/AppShell";
import { SessionProvider } from "@/components/session";

export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <SessionProvider>
      <AppShell>{children}</AppShell>
    </SessionProvider>
  );
}
