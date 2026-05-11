# Max Self-Hosted Bot — Milestone A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Get `suren-pm/max-bot` to a state where Railway successfully builds, deploys, and serves `GET /health` returning HTTP 200 with `{"status": "ok"}` — the foundation for Milestones B–F.

**Architecture:** Add an Express HTTP server entrypoint to the existing meet-teams-bot TypeScript codebase. Keep the original Playwright bot machinery intact for use in later milestones; for now we just need a long-running web service that Railway can health-check. Dockerfile is adapted to run the server, `railway.toml` configures build/start commands.

**Tech Stack:** TypeScript, Node.js 22+, Express, Jest, supertest, Docker, Railway.

**Pre-conditions already in place:**
- GitHub repo `suren-pm/max-bot` exists (forked from `Meeting-BaaS/meet-teams-bot`)
- Railway project `max-self-hosted` exists (ID `1905056d-e126-4dad-8f3c-ed26bcbe720e`)
- Railway service `max-bot` is connected to the GitHub repo's `main` branch with auto-deploy enabled
- Current state: service is offline because the existing Dockerfile/scripts aren't shaped as a Railway-managed long-running service yet

**What ships at end of Milestone A:**
- `https://max-bot-production.up.railway.app/health` returns `200 {"status":"ok","service":"max-bot","version":"0.1.0"}`
- The existing meet-teams-bot Playwright code remains untouched (just dormant — no longer the entrypoint)
- Foundation for Milestones B–F is in place

---

## File Structure

| File | Action | Purpose |
|---|---|---|
| `src/server.ts` | Create | Express HTTP server entrypoint. Owns `/health`, will gain `/join` and `/leave` in later milestones. |
| `src/server.test.ts` | Create | Jest + supertest tests for the HTTP layer. |
| `package.json` | Modify | Add Express, supertest deps. Add `start` script pointing at compiled `dist/server.js`. |
| `tsconfig.json` | Modify (if needed) | Ensure `outDir: dist`, `rootDir: src`. |
| `Dockerfile` | Modify | Replace existing CMD with `node dist/server.js`. Keep build steps for Playwright + Chromium for later milestones. |
| `railway.toml` | Create | Build command, start command, health-check path. |
| `.gitignore` | Modify | Ensure `dist/` is ignored. |
| `docs/CLAUDE-NOTES.md` | Create | Living notes file: what we discovered about the existing codebase, decisions made, gotchas. Lightweight project-memory inside the repo. |

**Files we deliberately do NOT touch in Milestone A:**
- Everything in existing `src/` directory under the original meet-teams-bot architecture (recording flow, Playwright orchestrator, audio routing). All of it stays as-is. We add `server.ts` alongside it; we don't refactor or delete any existing code in this milestone.

---

## Pre-work — local clone and dependency setup

### Task A.0: Clone the repo locally for dev work

**Files:** None modified in this task; this is environment setup.

- [ ] **Step 1: Clone the repo to a working directory**

```bash
cd ~/Documents/Claude/
git clone git@github.com:suren-pm/max-bot.git
cd max-bot
```

Expected: clone succeeds, `~/Documents/Claude/max-bot/` exists with the full repo contents.

- [ ] **Step 2: Verify on main branch, fresh fork state**

```bash
git status
git log --oneline -3
```

Expected: `On branch main`, recent commits match upstream `Meeting-BaaS/meet-teams-bot`.

- [ ] **Step 3: Install dependencies**

```bash
npm install
```

Expected: `node_modules/` populated, no fatal errors. Warnings about peer deps are OK.

- [ ] **Step 4: Verify Node version**

```bash
node --version
```

Expected: Node 22+ (per `package.json` engines field if present, or Railway's default).

If Node version is too old, use `nvm install 22 && nvm use 22` before continuing.

---

## Task A.1: Read existing repo to understand its shape

**Goal:** Document what's in there so we don't accidentally break the recording flow we'll need in later milestones.

**Files to create:**
- `docs/CLAUDE-NOTES.md`

- [ ] **Step 1: Read `README.md` and note the existing entrypoint**

```bash
cat README.md | head -100
```

Expected: README describes `./run_bot.sh build` and `./run_bot.sh run` as the existing entrypoints. Note this in CLAUDE-NOTES.md.

- [ ] **Step 2: Read `package.json` and capture scripts + main**

```bash
cat package.json
```

Expected: see `scripts`, `dependencies`, `main`, `engines` fields. Record what `npm start`, `npm run build`, etc. currently do.

- [ ] **Step 3: Read `run_bot.sh`**

```bash
cat run_bot.sh
```

Expected: shell script that builds the Docker image and runs the bot container with config.

- [ ] **Step 4: Read `Dockerfile`**

```bash
cat Dockerfile
```

Expected: multi-stage build, installs Playwright + Chromium, copies source, builds TypeScript, sets CMD. Note the current CMD value.

- [ ] **Step 5: List `src/` structure**

```bash
ls src/
find src/ -name "*.ts" | head -20
```

Expected: see the existing TypeScript files. Don't read them all yet — just get a directory map.

- [ ] **Step 6: Write `docs/CLAUDE-NOTES.md` with findings**

Create the file with this template, filling in actual observed values:

```markdown
# Max-Bot Notes (rolling)

## What's here from upstream meet-teams-bot

- **Entrypoint (existing):** `./run_bot.sh run` → Docker container → CMD in Dockerfile is `[FILL IN]`
- **Main TS file (existing):** `src/[FILL IN]` — handles the original recording flow
- **package.json scripts:** [list them]
- **Key dependencies:** Playwright, [others]
- **Node version:** [from engines]

## What we're adding in Milestone A

- `src/server.ts` — Express HTTP server with `/health` endpoint
- Test infrastructure: `src/server.test.ts` using Jest + supertest
- New Dockerfile CMD: `node dist/server.js`
- `railway.toml` for Railway-specific config

## Decisions we made

- (List as they come up during implementation)

## Gotchas discovered

- (List as they come up)
```

- [ ] **Step 7: Commit the notes file**

```bash
git checkout -b milestone-a/skeleton
git add docs/CLAUDE-NOTES.md
git commit -m "docs: initial notes on existing meet-teams-bot structure"
```

Expected: branch created, commit made.

---

## Task A.2: Add Express + supertest dependencies

**Files:**
- Modify: `package.json`

- [ ] **Step 1: Install Express and its types**

```bash
npm install express
npm install --save-dev @types/express
```

Expected: package.json updated with Express dependency, types in devDependencies.

- [ ] **Step 2: Install supertest for HTTP testing**

```bash
npm install --save-dev supertest @types/supertest
```

Expected: package.json devDependencies includes supertest.

- [ ] **Step 3: Verify package.json was updated correctly**

```bash
grep -E "express|supertest" package.json
```

Expected: both `express` and `supertest` appear in the appropriate dependency sections.

- [ ] **Step 4: Commit dependency changes**

```bash
git add package.json package-lock.json
git commit -m "deps: add express + supertest for the HTTP server skeleton"
```

---

## Task A.3: Write failing test for `/health` endpoint (TDD red)

**Files:**
- Create: `src/server.test.ts`

- [ ] **Step 1: Create the test file with the contract**

```typescript
// src/server.test.ts
import request from "supertest";
import { createServer } from "./server";

describe("max-bot HTTP server", () => {
  describe("GET /health", () => {
    it("responds with 200 and a status payload", async () => {
      const app = createServer();
      const res = await request(app).get("/health");
      expect(res.status).toBe(200);
      expect(res.body).toMatchObject({
        status: "ok",
        service: "max-bot",
      });
      expect(typeof res.body.version).toBe("string");
    });
  });
});
```

- [ ] **Step 2: Run the test and verify it fails**

```bash
npx jest src/server.test.ts
```

Expected: FAIL with "Cannot find module './server'" or similar. This is the red state.

---

## Task A.4: Implement the minimal Express server (TDD green)

**Files:**
- Create: `src/server.ts`

- [ ] **Step 1: Create `src/server.ts` with the minimum to pass the test**

```typescript
// src/server.ts
import express, { Application, Request, Response } from "express";

const VERSION = "0.1.0";

export function createServer(): Application {
  const app = express();
  app.use(express.json());

  app.get("/health", (_req: Request, res: Response) => {
    res.status(200).json({
      status: "ok",
      service: "max-bot",
      version: VERSION,
    });
  });

  return app;
}

// Allow running directly: `node dist/server.js` on Railway.
// PORT is provided by Railway; default 8080 for local dev.
if (require.main === module) {
  const port = Number(process.env.PORT) || 8080;
  const app = createServer();
  app.listen(port, () => {
    // eslint-disable-next-line no-console
    console.log(`max-bot listening on :${port}`);
  });
}
```

- [ ] **Step 2: Run the test again — should pass**

```bash
npx jest src/server.test.ts
```

Expected: 1 passed, 0 failed.

- [ ] **Step 3: Try running the server locally**

```bash
npx ts-node src/server.ts &
sleep 2
curl -s http://localhost:8080/health
kill %1
```

Expected: JSON response `{"status":"ok","service":"max-bot","version":"0.1.0"}`.

If `ts-node` isn't installed: `npm install --save-dev ts-node`.

- [ ] **Step 4: Commit**

```bash
git add src/server.ts src/server.test.ts package.json package-lock.json
git commit -m "feat(server): minimal Express HTTP server with /health endpoint"
```

---

## Task A.5: Add npm scripts for build/start/test

**Files:**
- Modify: `package.json`

- [ ] **Step 1: Read current scripts**

```bash
node -e "console.log(JSON.stringify(require('./package.json').scripts, null, 2))"
```

Note the existing scripts so we don't override anything important.

- [ ] **Step 2: Add or update scripts in `package.json`**

Open `package.json`, ensure the `scripts` object contains at least these entries (preserving any existing ones):

```json
{
  "scripts": {
    "build": "tsc -p tsconfig.json",
    "start": "node dist/server.js",
    "dev": "ts-node src/server.ts",
    "test": "jest"
  }
}
```

If `build` already exists with a different command (e.g., for the recording bot), rename it to `build:bot` and use `build` for our TypeScript compilation. Document in CLAUDE-NOTES.md.

- [ ] **Step 3: Verify TypeScript can compile**

```bash
npm run build
```

Expected: `dist/` directory created with `server.js` inside. No compile errors.

If errors appear in OTHER files (existing src code), examine — most likely a tsconfig setting. Do NOT modify the existing code; instead adjust `tsconfig.json` `include` to only build `src/server.ts` for Milestone A, e.g.:

```json
{
  "compilerOptions": { /* keep existing options */ },
  "include": ["src/server.ts", "src/server.test.ts"]
}
```

Note this scoping in CLAUDE-NOTES.md — we widen `include` later when other files become Milestone targets.

- [ ] **Step 4: Verify built server actually runs**

```bash
node dist/server.js &
sleep 2
curl -s http://localhost:8080/health
kill %1
```

Expected: same JSON response as before.

- [ ] **Step 5: Commit**

```bash
git add package.json tsconfig.json
git commit -m "build: npm scripts for the server (build, start, dev, test)"
```

---

## Task A.6: Adapt Dockerfile to run the new server

**Files:**
- Modify: `Dockerfile`

- [ ] **Step 1: Re-read the existing Dockerfile**

```bash
cat Dockerfile
```

Note the current CMD line (the line beginning `CMD ` near the bottom).

- [ ] **Step 2: Change the CMD to run our server**

Edit `Dockerfile`. Find the existing `CMD` line and replace ONLY that line with:

```dockerfile
CMD ["node", "dist/server.js"]
```

Keep everything else (FROM, RUN, COPY, ENV, etc.) exactly as is — the existing Playwright + Chromium install steps are needed in later milestones.

- [ ] **Step 3: Ensure the build step compiles our server**

Find the `RUN` line that does the TypeScript build (likely `RUN npm run build` or similar). If not present, add it after `COPY . .`:

```dockerfile
RUN npm run build
```

This produces `dist/server.js` inside the container.

- [ ] **Step 4: Ensure PORT is exposed**

Add (if not already present, near the bottom before CMD):

```dockerfile
EXPOSE 8080
```

Railway sets `PORT` dynamically and routes traffic to whatever port the container listens on — `EXPOSE` is documentation-only but standard.

- [ ] **Step 5: Build the Docker image locally to catch problems early**

```bash
docker build -t max-bot:local .
```

Expected: image builds successfully. Note any errors and address them before continuing.

- [ ] **Step 6: Run the image locally**

```bash
docker run --rm -p 8080:8080 -e PORT=8080 max-bot:local &
sleep 5
curl -s http://localhost:8080/health
docker stop $(docker ps -q --filter ancestor=max-bot:local)
```

Expected: `/health` returns the JSON response from inside the container.

- [ ] **Step 7: Commit**

```bash
git add Dockerfile
git commit -m "build(docker): point CMD at dist/server.js for Railway runtime"
```

---

## Task A.7: Add `railway.toml` for Railway config

**Files:**
- Create: `railway.toml`

- [ ] **Step 1: Create `railway.toml` at the repo root**

```toml
# railway.toml
# Build and run configuration for the max-bot service.

[build]
builder = "DOCKERFILE"
dockerfilePath = "Dockerfile"

[deploy]
startCommand = "node dist/server.js"
healthcheckPath = "/health"
healthcheckTimeout = 60
restartPolicyType = "ON_FAILURE"
restartPolicyMaxRetries = 3
```

- [ ] **Step 2: Verify TOML syntax**

```bash
node -e "const fs = require('fs'); console.log(fs.readFileSync('railway.toml', 'utf8'))"
```

Expected: file content prints cleanly. If using TOML linter (`npx @taplo/cli format railway.toml`), no errors.

- [ ] **Step 3: Commit**

```bash
git add railway.toml
git commit -m "build(railway): config for build, start, and /health healthcheck"
```

---

## Task A.8: Confirm `.gitignore` ignores `dist/`

**Files:**
- Modify: `.gitignore` (only if needed)

- [ ] **Step 1: Check current `.gitignore`**

```bash
grep -E "^dist" .gitignore
```

- [ ] **Step 2: Add `dist/` if not already there**

If grep returned nothing, append:

```bash
echo "dist/" >> .gitignore
git add .gitignore
git commit -m "build: ignore dist/ compiled output"
```

If `dist/` already appears, skip this task.

---

## Task A.9: Push branch, open PR, merge to main, watch Railway deploy

**Files:** None modified.

- [ ] **Step 1: Push the branch**

```bash
git push -u origin milestone-a/skeleton
```

Expected: branch published to GitHub.

- [ ] **Step 2: Open a PR on GitHub**

Navigate to `https://github.com/suren-pm/max-bot/pull/new/milestone-a/skeleton`. PR title:

```
Milestone A: HTTP server skeleton with /health endpoint
```

PR body:

```
Adds an Express HTTP server entrypoint to make the repo deployable as a Railway-managed service.

Changes:
- src/server.ts: minimal Express server with /health
- src/server.test.ts: Jest+supertest test for /health
- Dockerfile: CMD now runs node dist/server.js
- railway.toml: build + healthcheck config
- package.json: build/start/dev/test scripts
- docs/CLAUDE-NOTES.md: rolling notes about discoveries

Existing recording-bot code is untouched. This is just the skeleton — Milestones B–F add Playwright join, audio capture, audio injection, max-brain integration, and hardening.

Acceptance: /health returns 200 from Railway URL.
```

- [ ] **Step 3: Merge the PR**

Either via GitHub UI or via CLI:

```bash
gh pr merge --merge --delete-branch
```

(If `gh` isn't installed, use the GitHub UI green Merge button.)

Expected: PR merged, `milestone-a/skeleton` branch deleted, `main` updated.

- [ ] **Step 4: Watch Railway auto-deploy**

Navigate to `https://railway.com/project/1905056d-e126-4dad-8f3c-ed26bcbe720e`. Watch the max-bot service → Deployments tab. A new deployment should appear within ~30 seconds of the merge.

Expected timeline:
- ~30 s: new deploy appears, status `BUILDING`
- ~2–5 min: status `DEPLOYING` then `ACTIVE`
- If it fails: read the build logs (`View logs` button), find the error, fix on a new branch, re-PR.

- [ ] **Step 5: Verify the live /health endpoint**

Once the deploy shows `ACTIVE`:

```bash
curl -s https://max-bot-production.up.railway.app/health | python3 -m json.tool
```

Expected:

```json
{
    "status": "ok",
    "service": "max-bot",
    "version": "0.1.0"
}
```

If you get 404 / 502 / timeout, check Railway's logs and adjust.

---

## Task A.10: Update `CLAUDE-NOTES.md` with what we learned

**Files:**
- Modify: `docs/CLAUDE-NOTES.md`

- [ ] **Step 1: Append a "Milestone A — completed" section**

Open `docs/CLAUDE-NOTES.md`, add at the bottom:

```markdown
## Milestone A — completed YYYY-MM-DD

- Service deploys cleanly on Railway from the `main` branch
- `/health` returns the expected JSON
- Existing recording-bot code is untouched
- Decisions that came up:
  - (list any decisions made during the milestone)
- Gotchas encountered:
  - (list any gotchas)
- Time spent: (note hours)

Ready for Milestone B: Playwright join flow.
```

- [ ] **Step 2: Commit directly to main**

```bash
git checkout main
git pull
git add docs/CLAUDE-NOTES.md
git commit -m "docs: milestone A complete notes"
git push
```

---

## Milestone A acceptance checklist

- [ ] `https://max-bot-production.up.railway.app/health` returns HTTP 200 with the expected JSON
- [ ] Railway service status shows `ACTIVE` with a green deployment
- [ ] Jest test `src/server.test.ts` passes locally
- [ ] Docker build succeeds locally
- [ ] No existing meet-teams-bot files were modified except the Dockerfile `CMD` line
- [ ] `docs/CLAUDE-NOTES.md` documents what was done and any decisions/gotchas

When all six are checked, Milestone A is complete and we are ready to start Milestone B.

---

# Milestones B–F (roadmap-level — detailed plans deferred)

Detailed bite-sized plans for each of the below will be written via `superpowers:writing-plans` at the start of that milestone. Trying to plan them now in TDD detail would be largely fiction — too many architectural decisions depend on what we discover during Milestone A and earlier milestones.

## Milestone B — Playwright join flow

**Goal:** `POST /join {meeting_url, bot_name}` causes a Chromium instance inside the max-bot container to join the specified Google Meet, with the named bot visible in the waiting room.

**Likely shape:**
- New file `src/bot/joinMeet.ts` — wraps the existing meet-teams-bot Playwright code with a function-call interface (instead of CLI args)
- Add `POST /join` route to `src/server.ts` that spawns a Playwright process and returns a `bot_id`
- In-memory map `Map<bot_id, BrowserContext>` for tracking
- Test: dev-mode acceptance test with a real test meeting URL — manual verification via screenshot

**Acceptance:** Suren joins TEST meeting `mmg-mjgn-njd`, sends `POST /join` via curl, bot appears in the waiting room with name "Max" within 30 s.

**Estimate:** 3–5 days

## Milestone C — Audio capture out (meeting → max-brain)

**Goal:** Audio Max hears from the meeting reaches a WebSocket endpoint as 16 kHz mono PCM in 100 ms chunks (matching the protocol max-brain expects from MBaaS today).

**Likely shape:**
- New file `src/audio/capture.ts` — wires Chromium's audio output through a PulseAudio sink → ffmpeg resample → byte stream
- New WebSocket endpoint `/ws/{bot_id}` on the Express server
- Route the audio stream to the WebSocket

**Acceptance:** A test client connects to `/ws/{bot_id}`, receives PCM that, when saved to a WAV file, plays back as recognizable meeting audio.

**Estimate:** 3–5 days. PulseAudio in Docker is the main risk.

## Milestone D — Audio injection in (max-brain → meeting)

**Goal:** PCM audio pushed to the bot via WebSocket is heard by other meeting participants as the bot's voice.

**Likely shape:**
- New file `src/audio/inject.ts` — accepts PCM from WebSocket, writes to a PulseAudio source, that source becomes Chrome's microphone input via `--use-fake-ui-for-media-stream` and `--use-file-for-fake-audio-capture` patterns (or similar)
- Bidirectional WebSocket from Milestone C is extended

**Acceptance:** A test client sends a pre-recorded WAV via WebSocket to the bot; Suren in the TEST meeting hears that clip play from the bot.

**Estimate:** 5–7 days. **This is the hardest milestone** — Chrome's `getUserMedia` + PulseAudio routing in Docker is notoriously finicky. Have a fallback plan (e.g., dedicated Ubuntu VPS instead of Railway) if Docker proves intractable after a week.

## Milestone E — Integrate with max-brain

**Goal:** Suren triggers via curl → max-bot joins Meet → audio flows bidirectionally → real Pipecat conversation works end-to-end with Claude Haiku 4.5 brain.

**Likely shape:**
- Modify `max-brain` repo's `/join` handler — point at `${BOT_SERVICE_URL}/join` instead of MBaaS
- Verify WebSocket protocol is byte-identical
- Re-deploy max-brain (un-pause it) pointing at the new bot service URL
- End-to-end test in TEST meeting

**Acceptance:** Suren has a 5-minute conversation with Max via the self-hosted bot. Quality is comparable to MBaaS-Max at V1.4.

**Estimate:** 2–3 days

## Milestone F — Hardening

**Goal:** Stable for 30+ minute sessions with no crashes, clean cleanup when meeting ends, basic anti-bot resilience.

**Likely shape:**
- Auto-restart Playwright on crash
- Detect meeting-ended state, run cleanup
- Add timeouts and resource limits to prevent zombie Chrome processes
- Basic Playwright stealth-mode flags
- Health monitoring beyond /health (e.g., bot count, meeting durations)

**Acceptance:** Three back-to-back 30-min sessions without manual intervention, no crashes, clean state between sessions.

**Estimate:** 3–5 days

---

## Self-review

**Spec coverage:**
- ✅ Milestone A acceptance criterion from the spec (`/health` returns 200 from Railway) is the explicit acceptance criterion of this plan.
- ✅ Architecture description from spec section 3 is reflected — we add `src/server.ts` as the new entrypoint, keep existing meet-teams-bot code untouched.
- ✅ Existing-code-untouched principle from spec section 9 ("max-brain stays mostly unchanged" and the spirit of "existing build is untouched") is preserved on the max-bot side too — we add files alongside, we don't delete or refactor.
- ✅ Milestones B–F outlined at roadmap level with explicit acceptance criteria and estimates.

**Placeholder scan:**
- No "TBD" or "implement later" in the detailed Milestone A tasks.
- Where a step says "fill in" (e.g., the CLAUDE-NOTES.md template), it's a notes file where the engineer records observations during exploration — that's the correct shape, not a plan failure.

**Type consistency:**
- `createServer()` function defined in Task A.4 is called by name in Task A.3 (the test). Type: `() => Application`. Consistent.
- Endpoint shape `{status, service, version}` from the test in A.3 matches the implementation in A.4.

**Scope check:**
- Plan is focused on Milestone A (1–2 days of work).
- B–F are outlined but not detailed — explicit decomposition rationale given.
- Acceptance criterion is concrete and testable.

No issues found. Plan ready for execution.
