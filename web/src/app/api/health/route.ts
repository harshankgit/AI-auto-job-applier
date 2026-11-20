import { withMac } from "@/lib/mac-proxy";
import { rootIsPresent } from "@/lib/paths";
import { getStatus } from "@/lib/runner";
import fs from "node:fs";
import path from "node:path";
import { PROJECT_ROOT } from "@/lib/paths";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** Cheap liveness probe used by the site badge, the tunnel loop and the self-test loop. */
async function handleGET() {
  const root = rootIsPresent();
  let selftest: unknown = null;
  if (root && PROJECT_ROOT) {
    try {
      selftest = JSON.parse(fs.readFileSync(path.join(PROJECT_ROOT, "data", "selftest.json"), "utf8"));
    } catch {
      selftest = null;
    }
  }
  let running = false;
  try {
    running = root ? (await getStatus()).running : false;
  } catch {
    running = false;
  }
  return Response.json({ ok: true, root, running, at: new Date().toISOString(), selftest });
}

export const GET = withMac(handleGET);
