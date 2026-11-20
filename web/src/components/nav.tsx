"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/", label: "Dashboard" },
  { href: "/applications", label: "Applications" },
  { href: "/results", label: "Results" },
  { href: "/report", label: "Report" },
] as const;

export function Nav() {
  const pathname = usePathname();

  return (
    <header className="sticky top-0 z-30 border-b border-zinc-800 bg-zinc-950/85 backdrop-blur">
      <div className="mx-auto flex w-full max-w-[1400px] items-center gap-4 px-4 py-3 sm:px-6 lg:px-8">
        <Link href="/" className="flex min-w-0 items-center gap-2.5">
          <span
            aria-hidden
            className="h-5 w-5 shrink-0 rounded-[5px] bg-gradient-to-br from-indigo-400 to-indigo-600"
          />
          <span className="truncate text-sm font-semibold tracking-tight text-zinc-100">
            AI Job Auto-Applier
          </span>
        </Link>

        <nav className="ml-auto flex items-center gap-1 overflow-x-auto whitespace-nowrap">
          {LINKS.map((link) => {
            const active = link.href === "/" ? pathname === "/" : pathname.startsWith(link.href);
            return (
              <Link
                key={link.href}
                href={link.href}
                aria-current={active ? "page" : undefined}
                className={[
                  "rounded-lg px-3 py-1.5 text-sm transition-colors",
                  active
                    ? "bg-zinc-800/80 text-zinc-100"
                    : "text-zinc-400 hover:bg-zinc-900 hover:text-zinc-200",
                ].join(" ")}
              >
                {link.label}
              </Link>
            );
          })}
        </nav>
      </div>
    </header>
  );
}
