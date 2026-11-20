# AI Job Auto-Applier Dashboard

A full-stack Next.js 15 + TypeScript + Tailwind v4 web dashboard for the jobbot job application automation system. Provides real-time visibility into job applications, candidate profiles, and bot activity with read-only audit access and local-only write capabilities.

## Quick Start

### Development

```bash
cd web
npm install
npm run dev
```

Open [http://localhost:3000](http://localhost:3000) in your browser.

### Production Build

```bash
npm run build
npm start
```

## Features

### 📊 Dashboard (`/`)
- **Candidate Cards**: Per-candidate stats (total, submitted, failed, companies)
- **Global Stats**: Total applications, submission rate, error count, average score
- **Run Controls**: Trigger job application runs (local only)
- **Live Log Panel**: Real-time bot activity with color-coded log levels
- **Resume Upload**: Drag-and-drop resume uploader with validation

### 📋 Applications (`/applications`)
- **Sortable Table**: Sort by applied date, score, company, candidate, or status
- **Filterable**: Filter by candidate, status, source, or company search
- **CSV Export**: Download filtered results as CSV
- **Real Data**: 16+ applications from sqlite database with full audit trail

### 📄 Report (`/report`)
- **Markdown Viewer**: Full job discovery and application report
- **Live Updates**: Refreshes when bot completes runs
- **Detailed Metrics**: Company boards queried, postings fetched, rejection reasons
- **Per-Candidate Breakdown**: Contact details, submission outcomes, unanswered fields

## API Routes

All API routes use `runtime = "nodejs"` and `dynamic = "force-dynamic"` for real-time behavior.

### `GET /api/applications`
Returns all job applications with aggregated statistics.

**Response:**
```json
{
  "rows": [
    {
      "id": 16,
      "candidateId": "harshank",
      "company": "Coinbase",
      "title": "Software Engineer, Security Platform",
      "status": "ERROR",
      "score": 64,
      "appliedAt": "2026-11-19T12:30:15.000Z",
      "source": "greenhouse"
    }
  ],
  "stats": {
    "total": 16,
    "submitted": 0,
    "failed": 10,
    "skipped": 6,
    "companies": 2,
    "avgScore": 66.0,
    "byStatus": { "ERROR": 10, "SKIPPED": 6 },
    "byCandidate": {
      "harshank": { "total": 2, "submitted": 0, "failed": 2, "companies": 2, "avgScore": 66.0 }
    }
  },
  "source": "sqlite",
  "error": null
}
```

### `GET /api/status`
Returns current bot run state.

**Response:**
```json
{
  "running": true,
  "pid": 18686,
  "startedAt": null,
  "candidate": null,
  "source": "external",
  "lastRun": null
}
```

### `POST /api/apply`
Trigger a job application run for a candidate (local only).

**Request:**
```json
{
  "candidate": "harshank",
  "limit": 20
}
```

**Response (200):**
```json
{
  "started": true,
  "candidate": "harshank",
  "pid": 12345
}
```

**Response (409 - Concurrent Run):**
```json
{
  "error": "A run is already in progress"
}
```

### `POST /api/resume`
Upload a resume file and update candidate profile (local only).

**Request:** Multipart form data
- `candidate`: Candidate ID (harshank or divya)
- `file`: PDF, DOC, or DOCX file (max 10MB)

**Response (200):**
```json
{
  "path": "/Users/apple/Desktop/AI/jobbot/data/resumes/harshank/resume-2026-11-19T12-30-15.pdf",
  "candidate": "harshank",
  "updated": true
}
```

**Response (409 - Concurrent Run):**
```json
{
  "error": "Cannot upload resume while a run is in progress"
}
```

**Response (413 - File Too Large):**
```json
{
  "error": "File exceeds 10MB limit"
}
```

**Response (415 - Invalid Type):**
```json
{
  "error": "Only PDF, DOC, and DOCX files are supported"
}
```

### `GET /api/report`
Returns the latest job application report in markdown format.

**Response:**
```json
{
  "markdown": "# AI Job Application Report\n\n**Run window (UTC):** 2026-11-19 12:29:59 -> 2026-11-19 12:30:15\n...",
  "updatedAt": "2026-11-19T12:30:15.398Z",
  "path": "/Users/apple/Desktop/AI/jobbot/reports/APPLICATIONS.md"
}
```

### `GET /api/logs`
Stream bot activity logs in real-time via Server-Sent Events (SSE), or return JSON.

**SSE Stream:**
```
data: [2026-11-19 12:30:15] INFO: Starting job application run for harshank
data: [2026-11-19 12:30:16] SUBMITTED: Applied to Coinbase - Software Engineer
data: [2026-11-19 12:30:17] ERROR: Form validation failed on Starburst posting
```

**JSON Fallback (`?tail=N`):**
```bash
curl http://localhost:3000/api/logs?tail=10
```

Returns last N lines as JSON array.

## Data Sources

### SQLite Database
- **Path**: `data/applications.db`
- **Table**: `attempts` (read-only)
- **Fields**: id, jobKey, candidateId, company, title, status, score, appliedAt, etc.
- **Primary Source**: Used when available, fallback to CSV

### CSV Fallback
- **Path**: `reports/applications.csv`
- **Used When**: SQLite unavailable
- **Format**: Standard CSV with headers

### Markdown Report
- **Path**: `reports/APPLICATIONS.md`
- **Updated**: After each bot run
- **Content**: Full job discovery metrics, per-candidate breakdown, application outcomes

### Log File
- **Path**: `logs/realrun.log`
- **Format**: Line-delimited timestamps + messages
- **Streaming**: SSE endpoint reads live from this file

### Configuration
- **Path**: `config/profiles.yaml`
- **Format**: YAML with candidate blocks
- **Resume Paths**: Line-based parsing, atomic writes via temp+rename

## Project Root Discovery

The dashboard automatically discovers the jobbot project root by:

1. Checking `JOBBOT_ROOT` environment variable (if set)
2. Walking up from current working directory to find `config/profiles.yaml` + `jobbot/__init__.py`
3. Falling back to parent of current working directory

**To override:**
```bash
export JOBBOT_ROOT=/path/to/jobbot
npm run dev
```

## Candidate Metadata

Hardcoded candidate information (from `src/lib/candidates.ts`):

### Harshank Kanungo
- **ID**: `harshank`
- **Title**: Full Stack Developer
- **Experience**: 3 years
- **Expected CTC**: 14-15 LPA
- **Email**: harshankkanungo259@gmail.com
- **Phone**: +91 8109774968
- **Location**: Gurugram, Haryana, India
- **Default Resume**: `/Users/apple/Desktop/AI/harshank/Harshank_Kanungo_Full_Stack_Developer_ATS.pdf`

### Divya Kanungo
- **ID**: `divya`
- **Title**: MERN Stack Developer
- **Experience**: 1 year
- **Expected CTC**: 4-5 LPA
- **Email**: From attempt rows or fallback
- **Phone**: From attempt rows or fallback
- **Location**: Gurugram, Haryana, India
- **Default Resume**: `/Users/apple/Desktop/AI/resume/Divya_Kanungo_MERN_Stack_Developer.pdf`

## Status Colors

Applications are color-coded by status:

| Status | Color | Meaning |
|--------|-------|---------|
| SUBMITTED | Emerald | Successfully submitted |
| SKIPPED | Slate | Skipped by bot logic |
| CLOSED | Amber | Job posting closed |
| ALREADY_APPLIED | Sky | Already applied to this company |
| LOGIN_REQUIRED | Amber | Login required on job board |
| ERROR | Rose | Technical failure |
| FAILED | Rose | Application failed |
| FAILED_VALIDATION | Rose | Form validation failed |

## Vercel Deployment

### Read-Only Features (Enabled)
- ✅ Dashboard with candidate cards and stats
- ✅ Applications table with sorting/filtering
- ✅ Report viewer with markdown rendering
- ✅ Log streaming (reads from disk)

### Write Features (Disabled)
- ❌ Apply trigger (no local bot access)
- ❌ Resume upload (no Chrome profile)

### Setup

1. Push to GitHub
2. Connect repository to Vercel
3. Set environment variable (if needed):
   ```
   JOBBOT_ROOT=/path/to/jobbot
   ```
4. Deploy

**Note**: The dashboard will work in read-only mode on Vercel. To enable write features, deploy locally or use a VPS with the jobbot bot running.

## Important Assumptions

### Data Integrity
- **No Fabricated Data**: Unknown values render as "—" (em dash), never invented
- **Real-Time**: All data loaded fresh on each request (no caching)
- **Audit Trail**: Every application attempt logged to sqlite + CSV + markdown

### Resume Upload
- **Local Only**: Requires Chrome profile at `data/chrome-profile`
- **Atomic Writes**: YAML patching via temp file + rename
- **Validation**: Only PDF, DOC, DOCX; max 10MB
- **Concurrent Safety**: Returns 409 if bot already running

### Apply Trigger
- **Local Only**: Spawns `uv run` via runner.ts
- **Concurrent Safety**: Returns 409 if bot already running
- **External Run Banner**: Shows when bot running from terminal
- **Dashboard Disabled**: Apply button disabled during external runs

### Log Streaming
- **SSE Backlog**: 200 lines on initial connection
- **Heartbeat**: Every 15 seconds (keeps connection alive)
- **Idle Grace**: Closes after 25 seconds of inactivity (no running bot, no new logs)
- **JSON Fallback**: `?tail=N` returns last N lines as JSON array

### TypeScript Strict Mode
- All code compiled with `strict: true`
- No `any` types, no implicit `unknown`
- All error paths handled explicitly
- Type-safe API responses

## Development

### File Structure
```
web/
├── src/
│   ├── app/
│   │   ├── page.tsx                 Dashboard page
│   │   ├── applications/page.tsx    Applications page
│   │   ├── report/page.tsx          Report page
│   │   ├── api/
│   │   │   ├── applications/route.ts
│   │   │   ├── status/route.ts
│   │   │   ├── report/route.ts
│   │   │   ├── apply/route.ts
│   │   │   ├── resume/route.ts
│   │   │   └── logs/route.ts
│   │   ├── layout.tsx               Root layout
│   │   └── globals.css              Tailwind theme
│   ├── components/
│   │   ├── dashboard.tsx            Main orchestrator
│   │   ├── applications-table.tsx   Sortable table
│   │   ├── candidate-card.tsx       Candidate stats
│   │   ├── resume-drop.tsx          Upload zone
│   │   ├── log-panel.tsx            SSE stream
│   │   ├── nav.tsx                  Navigation
│   │   └── ui.tsx                   Shared UI components
│   └── lib/
│       ├── paths.ts                 Project discovery
│       ├── types.ts                 TypeScript interfaces
│       ├── csv.ts                   CSV parsing
│       ├── applications.ts          Data loading
│       ├── candidates.ts            Metadata
│       ├── format.ts                Formatting
│       ├── status.ts                Styling
│       ├── profiles.ts              YAML management
│       └── runner.ts                Process management
├── package.json
├── tsconfig.json
├── next.config.ts
├── postcss.config.mjs
└── .gitignore
```

### Build & Test

```bash
# Type check
npm run build

# Development with hot reload
npm run dev

# Production build
npm run build
npm start

# Lint (if configured)
npm run lint
```

## Troubleshooting

### API returns HTML instead of JSON
- Dev server may be in error state
- Restart: `npm run dev`
- Check `/tmp/nextdev.log` for errors

### Resume upload fails with 409
- Bot is currently running
- Wait for bot to complete or kill the process
- Check `/api/status` to see current state

### Applications table shows no data
- SQLite database not found
- Check `JOBBOT_ROOT` environment variable
- Verify `data/applications.db` exists in jobbot project

### Log panel shows "closed" immediately
- Bot not running and log file is quiet
- This is expected behavior (idle grace timeout)
- Start a bot run to see live logs

## License

Part of the jobbot project. See parent repository for license details.
