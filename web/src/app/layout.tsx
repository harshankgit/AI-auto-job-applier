import type { Metadata } from "next";

import { Nav } from "@/components/nav";

import "./globals.css";

export const metadata: Metadata = {
  title: "AI Job Auto-Applier",
  description: "Operations console for the jobbot application pipeline",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">
        <Nav />
        <main className="mx-auto w-full max-w-[1400px] px-4 pb-20 pt-6 sm:px-6 lg:px-8">
          {children}
        </main>
      </body>
    </html>
  );
}
