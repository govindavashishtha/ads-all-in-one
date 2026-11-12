import type { Metadata } from "next";
import { BASE_PATH } from "@/lib/config";
import { MockBanner } from "@/components/MockBanner";
import "./globals.css";

export const metadata: Metadata = {
  title: "ads-autopilot",
  description: "Single-client Meta + Google Ads autopilot dashboard",
  robots: { index: false, follow: false },
  icons: {
    icon: [
      { url: `${BASE_PATH}/icon.svg`, type: "image/svg+xml" },
      { url: `${BASE_PATH}/favicon.ico`, sizes: "any" },
    ],
  },
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
