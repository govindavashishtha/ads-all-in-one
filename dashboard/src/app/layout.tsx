import type { Metadata } from "next";
import { MockBanner } from "@/components/MockBanner";
import "./globals.css";

export const metadata: Metadata = {
  title: "ads-autopilot",
  description: "Single-client Meta + Google Ads autopilot dashboard",
  robots: { index: false, follow: false },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="flex min-h-full flex-col">
        <MockBanner />
        {children}
      </body>
    </html>
  );
}
