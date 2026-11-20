/**
 * Minimal RFC-4180 CSV reader.
 *
 * The bot's report quotes any field containing a comma, a newline or a quote
 * (and escapes inner quotes by doubling them), so a naive split on "," loses
 * data. This walks the text character by character instead.
 */
export function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let inQuotes = false;
  let touched = false;

  const endField = () => {
    row.push(field);
    field = "";
  };
  const endRow = () => {
    endField();
    rows.push(row);
    row = [];
    touched = false;
  };

  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i] as string;

    if (inQuotes) {
      if (ch === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i += 1;
        } else {
          inQuotes = false;
        }
      } else {
        field += ch;
      }
      continue;
    }

    if (ch === '"' && field === "") {
      inQuotes = true;
      touched = true;
      continue;
    }
    if (ch === ",") {
      endField();
      touched = true;
      continue;
    }
    if (ch === "\r") continue;
    if (ch === "\n") {
      endRow();
      continue;
    }
    field += ch;
    touched = true;
  }

  if (touched || field !== "" || row.length > 0) endRow();
  return rows.filter((r) => r.some((cell) => cell.trim() !== ""));
}

/** Quote a single value for CSV output (used by the client-side export). */
export function csvQuote(value: string): string {
  return /[",\n\r]/.test(value) ? `"${value.replaceAll('"', '""')}"` : value;
}

export function toCsv(headers: string[], rows: string[][]): string {
  const lines = [headers.map(csvQuote).join(",")];
  for (const row of rows) lines.push(row.map(csvQuote).join(","));
  return `${lines.join("\n")}\n`;
}
