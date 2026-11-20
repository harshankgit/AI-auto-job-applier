"use client";

import { useEffect, useState } from "react";

import { ErrorNote, Spinner } from "@/components/ui";
import { formatUtc } from "@/lib/format";

interface ReportData {
  markdown: string;
  updatedAt: string;
}

export default function ReportPage() {
  const [data, setData] = useState<ReportData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const load = async () => {
      try {
        const res = await fetch("/api/report", { cache: "no-store" });
        const json = (await res.json()) as ReportData;
        setData(json);
        setError(null);
      } catch (err) {
        setError(err instanceof Error ? err.message : "failed to load report");
      } finally {
        setLoading(false);
      }
    };

    void load();
  }, []);

  return (
    <main className="space-y-8 px-4 py-8 sm:px-6 lg:px-8">
      <div>
        <h1 className="mb-4 text-2xl font-bold tracking-tight text-zinc-100">Report</h1>
        <p className="text-sm text-zinc-400">
          Latest job application report from the bot.
        </p>
      </div>

      {loading ? (
        <div className="flex items-center gap-2 text-sm text-zinc-400">
          <Spinner />
          Loading report…
        </div>
      ) : error ? (
        <ErrorNote title="Failed to load report" body={error} />
      ) : data ? (
        <div className="space-y-4">
          <div className="text-xs text-zinc-500">
            Updated: {formatUtc(data.updatedAt)}
          </div>
          <div className="prose prose-invert max-w-none rounded border border-zinc-800 bg-zinc-900/50 p-6">
            <div
              dangerouslySetInnerHTML={{
                __html: markdownToHtml(data.markdown),
              }}
            />
          </div>
        </div>
      ) : null}
    </main>
  );
}

/**
 * Simple markdown to HTML converter for basic formatting.
 * Handles: headings, bold, italic, code, links, lists, paragraphs.
 */
function markdownToHtml(md: string): string {
  let html = md;

  // Headings
  html = html.replace(/^### (.*?)$/gm, "<h3>$1</h3>");
  html = html.replace(/^## (.*?)$/gm, "<h2>$1</h2>");
  html = html.replace(/^# (.*?)$/gm, "<h1>$1</h1>");

  // Bold
  html = html.replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>");

  // Italic
  html = html.replace(/\*(.*?)\*/g, "<em>$1</em>");

  // Inline code
  html = html.replace(/`(.*?)`/g, "<code>$1</code>");

  // Links
  html = html.replace(/\[(.*?)\]\((.*?)\)/g, '<a href="$2">$1</a>');

  // Unordered lists
  html = html.replace(/^\* (.*?)$/gm, "<li>$1</li>");
  html = html.replace(/(<li>.*?<\/li>)/s, "<ul>$1</ul>");

  // Ordered lists
  html = html.replace(/^\d+\. (.*?)$/gm, "<li>$1</li>");

  // Paragraphs
  html = html
    .split("\n\n")
    .map((para) => {
      if (para.match(/^<[hul]/)) return para;
      if (para.trim() === "") return "";
      return `<p>${para}</p>`;
    })
    .join("\n");

  // Line breaks
  html = html.replace(/\n/g, "<br />");

  return html;
}
