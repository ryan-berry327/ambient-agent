"""Phase B builder: scaffold from brief, smoke-test dummy data, optional git push."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Any, Optional

from app.config import settings
from app import database
from app.database import BriefModel, BuildRunModel, SessionModel, utcnow
from app.schemas import BuildRunOut
from app.services.ws_hub import ws_hub

logger = logging.getLogger(__name__)


def _duration_sec(started_at, finished_at) -> float:
    if not started_at or not finished_at:
        return 0.0
    if started_at.tzinfo is None and finished_at.tzinfo is not None:
        finished_at = finished_at.replace(tzinfo=None)
    elif started_at.tzinfo is not None and finished_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=None)
    return (finished_at - started_at).total_seconds()


class BuilderService:
    def __init__(self) -> None:
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    def get_latest(self, session_id: str) -> Optional[BuildRunOut]:
        db = database.SessionLocal()
        try:
            row = (
                db.query(BuildRunModel)
                .filter(BuildRunModel.session_id == session_id)
                .order_by(BuildRunModel.id.desc())
                .first()
            )
            return self._row_to_out(row) if row else None
        finally:
            db.close()

    async def start_build(self, session_id: str) -> BuildRunOut:
        if self._running:
            raise RuntimeError("A build is already in progress")
        self._running = True
        try:
            run_id = await asyncio.to_thread(self._create_run, session_id)
            await ws_hub.broadcast(
                "build.started",
                {"session_id": session_id, "build_id": run_id},
            )
            out = await asyncio.to_thread(self._execute_build, run_id)
            await ws_hub.broadcast("build.updated", out.model_dump(mode="json"))
            return out
        finally:
            self._running = False

    def _create_run(self, session_id: str) -> int:
        db = database.SessionLocal()
        try:
            session = db.get(SessionModel, session_id)
            if not session:
                raise LookupError("Session not found")

            brief = (
                db.query(BriefModel)
                .filter(BriefModel.session_id == session_id)
                .order_by(BriefModel.created_at.desc())
                .first()
            )
            if not brief:
                raise ValueError("Generate a brief before building")
            if not brief.selected_pathway_id:
                raise ValueError("Select a pathway before building")
            if json.loads(brief.viability_json).get("status") == "red":
                raise ValueError("Brief viability is red — refine requirements before building")

            row = BuildRunModel(
                session_id=session_id,
                spec_version=session.spec_version,
                brief_id=brief.id,
                pathway_id=brief.selected_pathway_id,
                status="queued",
                started_at=utcnow(),
                finished_at=None,
                files_changed="[]",
                agent_summary="",
                duration_sec=0.0,
                cost_usd=0.0,
                test_status="pending",
                test_log="",
                push_status="pending",
                repo_url="",
                error="",
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return row.id
        finally:
            db.close()

    def _execute_build(self, run_id: int) -> BuildRunOut:
        db = database.SessionLocal()
        try:
            row = db.get(BuildRunModel, run_id)
            if not row:
                raise LookupError("Build run not found")
            brief = db.get(BriefModel, row.brief_id) if row.brief_id else None
            if not brief:
                row.status = "failed"
                row.error = "Brief missing"
                row.finished_at = utcnow()
                db.commit()
                return self._row_to_out(row)

            row.status = "running"
            db.commit()

            actionable = json.loads(brief.actionable_json)
            pathways = json.loads(brief.pathways_json)
            pathway = next(
                (p for p in pathways if p.get("id") == row.pathway_id),
                pathways[0] if pathways else {"id": "exact", "title": "Exact ask"},
            )

            # Pathway may narrow scope
            if row.pathway_id == "mvp":
                confirmed = [i for i in actionable if i.get("status") == "confirmed"]
                build_items = confirmed or actionable[: max(1, min(3, len(actionable)))]
            elif row.pathway_id == "efficient":
                build_items = actionable[: max(1, min(3, len(actionable)))]
            else:
                build_items = actionable

            out_dir = settings.builds_dir / f"session_{row.session_id[:8]}_build_{run_id}"
            if out_dir.exists():
                shutil.rmtree(out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)

            files = self._scaffold_project(
                out_dir=out_dir,
                goal=brief.goal,
                summary=brief.summary,
                pathway=pathway,
                items=build_items,
                constraints=json.loads(brief.viability_json).get("constraints") or [],
            )

            row.files_changed = json.dumps(files)
            row.status = "testing"
            db.commit()

            test_ok, test_log = self._run_smoke(out_dir)
            row.test_status = "passed" if test_ok else "failed"
            row.test_log = test_log[:8000]

            if not test_ok:
                row.status = "failed"
                row.error = "Dummy-data smoke test failed"
                row.finished_at = utcnow()
                row.duration_sec = _duration_sec(row.started_at, row.finished_at)
                row.agent_summary = (
                    f"Scaffolded {len(files)} files for pathway '{pathway.get('title')}' "
                    f"but smoke tests failed."
                )
                db.commit()
                return self._row_to_out(row)

            row.status = "pushing"
            db.commit()
            push_status, repo_url, push_log = self._maybe_push(out_dir, row.session_id, run_id)
            row.push_status = push_status
            row.repo_url = repo_url
            if push_log:
                row.test_log = (row.test_log + "\n\n--- push ---\n" + push_log)[:8000]

            row.status = "succeeded" if push_status in ("pushed", "local_only") else "failed"
            if row.status == "failed" and not row.error:
                row.error = "Git push failed"
            row.finished_at = utcnow()
            row.duration_sec = _duration_sec(row.started_at, row.finished_at)
            row.agent_summary = (
                f"Built pathway '{pathway.get('title')}' with {len(build_items)} requirement(s). "
                f"Smoke: {row.test_status}. Push: {row.push_status}."
            )
            db.commit()
            db.refresh(row)
            return self._row_to_out(row)
        except Exception as exc:
            logger.exception("Build failed: %s", exc)
            row = db.get(BuildRunModel, run_id)
            if row:
                row.status = "failed"
                row.error = str(exc)
                row.finished_at = utcnow()
                row.duration_sec = _duration_sec(row.started_at, row.finished_at)
                db.commit()
                return self._row_to_out(row)
            raise
        finally:
            db.close()

    def _scaffold_project(
        self,
        out_dir: Path,
        goal: str,
        summary: str,
        pathway: dict[str, Any],
        items: list[dict[str, Any]],
        constraints: list[str],
    ) -> list[str]:
        src = out_dir / "src"
        data_dir = src / "data"
        scripts = out_dir / "scripts"
        src.mkdir(parents=True, exist_ok=True)
        data_dir.mkdir(parents=True, exist_ok=True)
        scripts.mkdir(parents=True, exist_ok=True)

        features = []
        for item in items:
            req = item.get("requirement") or "Feature"
            cat = (item.get("category") or "general").lower()
            slug = re.sub(r"[^a-z0-9]+", "_", req.lower()).strip("_")[:40] or "feature"
            features.append(
                {
                    "id": item.get("id") or slug,
                    "slug": slug,
                    "title": req,
                    "category": cat,
                    "acceptance": item.get("acceptance_hint") or "Returns expected dummy payload",
                    "status": item.get("status") or "tentative",
                }
            )

        dummy = {
            "goal": goal,
            "pathway": pathway.get("id"),
            "generated_at": utcnow().isoformat(),
            "sales": [
                {"month": "Jan", "revenue": 12000},
                {"month": "Feb", "revenue": 15000},
                {"month": "Mar", "revenue": 18000},
            ],
            "users": [{"id": "u1", "email": "demo@example.com", "role": "viewer"}],
            "features": features,
        }
        (data_dir / "dummy.json").write_text(json.dumps(dummy, indent=2), encoding="utf-8")

        pipeline_js = textwrap.dedent(
            """\
            import { readFileSync } from "node:fs";
            import { fileURLToPath } from "node:url";
            import { dirname, join } from "node:path";

            const __dirname = dirname(fileURLToPath(import.meta.url));

            export function loadDummy() {
              const raw = readFileSync(join(__dirname, "data", "dummy.json"), "utf8");
              return JSON.parse(raw);
            }

            /** Simulate end-to-end dummy data flow for the briefed features. */
            export function runPipeline(data = loadDummy()) {
              const salesTotal = (data.sales || []).reduce((sum, row) => sum + Number(row.revenue || 0), 0);
              const featureResults = (data.features || []).map((f) => {
                if (f.category === "auth") {
                  return { id: f.id, ok: Boolean(data.users?.length), kind: "auth_login_mock" };
                }
                if (f.category === "data" || /chart|revenue|sales/i.test(f.title)) {
                  return { id: f.id, ok: salesTotal > 0, kind: "chart_series", value: salesTotal };
                }
                if (f.category === "api" || /csv|export/i.test(f.title)) {
                  const csv = ["month,revenue", ...(data.sales || []).map((r) => `${r.month},${r.revenue}`)].join("\\n");
                  return { id: f.id, ok: csv.includes("revenue"), kind: "csv_export", bytes: csv.length };
                }
                return { id: f.id, ok: true, kind: "generic", title: f.title };
              });
              const ok = featureResults.every((r) => r.ok) && featureResults.length > 0;
              return {
                ok,
                goal: data.goal,
                pathway: data.pathway,
                salesTotal,
                featureResults,
                userCount: (data.users || []).length,
              };
            }
            """
        )
        (src / "pipeline.js").write_text(pipeline_js, encoding="utf-8")

        server_js = textwrap.dedent(
            """\
            import http from "node:http";
            import { runPipeline } from "./pipeline.js";

            const port = Number(process.env.PORT || 4173);
            const server = http.createServer((req, res) => {
              if (req.url === "/health") {
                res.writeHead(200, { "Content-Type": "application/json" });
                res.end(JSON.stringify({ status: "ok" }));
                return;
              }
              if (req.url === "/api/pipeline") {
                const result = runPipeline();
                res.writeHead(result.ok ? 200 : 500, { "Content-Type": "application/json" });
                res.end(JSON.stringify(result, null, 2));
                return;
              }
              res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
              res.end(`<!doctype html><html><body>
                <h1>Ambient Build Preview</h1>
                <p>GET <a href="/api/pipeline">/api/pipeline</a> for dummy-data flow.</p>
              </body></html>`);
            });
            server.listen(port, () => console.log(`listening on ${port}`));
            """
        )
        (src / "server.js").write_text(server_js, encoding="utf-8")

        smoke = textwrap.dedent(
            """\
            import { runPipeline } from "../src/pipeline.js";

            const result = runPipeline();
            console.log(JSON.stringify(result, null, 2));
            if (!result.ok) {
              console.error("SMOKE FAILED: dummy data did not flow through all features");
              process.exit(1);
            }
            if (!result.featureResults?.length) {
              console.error("SMOKE FAILED: no features exercised");
              process.exit(1);
            }
            console.log("SMOKE PASSED");
            """
        )
        (scripts / "smoke_dummy.mjs").write_text(smoke, encoding="utf-8")

        package_json = {
            "name": "ambient-build",
            "version": "0.1.0",
            "private": True,
            "type": "module",
            "scripts": {
                "start": "node src/server.js",
                "smoke": "node scripts/smoke_dummy.mjs",
                "test": "npm run smoke",
            },
        }
        (out_dir / "package.json").write_text(json.dumps(package_json, indent=2), encoding="utf-8")

        readme = textwrap.dedent(
            f"""\
            # Ambient Build

            Auto-generated from an ambient call brief.

            ## Goal
            {goal}

            ## Pathway
            **{pathway.get('title', pathway.get('id'))}** — {pathway.get('summary', '')}

            ## Summary
            {summary}

            ## Constraints
            {chr(10).join(f'- {c}' for c in constraints) or '- None listed'}

            ## Requirements
            {chr(10).join(f"- ({f['status']}) {f['title']}" for f in features) or '- None'}

            ## Smoke test
            ```bash
            npm test
            ```
            Exercises dummy sales/auth/export flow through `src/pipeline.js`.
            """
        )
        (out_dir / "README.md").write_text(readme, encoding="utf-8")
        (out_dir / ".gitignore").write_text("node_modules/\n.DS_Store\n", encoding="utf-8")

        brief_snapshot = {
            "goal": goal,
            "summary": summary,
            "pathway": pathway,
            "items": items,
            "constraints": constraints,
        }
        (out_dir / "BRIEF.json").write_text(json.dumps(brief_snapshot, indent=2), encoding="utf-8")

        rels = [
            "package.json",
            "README.md",
            ".gitignore",
            "BRIEF.json",
            "src/pipeline.js",
            "src/server.js",
            "src/data/dummy.json",
            "scripts/smoke_dummy.mjs",
        ]
        return rels

    def _run_smoke(self, out_dir: Path) -> tuple[bool, str]:
        try:
            proc = subprocess.run(
                ["node", "scripts/smoke_dummy.mjs"],
                cwd=out_dir,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            log = (proc.stdout or "") + (proc.stderr or "")
            return proc.returncode == 0, log.strip() or f"exit={proc.returncode}"
        except FileNotFoundError:
            # Node missing — fall back to Python validation of dummy flow
            return self._python_smoke_fallback(out_dir)
        except Exception as exc:
            return False, str(exc)

    def _python_smoke_fallback(self, out_dir: Path) -> tuple[bool, str]:
        try:
            data = json.loads((out_dir / "src" / "data" / "dummy.json").read_text(encoding="utf-8"))
            sales_total = sum(int(r.get("revenue") or 0) for r in data.get("sales") or [])
            features = data.get("features") or []
            if not features:
                return False, "No features in dummy.json"
            if sales_total <= 0 and any(
                f.get("category") == "data" or re.search(r"chart|revenue|sales", f.get("title") or "", re.I)
                for f in features
            ):
                return False, "Sales dummy data empty"
            return True, f"PYTHON_SMOKE_PASSED features={len(features)} salesTotal={sales_total}"
        except Exception as exc:
            return False, f"python smoke failed: {exc}"

    def _maybe_push(self, out_dir: Path, session_id: str, run_id: int) -> tuple[str, str, str]:
        """Init git repo and push when GitHub is configured; otherwise local_only."""
        token = settings.github_token or os.environ.get("GITHUB_TOKEN", "")
        repo = settings.github_repo  # owner/name
        logs: list[str] = []

        def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                cmd,
                cwd=out_dir,
                capture_output=True,
                text=True,
                check=False,
            )

        init = run(["git", "init", "-b", "main"])
        logs.append(init.stdout + init.stderr)
        run(["git", "config", "user.email", "ambient-agent@local"])
        run(["git", "config", "user.name", "Ambient Call Agent"])
        run(["git", "add", "."])
        commit = run(["git", "commit", "-m", f"feat: ambient build from session {session_id[:8]}"])
        logs.append(commit.stdout + commit.stderr)
        if commit.returncode != 0:
            return "failed", "", "\n".join(logs)

        if not token or not repo:
            logs.append("GITHUB_TOKEN/GITHUB_REPO not set — keeping local build only")
            return "local_only", str(out_dir), "\n".join(logs)

        remote = f"https://x-access-token:{token}@github.com/{repo}.git"
        branch = f"ambient-build/{session_id[:8]}-{run_id}"
        run(["git", "checkout", "-B", branch])
        # Prefer pushing a branch; remote may already exist
        run(["git", "remote", "remove", "origin"])
        add = run(["git", "remote", "add", "origin", remote])
        logs.append(add.stdout + add.stderr)
        push = run(["git", "push", "-u", "origin", branch])
        logs.append(push.stdout + push.stderr)
        # Redact token from logs
        redacted = "\n".join(logs).replace(token, "***")
        if push.returncode != 0:
            return "failed", "", redacted
        url = f"https://github.com/{repo}/tree/{branch}"
        return "pushed", url, redacted

    def _row_to_out(self, row: BuildRunModel) -> BuildRunOut:
        return BuildRunOut(
            id=row.id,
            session_id=row.session_id,
            spec_version=row.spec_version,
            brief_id=row.brief_id,
            pathway_id=row.pathway_id,
            status=row.status,
            started_at=row.started_at,
            finished_at=row.finished_at,
            files_changed=json.loads(row.files_changed or "[]"),
            agent_summary=row.agent_summary or "",
            duration_sec=row.duration_sec or 0.0,
            cost_usd=row.cost_usd or 0.0,
            test_status=row.test_status or "pending",
            test_log=row.test_log or "",
            push_status=row.push_status or "pending",
            repo_url=row.repo_url or "",
            error=row.error or "",
        )


builder_service = BuilderService()
