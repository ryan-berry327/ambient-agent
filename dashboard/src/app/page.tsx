"use client";

import { useCallback, useEffect, useRef, useState } from "react";

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const WS_URL = (process.env.NEXT_PUBLIC_WS_URL || "ws://localhost:8000").replace(/^http/, "ws") + "/ws";

type Device = {
  index: number;
  name: string;
  kind: "input" | "loopback";
  default_sample_rate: number;
};

type TranscriptSegment = {
  id: number;
  ts: string;
  channel: string;
  speaker: string;
  text: string;
  is_final: boolean;
};

type SpecItem = {
  uuid: string;
  requirement: string;
  status: "confirmed" | "tentative" | "retracted";
  evidence_quote: string;
  category: string;
  acceptance_hint?: string | null;
  locked_by_human: boolean;
  spec_version: number;
};

type SpecChange = {
  id: number;
  spec_version: number;
  item_uuid: string;
  action: string;
  reason: string;
  ts: string;
};

type SessionState = {
  session_id: string | null;
  status: string;
  spec_version: number;
  replay_mode: boolean;
  deepgram_minutes: number;
  haiku_input_tokens: number;
  haiku_output_tokens: number;
  estimated_cost_usd: number;
};

type Pathway = {
  id: string;
  title: string;
  summary: string;
  effort: string;
  tradeoffs: string;
  approach: string;
};

type Brief = {
  id: string;
  session_id: string;
  spec_version: number;
  goal: string;
  summary: string;
  actionable_items: Array<Record<string, unknown>>;
  deferred_items: Array<Record<string, unknown>>;
  viability: { status: "green" | "amber" | "red"; summary: string; constraints: string[] };
  pathways: Pathway[];
  recommended_pathway_id: string;
  selected_pathway_id?: string | null;
  created_at: string;
};

type BuildRun = {
  id: number;
  session_id: string;
  spec_version: number;
  pathway_id?: string | null;
  status: string;
  agent_summary: string;
  test_status: string;
  test_log: string;
  push_status: string;
  repo_url: string;
  error: string;
  files_changed: string[];
  duration_sec: number;
};

export default function DashboardPage() {
  const [devices, setDevices] = useState<Device[]>([]);
  const [micIndex, setMicIndex] = useState<number | "">("");
  const [systemIndex, setSystemIndex] = useState<number | "">("");
  const [session, setSession] = useState<SessionState | null>(null);
  const [transcript, setTranscript] = useState<TranscriptSegment[]>([]);
  const [interim, setInterim] = useState<Record<string, TranscriptSegment>>({});
  const [specItems, setSpecItems] = useState<SpecItem[]>([]);
  const [specVersion, setSpecVersion] = useState(0);
  const [specChanges, setSpecChanges] = useState<SpecChange[]>([]);
  const [rightTab, setRightTab] = useState<"spec" | "journey" | "brief" | "build">("spec");
  const [distilling, setDistilling] = useState(false);
  const [briefing, setBriefing] = useState(false);
  const [building, setBuilding] = useState(false);
  const [brief, setBrief] = useState<Brief | null>(null);
  const [buildRun, setBuildRun] = useState<BuildRun | null>(null);
  const [connected, setConnected] = useState(false);
  const transcriptRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WebSocket | null>(null);

  const scrollTranscript = useCallback(() => {
    const el = transcriptRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, []);

  useEffect(() => {
    scrollTranscript();
  }, [transcript, interim, scrollTranscript]);

  const fetchDevices = useCallback(async () => {
    try {
      const res = await fetch(`${API}/devices`);
      const data: Device[] = await res.json();
      setDevices(data);
      const inputs = data.filter((d) => d.kind === "input");
      const loopbacks = data.filter((d) => d.kind === "loopback");
      if (inputs.length) setMicIndex(inputs[0].index);
      if (loopbacks.length) setSystemIndex(loopbacks[0].index);
    } catch {
      /* backend may be offline during dev */
    }
  }, []);

  const fetchSpecChanges = useCallback(async () => {
    try {
      const res = await fetch(`${API}/spec/changes`);
      const data: SpecChange[] = await res.json();
      setSpecChanges(data);
    } catch {
      /* ignore */
    }
  }, []);

  const fetchTranscript = useCallback(async () => {
    try {
      const res = await fetch(`${API}/transcript`);
      const data = await res.json();
      const segments: TranscriptSegment[] = data.segments ?? [];
      setTranscript(segments);
    } catch {
      /* ignore */
    }
  }, []);

  const fetchSpec = useCallback(async () => {
    try {
      const res = await fetch(`${API}/spec`);
      const data = await res.json();
      setSpecItems(data.items ?? []);
      if (data.version != null) setSpecVersion(data.version);
    } catch {
      /* ignore */
    }
  }, []);

  const fetchBrief = useCallback(async () => {
    try {
      const res = await fetch(`${API}/brief`);
      if (!res.ok) return;
      const data = await res.json();
      setBrief(data);
    } catch {
      /* ignore */
    }
  }, []);

  const fetchBuild = useCallback(async () => {
    try {
      const res = await fetch(`${API}/build/latest`);
      if (!res.ok) return;
      const data = await res.json();
      setBuildRun(data);
    } catch {
      /* ignore */
    }
  }, []);

  const connectWs = useCallback(() => {
    const existing = wsRef.current;
    if (
      existing?.readyState === WebSocket.OPEN ||
      existing?.readyState === WebSocket.CONNECTING
    ) {
      return;
    }
    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;

    ws.onopen = () => setConnected(true);
    ws.onerror = () => ws.close();
    ws.onclose = () => {
      setConnected(false);
      wsRef.current = null;
      setTimeout(connectWs, 2000);
    };

    ws.onmessage = (evt) => {
      const msg = JSON.parse(evt.data);
      const { type, payload } = msg;

      if (type === "session.state") {
        setSession(payload);
      } else if (type === "transcript.append") {
        const seg: TranscriptSegment = {
          id: payload.id,
          ts: payload.ts,
          channel: payload.channel,
          speaker: payload.speaker,
          text: payload.text,
          is_final: payload.is_final,
        };
        if (seg.is_final) {
          setInterim((prev) => {
            const next = { ...prev };
            delete next[seg.channel];
            return next;
          });
          setTranscript((prev) => {
            if (prev.some((p) => p.id === seg.id && seg.id > 0)) return prev;
            return [...prev, seg];
          });
        } else {
          setInterim((prev) => ({ ...prev, [seg.channel]: seg }));
        }
      } else if (type === "spec.updated") {
        if (payload.items) setSpecItems(payload.items);
        if (payload.version != null) setSpecVersion(payload.version);
        fetchSpecChanges();
      } else if (type === "distill.started") {
        setDistilling(true);
      } else if (type === "distill.finished") {
        setDistilling(false);
      } else if (type === "brief.started") {
        setBriefing(true);
      } else if (type === "brief.finished") {
        setBriefing(false);
      } else if (type === "brief.updated") {
        setBrief(payload);
        setBriefing(false);
        setRightTab("brief");
      } else if (type === "build.started") {
        setBuilding(true);
        setRightTab("build");
      } else if (type === "build.updated") {
        setBuildRun(payload);
        setBuilding(
          ["queued", "running", "testing", "pushing"].includes(String(payload?.status || ""))
        );
      }
    };
  }, [fetchSpecChanges]);

  useEffect(() => {
    fetchDevices();
    fetch(`${API}/session/state`)
      .then((r) => r.json())
      .then(setSession)
      .catch(() => {});
    fetchBrief();
    fetchBuild();
    connectWs();
    return () => wsRef.current?.close();
  }, [fetchDevices, connectWs, fetchBrief, fetchBuild]);

  useEffect(() => {
    if (session?.status !== "running") {
      if (session?.status === "idle") {
        setTranscript([]);
        setInterim({});
      }
      return;
    }
    fetchTranscript();
    fetchSpec();
    fetchBrief();
    const id = setInterval(() => {
      fetchTranscript();
      fetchSpec();
      fetchBrief();
    }, 2000);
    return () => clearInterval(id);
  }, [session?.status, session?.session_id, fetchTranscript, fetchSpec, fetchBrief]);

  const startSession = async () => {
    const body: Record<string, unknown> = {};
    if (micIndex !== "") body.mic_device_index = micIndex;
    if (systemIndex !== "") body.system_device_index = systemIndex;
    const res = await fetch(`${API}/session/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      alert(await res.text());
      return;
    }
    setTranscript([]);
    setInterim({});
    setSpecItems([]);
    setSpecChanges([]);
    setSpecVersion(0);
    setBrief(null);
    setBuildRun(null);
    const state = await res.json();
    setSession(state);
    fetchTranscript();
    fetchSpec();
  };

  const stopSession = async () => {
    const res = await fetch(`${API}/session/stop`, { method: "POST" });
    const state = await res.json();
    setSession(state);
  };

  const overrideSpec = async (uuid: string, status: string, unlock = false) => {
    await fetch(`${API}/spec/${uuid}/override`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(unlock ? { status: "confirmed", unlock: true } : { status }),
    });
  };

  const generateBrief = async () => {
    setBriefing(true);
    const res = await fetch(`${API}/brief/generate`, { method: "POST" });
    setBriefing(false);
    if (!res.ok) {
      alert(await res.text());
      return;
    }
    const data = await res.json();
    setBrief(data);
    setRightTab("brief");
  };

  const selectPathway = async (pathwayId: string) => {
    const res = await fetch(`${API}/brief/select-pathway`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pathway_id: pathwayId }),
    });
    if (!res.ok) {
      alert(await res.text());
      return;
    }
    setBrief(await res.json());
  };

  const startBuild = async () => {
    setBuilding(true);
    setRightTab("build");
    const res = await fetch(`${API}/build/start`, { method: "POST" });
    if (!res.ok) {
      setBuilding(false);
      alert(await res.text());
      return;
    }
    const data = await res.json();
    setBuildRun(data);
    setBuilding(["queued", "running", "testing", "pushing"].includes(data.status));
  };

  const isRunning = session?.status === "running";
  const inputDevices = devices.filter((d) => d.kind === "input");
  const loopbackDevices = devices.filter((d) => d.kind === "loopback");
  const canBuild =
    !!brief &&
    !!brief.selected_pathway_id &&
    brief.viability.status !== "red" &&
    !building &&
    !briefing;

  const interimLines = Object.values(interim);

  return (
    <div className="app">
      <header className="topbar">
        <h1>Ambient Call Agent</h1>
        <span className={`status-dot ${isRunning ? "running" : "idle"}`} />
        <span style={{ fontSize: "0.8rem", color: "#9aa0a6" }}>
          {session?.status ?? "idle"}
          {connected ? "" : " (ws reconnecting…)"}
        </span>

        {!isRunning && (
          <div className="device-select">
            <label>
              Mic
              <select
                value={micIndex}
                onChange={(e) => setMicIndex(Number(e.target.value))}
                disabled={session?.replay_mode ?? true}
              >
                {inputDevices.map((d) => (
                  <option key={d.index} value={d.index}>
                    {d.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              System
              <select
                value={systemIndex}
                onChange={(e) => setSystemIndex(Number(e.target.value))}
                disabled={session?.replay_mode ?? true}
              >
                {loopbackDevices.map((d) => (
                  <option key={d.index} value={d.index}>
                    {d.name}
                  </option>
                ))}
              </select>
            </label>
          </div>
        )}

        {session?.replay_mode && <span className="badge replay">REPLAY MODE</span>}
        {distilling && <span className="badge">Distilling…</span>}
        {briefing && <span className="badge">Briefing…</span>}
        {building && <span className="badge">Building…</span>}

        <div className="spacer" />

        <div className="cost-meter">
          Audio: {(session?.deepgram_minutes ?? 0).toFixed(2)} min · Cursor:{" "}
          {(session?.haiku_input_tokens ?? 0).toLocaleString()} in /{" "}
          {(session?.haiku_output_tokens ?? 0).toLocaleString()} out · ~$
          {(session?.estimated_cost_usd ?? 0).toFixed(4)}
        </div>

        <button
          className="btn-secondary"
          onClick={generateBrief}
          disabled={!session?.session_id || briefing || specItems.length === 0}
          title="Summarize spec into brief + viability + pathways"
        >
          Generate brief
        </button>

        <button
          className={`btn-build ${canBuild ? "ready" : ""}`}
          onClick={startBuild}
          disabled={!canBuild}
          title={
            !brief
              ? "Generate a brief first"
              : brief.viability.status === "red"
                ? "Viability is red — refine requirements"
                : "Scaffold, smoke-test dummy data, and push if configured"
          }
        >
          Build now
        </button>

        {isRunning ? (
          <button className="btn-danger" onClick={stopSession}>
            Stop session
          </button>
        ) : (
          <button className="btn-primary" onClick={startSession}>
            Start session
          </button>
        )}
      </header>

      <div className="main">
        <section className="panel">
          <div className="panel-header">Live transcript</div>
          <div className="panel-body" ref={transcriptRef}>
            {transcript.length === 0 && interimLines.length === 0 && (
              <div className="empty-state">Transcript will appear here during a session.</div>
            )}
            {transcript.map((seg) => (
              <div key={`f-${seg.id}-${seg.ts}`} className="transcript-line">
                <div className="transcript-meta">
                  <span className={seg.channel === "mic" ? "channel-mic" : "channel-system"}>
                    {seg.channel}
                  </span>{" "}
                  · {seg.speaker} · {new Date(seg.ts).toLocaleTimeString()}
                </div>
                {seg.text}
              </div>
            ))}
            {interimLines.map((seg) => (
              <div key={`i-${seg.channel}`} className="transcript-line interim">
                <div className="transcript-meta">
                  <span className={seg.channel === "mic" ? "channel-mic" : "channel-system"}>
                    {seg.channel}
                  </span>{" "}
                  · {seg.speaker}
                </div>
                {seg.text}
              </div>
            ))}
          </div>
        </section>

        <section className="panel">
          <div className="panel-header">
            <div className="tabs">
              <button
                className={`tab ${rightTab === "spec" ? "active" : ""}`}
                onClick={() => setRightTab("spec")}
              >
                Spec v{specVersion}
              </button>
              <button
                className={`tab ${rightTab === "journey" ? "active" : ""}`}
                onClick={() => setRightTab("journey")}
              >
                Journey
              </button>
              <button
                className={`tab ${rightTab === "brief" ? "active" : ""}`}
                onClick={() => setRightTab("brief")}
              >
                Brief
              </button>
              <button
                className={`tab ${rightTab === "build" ? "active" : ""}`}
                onClick={() => setRightTab("build")}
              >
                Build
              </button>
            </div>
          </div>
          <div className="panel-body">
            {rightTab === "spec" &&
              (specItems.length === 0 ? (
                <div className="empty-state">
                  Requirements will be distilled from the transcript automatically.
                </div>
              ) : (
                specItems.map((item) => (
                  <div key={item.uuid} className={`spec-card ${item.status}`}>
                    <div className="requirement">{item.requirement}</div>
                    {item.evidence_quote && (
                      <div className="evidence">&ldquo;{item.evidence_quote}&rdquo;</div>
                    )}
                    <div className="meta">
                      {item.category} · {item.status}
                      {item.locked_by_human && " · locked"}
                    </div>
                    <div className="spec-actions">
                      <button onClick={() => overrideSpec(item.uuid, "confirmed")}>Confirm</button>
                      <button onClick={() => overrideSpec(item.uuid, "retracted")}>Retract</button>
                      {item.locked_by_human && (
                        <button
                          className="lock-indicator"
                          onClick={() => overrideSpec(item.uuid, item.status, true)}
                        >
                          Unlock
                        </button>
                      )}
                    </div>
                  </div>
                ))
              ))}

            {rightTab === "journey" &&
              (specChanges.length === 0 ? (
                <div className="empty-state">Spec changes will appear after the first distill run.</div>
              ) : (
                <table className="journey-table">
                  <thead>
                    <tr>
                      <th>Version</th>
                      <th>Action</th>
                      <th>Item</th>
                      <th>Reason</th>
                      <th>Time</th>
                    </tr>
                  </thead>
                  <tbody>
                    {specChanges.map((c) => (
                      <tr key={c.id}>
                        <td>v{c.spec_version}</td>
                        <td className={`action-${c.action}`}>{c.action}</td>
                        <td title={c.item_uuid}>{c.item_uuid.slice(0, 8)}…</td>
                        <td>{c.reason}</td>
                        <td>{new Date(c.ts).toLocaleTimeString()}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ))}

            {rightTab === "brief" &&
              (!brief ? (
                <div className="empty-state">
                  After distillation, generate a brief for viability, constraints, and pathways.
                </div>
              ) : (
                <div className="brief-panel">
                  <div className="brief-goal">{brief.goal}</div>
                  <p className="brief-summary">{brief.summary}</p>
                  <div className={`viability-badge ${brief.viability.status}`}>
                    Viability: {brief.viability.status}
                  </div>
                  <p className="brief-summary">{brief.viability.summary}</p>
                  {brief.viability.constraints?.length > 0 && (
                    <ul className="constraint-list">
                      {brief.viability.constraints.map((c) => (
                        <li key={c}>{c}</li>
                      ))}
                    </ul>
                  )}
                  <div className="meta" style={{ marginBottom: "0.75rem" }}>
                    {brief.actionable_items.length} actionable · {brief.deferred_items.length}{" "}
                    deferred · spec v{brief.spec_version}
                  </div>
                  <div className="pathway-list">
                    {brief.pathways.map((p) => {
                      const selected = brief.selected_pathway_id === p.id;
                      const recommended = brief.recommended_pathway_id === p.id;
                      return (
                        <button
                          key={p.id}
                          type="button"
                          className={`pathway-card ${selected ? "selected" : ""}`}
                          onClick={() => selectPathway(p.id)}
                        >
                          <div className="pathway-title">
                            {p.title}
                            {recommended && <span className="badge">recommended</span>}
                            <span className="effort">{p.effort}</span>
                          </div>
                          <div className="pathway-summary">{p.summary}</div>
                          <div className="pathway-meta">{p.tradeoffs}</div>
                          <div className="pathway-meta">{p.approach}</div>
                        </button>
                      );
                    })}
                  </div>
                </div>
              ))}

            {rightTab === "build" &&
              (!buildRun ? (
                <div className="empty-state">
                  Select a pathway in Brief, then click Build now to scaffold, smoke-test, and push.
                </div>
              ) : (
                <div className="build-panel">
                  <div className="requirement">
                    Build #{buildRun.id} · {buildRun.status}
                  </div>
                  <div className="meta">
                    pathway {buildRun.pathway_id || "—"} · smoke {buildRun.test_status} · push{" "}
                    {buildRun.push_status}
                    {buildRun.duration_sec ? ` · ${buildRun.duration_sec.toFixed(1)}s` : ""}
                  </div>
                  {buildRun.agent_summary && <p className="brief-summary">{buildRun.agent_summary}</p>}
                  {buildRun.repo_url && (
                    <p className="brief-summary">
                      Output: <code>{buildRun.repo_url}</code>
                    </p>
                  )}
                  {buildRun.error && <p className="build-error">{buildRun.error}</p>}
                  {buildRun.files_changed?.length > 0 && (
                    <ul className="constraint-list">
                      {buildRun.files_changed.map((f) => (
                        <li key={f}>{f}</li>
                      ))}
                    </ul>
                  )}
                  {buildRun.test_log && <pre className="build-log">{buildRun.test_log}</pre>}
                </div>
              ))}
          </div>
        </section>
      </div>
    </div>
  );
}
