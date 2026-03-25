import { useEffect, useMemo, useRef, useState } from "react";
import cytoscape from "cytoscape";
import { MessageCard } from "./components/MessageCard";

const ENV_API_BASE = import.meta.env.VITE_API_BASE_URL;
const API_BASE = ENV_API_BASE || (import.meta.env.DEV ? "http://localhost:8000" : "");
const REJECTION_TEXT = "This system is designed to answer questions related to the provided dataset only.";
const EXAMPLE_QUERIES = [
  { icon: "📦", text: "How many sales orders are in the dataset?" },
  { icon: "💰", text: "Show top 10 customers by total net amount billed." },
  { icon: "🚚", text: "List deliveries and their related billing documents." },
  { icon: "🧾", text: "Show billing documents by sold-to party." },
  { icon: "🏭", text: "Which products appear most frequently in sales order items?" },
];
const ENTITY_COLORS = {
  sales_order_headers: "#60a5fa",
  sales_order_items: "#818cf8",
  outbound_delivery_headers: "#34d399",
  outbound_delivery_items: "#10b981",
  billing_document_headers: "#f59e0b",
  billing_document_items: "#f97316",
  journal_entry_items_accounts_receivable: "#22d3ee",
  payments_accounts_receivable: "#14b8a6",
  business_partners: "#f472b6",
  products: "#a3e635",
};

function toHistory(messages) {
  return messages.map((m) => ({ role: m.role, content: m.content }));
}
function formatType(v) {
  return String(v || "").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}
function formatKey(v) {
  return String(v || "")
    .replace(/([a-z])([A-Z])/g, "$1 $2")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}
function formatDate(value) {
  if (typeof value !== "string" || !value.endsWith("T00:00:00.000Z")) return value;
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
}

function pad2(v) {
  const n = String(v ?? "").trim();
  if (!n) return "00";
  return n.padStart(2, "0");
}

function normalizeProperties(properties) {
  const input = properties || {};
  const out = [];
  const consumed = new Set();
  const keys = Object.keys(input);

  keys.forEach((key) => {
    if (consumed.has(key)) return;
    const match = key.match(/^(.*)_(hours|minutes|seconds)$/);
    if (!match) return;
    const base = match[1];
    const hKey = `${base}_hours`;
    const mKey = `${base}_minutes`;
    const sKey = `${base}_seconds`;
    const hasAny = hKey in input || mKey in input || sKey in input;
    if (!hasAny) return;
    consumed.add(hKey);
    consumed.add(mKey);
    consumed.add(sKey);
    const baseLabel = formatKey(base);
    const label = baseLabel.endsWith("Time") ? baseLabel : `${baseLabel} Time`;
    out.push([label, `${pad2(input[hKey])}:${pad2(input[mKey])}:${pad2(input[sKey])}`]);
  });

  keys.forEach((key) => {
    if (consumed.has(key)) return;
    const value = input[key];
    if (value === null || value === "" || value === undefined) return;
    out.push([formatKey(key), String(formatDate(value))]);
  });
  return out;
}
function highlightSql(sql) {
  const escaped = String(sql || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const keywords =
    /\b(SELECT|FROM|WHERE|JOIN|LEFT JOIN|RIGHT JOIN|INNER JOIN|OUTER JOIN|ON|GROUP BY|ORDER BY|LIMIT|WITH|AS|AND|OR|COUNT|SUM|AVG|MIN|MAX|DISTINCT|HAVING|CASE|WHEN|THEN|ELSE|END|DESC|ASC)\b/gi;
  return escaped.replace(keywords, '<span class="sql-keyword">$1</span>');
}

async function readResponseBodySafe(res) {
  const ct = res.headers.get("content-type") || "";
  try {
    if (ct.includes("application/json")) return await res.json();
    const text = await res.text();
    // best-effort parse
    return JSON.parse(text);
  } catch {
    try {
      return { error: await res.text() };
    } catch {
      return { error: "Failed to read response body." };
    }
  }
}

function normalizeChatPayload(data) {
  const answer = typeof data?.answer === "string" ? data.answer : "";
  const sql = typeof data?.sql === "string" ? data.sql : data?.sql === null ? null : null;
  const results = Array.isArray(data?.results) ? data.results : [];
  const error = typeof data?.error === "string" ? data.error : "";
  const requestId = typeof data?.request_id === "string" ? data.request_id : "";
  return { answer: answer || (error ? "An error occurred while processing your request." : "No answer returned."), sql, results, error, request_id: requestId };
}

function isColdStartLike(status) {
  return status === 502 || status === 503 || status === 504;
}
function buildElements(graphData) {
  const degree = {};
  (graphData.edges || []).forEach((e) => {
    degree[e.source] = (degree[e.source] || 0) + 1;
    degree[e.target] = (degree[e.target] || 0) + 1;
  });
  const nodes = (graphData.nodes || []).map((n) => ({
    data: {
      id: n.id,
      type: n.type,
      color: ENTITY_COLORS[n.type] || "#6366f1",
      primaryId: n.label || n.id,
      size: Math.min(32, 18 + (degree[n.id] || 0) * 2),
      raw: n,
    },
  }));
  const edges = (graphData.edges || []).map((e, i) => ({
    data: { id: `${e.source}-${e.target}-${i}`, source: e.source, target: e.target, label: e.relationship },
  }));
  return [...nodes, ...edges];
}

function collectHighlightNodeIds(graphNodes, payload) {
  const answerText = String(payload?.answer || "");
  const results = Array.isArray(payload?.results) ? payload.results : [];
  const tokens = new Set();

  // Candidate tokens from answer text.
  const tokenMatches = answerText.match(/[A-Za-z0-9_.\-]{3,}/g) || [];
  tokenMatches.forEach((t) => tokens.add(String(t).toLowerCase()));

  // Candidate tokens from SQL result cells.
  results.forEach((row) => {
    Object.values(row || {}).forEach((value) => {
      if (value === null || value === undefined || value === "") return;
      const text = String(value).trim();
      if (!text) return;
      tokens.add(text.toLowerCase());
      const parts = text.match(/[A-Za-z0-9_.\-]{3,}/g) || [];
      parts.forEach((p) => tokens.add(String(p).toLowerCase()));
    });
  });

  const matched = [];
  (graphNodes || []).forEach((n) => {
    const idText = String(n.id || "").toLowerCase();
    const labelText = String(n.label || "").toLowerCase();
    const primaryText = String(n.properties?.salesOrder || n.properties?.billingDocument || n.properties?.deliveryDocument || "").toLowerCase();
    for (const token of tokens) {
      if (idText.includes(token) || labelText.includes(token) || (primaryText && primaryText === token)) {
        matched.push(n.id);
        break;
      }
    }
  });
  return Array.from(new Set(matched));
}

export default function App() {
  const cyRef = useRef(null);
  const cyInstanceRef = useRef(null);
  const chatBottomRef = useRef(null);
  const [graphData, setGraphData] = useState({ nodes: [], edges: [] });
  const [graphLoading, setGraphLoading] = useState(true);
  const [graphError, setGraphError] = useState("");
  const [selectedNode, setSelectedNode] = useState(null);
  const [overlayOpen, setOverlayOpen] = useState(false);
  const [hoverInfo, setHoverInfo] = useState(null);
  const [messages, setMessages] = useState([{ role: "assistant", content: "Ask a question about SAP Order-to-Cash data.", payload: { answer: "Ask a question about SAP Order-to-Cash data." } }]);
  const [input, setInput] = useState("");
  const [chatLoading, setChatLoading] = useState(false);
  const [chatError, setChatError] = useState("");
  const [envError, setEnvError] = useState("");
  const [highlightedNodeIds, setHighlightedNodeIds] = useState([]);
  const cyElements = useMemo(() => buildElements(graphData), [graphData]);

  useEffect(() => {
    if (!API_BASE) {
      setEnvError(
        "Missing VITE_API_BASE_URL. Set it in your frontend environment (Render/production) so the UI knows where to call the backend API."
      );
    } else {
      setEnvError("");
    }
  }, []);

  useEffect(() => {
    (async () => {
      setGraphLoading(true);
      setGraphError("");
      try {
        if (!API_BASE) throw new Error("API base URL is not configured.");
        const res = await fetch(`${API_BASE}/api/graph`);
        const data = await readResponseBodySafe(res);
        console.debug("[graph] response", { ok: res.ok, status: res.status, data });
        if (!res.ok || data.error) throw new Error(data.error || `Failed to load graph (HTTP ${res.status}).`);
        setGraphData({ nodes: data.nodes || [], edges: data.edges || [] });
      } catch (err) {
        setGraphError(err.message || "Failed to load graph.");
      } finally {
        setGraphLoading(false);
      }
    })();
  }, []);

  useEffect(() => {
    if (!cyRef.current) return;
    if (cyInstanceRef.current) cyInstanceRef.current.destroy();
    const cy = cytoscape({
      container: cyRef.current,
      elements: cyElements,
      style: [
        { selector: "node", style: { "background-color": "data(color)", width: "data(size)", height: "data(size)", "border-width": 1, "border-color": "#e5e7eb" } },
        { selector: "edge", style: { "line-color": "#475569", width: 1.5, "target-arrow-shape": "triangle", "target-arrow-color": "#475569", "curve-style": "bezier", opacity: 0.6 } },
        { selector: ":selected", style: { "border-color": "#22d3ee", "border-width": 3 } },
        {
          selector: ".chat-highlight",
          style: {
            "border-color": "#facc15",
            "border-width": 4,
            "overlay-color": "#fde047",
            "overlay-opacity": 0.25,
            "shadow-color": "#facc15",
            "shadow-blur": 14,
            "shadow-opacity": 1,
          },
        },
      ],
      layout: { name: "cose", animate: true, animationDuration: 500, nodeRepulsion: 130000, idealEdgeLength: 110, fit: true, padding: 30 },
      minZoom: 0.2,
      maxZoom: 2.2,
    });
    cy.on("tap", "node", async (evt) => {
      const node = evt.target;
      try {
        if (!API_BASE) throw new Error("API base URL is not configured.");
        const res = await fetch(`${API_BASE}/api/graph/node/${encodeURIComponent(node.data("type"))}/${encodeURIComponent(node.id())}`);
        const data = await readResponseBodySafe(res);
        console.debug("[node] response", { ok: res.ok, status: res.status, data });
        if (!res.ok || data.error) throw new Error(data.error || `Failed to fetch node details (HTTP ${res.status}).`);
        setSelectedNode(data);
        setOverlayOpen(true);
      } catch (err) {
        setSelectedNode({ node: null, connected_nodes: [], connected_edges: [], error: err.message || "Failed to fetch node details." });
        setOverlayOpen(true);
      }
    });
    cy.on("mouseover", "node", (evt) => {
      const n = evt.target;
      const p = n.renderedPosition();
      setHoverInfo({ x: p.x, y: p.y, type: n.data("type"), primaryId: n.data("primaryId") });
    });
    cy.on("mouseout", "node", () => setHoverInfo(null));
    cyInstanceRef.current = cy;
    return () => cy.destroy();
  }, [cyElements]);

  useEffect(() => {
    const cy = cyInstanceRef.current;
    if (!cy) return;
    cy.nodes().removeClass("chat-highlight");
    highlightedNodeIds.forEach((id) => {
      const n = cy.getElementById(id);
      if (n && n.length) {
        n.addClass("chat-highlight");
      }
    });
  }, [highlightedNodeIds, cyElements]);

  useEffect(() => {
    chatBottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, chatLoading]);

  async function sendMessage(textOverride) {
    const text = (textOverride ?? input).trim();
    if (!text || chatLoading) return;
    const userMsg = { role: "user", content: text };
    setMessages((prev) => [...prev, userMsg]);
    setInput("");
    setChatLoading(true);
    setChatError("");
    setHighlightedNodeIds([]);
    try {
      if (!API_BASE) throw new Error("API base URL is not configured (VITE_API_BASE_URL).");

      const body = JSON.stringify({ message: text, history: toHistory(messages) });
      const doRequest = async () => {
        const res = await fetch(`${API_BASE}/api/chat`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body,
        });
        const raw = await readResponseBodySafe(res);
        return { res, raw };
      };

      let { res, raw } = await doRequest();
      console.debug("[chat] response", { ok: res.ok, status: res.status, raw });

      // Cold start handling: retry once on transient gateway/service errors.
      if (!res.ok && isColdStartLike(res.status)) {
        setMessages((prev) => [
          ...prev,
          {
            role: "assistant",
            content: "Backend is waking up… retrying once.",
            payload: { answer: "Backend is waking up… retrying once.", sql: null, results: [] },
          },
        ]);
        await new Promise((r) => setTimeout(r, 1500));
        ({ res, raw } = await doRequest());
        console.debug("[chat] retry response", { ok: res.ok, status: res.status, raw });
      }

      const payload = normalizeChatPayload(raw);
      if (!res.ok) {
        const errMsg = payload.error || `Chat request failed (HTTP ${res.status}).`;
        const reqInfo = payload.request_id ? ` (request_id=${payload.request_id})` : "";
        throw new Error(`${errMsg}${reqInfo}`);
      }

      setMessages((prev) => [...prev, { role: "assistant", content: payload.answer, payload }]);
      setHighlightedNodeIds(collectHighlightNodeIds(graphData.nodes, payload));
    } catch (err) {
      const msg = err.message || "Chat request failed.";
      setChatError(msg);
      setMessages((prev) => [...prev, { role: "assistant", content: `Request failed: ${msg}`, payload: { answer: `Request failed: ${msg}`, sql: null, results: [] } }]);
    } finally {
      setChatLoading(false);
    }
  }

  return (
    <div className="app-shell fade-in">
      <header className="topbar">
        <h1>SAP O2C Graph Query Studio</h1>
        <p>Graph-based exploration and conversational analytics over Order-to-Cash data</p>
      </header>
      <main className="content-grid">
        {envError ? <div className="state error" style={{ margin: "0 20px" }}>{envError}</div> : null}
        <section className="panel graph-panel">
          <div className="panel-header">
            <h2>Knowledge Graph</h2>
            <div className="header-badges">
              <span className="badge">{graphData.nodes.length} nodes</span>
              <span className="badge">{graphData.edges.length} edges</span>
              <span className="badge highlight-badge"><span className="highlight-dot" /> Chat highlight</span>
            </div>
          </div>
          <div className="graph-main">
            {graphLoading ? <div className="state">Loading graph...</div> : null}
            {graphError ? <div className="state error">{graphError}</div> : null}
            {!graphLoading && !graphError ? <div ref={cyRef} className="cy-container" /> : null}
            {hoverInfo ? (
              <div className="node-tooltip" style={{ left: hoverInfo.x + 12, top: hoverInfo.y + 12 }}>
                <div>{formatType(hoverInfo.type)}</div>
                <div className="tooltip-sub">{hoverInfo.primaryId}</div>
              </div>
            ) : null}
            <div className={`overlay-backdrop ${overlayOpen ? "show" : ""}`} onClick={() => setOverlayOpen(false)} />
            <aside className={`node-overlay ${overlayOpen ? "show" : ""}`}>
              <div className="overlay-header">
                <h3>{selectedNode?.node ? formatType(selectedNode.node.type) : "Node Details"}</h3>
                <button className="close-btn" onClick={() => setOverlayOpen(false)}>X</button>
              </div>
              <div className="overlay-body">
                {selectedNode?.error ? <div className="state error">{selectedNode.error}</div> : null}
                {selectedNode?.node ? (
                  <div className="inspector-content">
                    <div className="inspector-block">
                      <div className="block-title">Properties</div>
                      <div className="properties-table-wrap">
                        <table className="properties-table"><tbody>
                          {normalizeProperties(selectedNode.node.properties).map(([k, v]) => (
                            <tr key={k}><th>{k}</th><td>{v}</td></tr>
                          ))}
                        </tbody></table>
                      </div>
                    </div>
                    <div className="inspector-block">
                      <div className="block-title">Connected Nodes</div>
                      <div className="chips-wrap">
                        {(selectedNode.connected_nodes || []).map((cn) => (
                          <span key={cn.id} className="node-chip" style={{ borderColor: ENTITY_COLORS[cn.type] || "#71717a", color: ENTITY_COLORS[cn.type] || "#d4d4d8" }}>
                            {formatType(cn.type)}: {cn.label || cn.id}
                          </span>
                        ))}
                      </div>
                    </div>
                    <div className="inspector-block">
                      <div className="block-title">Connected Edges</div>
                      <div className="edge-lines">
                        {Object.values(
                          (selectedNode.connected_edges || []).reduce((acc, e) => {
                            const sType = formatType((e.source || "").split("|")[0]);
                            const tType = formatType((e.target || "").split("|")[0]);
                            const rel = e.relationship || "related_to";
                            const key = `${sType}|${rel}|${tType}`;
                            if (!acc[key]) {
                              acc[key] = { sType, rel, tType, count: 0 };
                            }
                            acc[key].count += 1;
                            return acc;
                          }, {})
                        ).map((line, idx) => (
                          <div key={`${line.sType}-${line.rel}-${line.tType}-${idx}`} className="edge-line">
                            {line.sType} {" -> "} {line.rel} {" -> "} {line.tType} ({line.count} connections)
                          </div>
                        ))}
                      </div>
                    </div>
                  </div>
                ) : null}
              </div>
            </aside>
          </div>
        </section>

        <section className="panel chat-panel">
          <div className="panel-header"><h2>Data Chat</h2></div>
          <div className="chip-row">
            {EXAMPLE_QUERIES.map((q) => (
              <button key={q.text} className="chip" onClick={() => sendMessage(q.text)} disabled={chatLoading} title={q.text}>
                <span>{q.icon}</span> {q.text}
              </button>
            ))}
          </div>
          <div className="chat-stream">
            {messages.map((m, idx) => (
              <MessageCard
                key={idx}
                message={m}
                rejectionText={REJECTION_TEXT}
                highlightSql={highlightSql}
              />
            ))}
            {chatLoading ? <div className="typing-indicator"><span /><span /><span /></div> : null}
            {chatError ? <div className="state error">{chatError}</div> : null}
            <div ref={chatBottomRef} />
          </div>
          <div className="chat-input-wrap">
            <textarea value={input} onChange={(e) => setInput(e.target.value)} onKeyDown={(e) => e.key === "Enter" && !e.shiftKey ? (e.preventDefault(), sendMessage()) : null} placeholder="Ask about orders, deliveries, invoices, journal entries, payments, customers, products..." rows={3} disabled={chatLoading} />
            <button className="send-btn" onClick={() => sendMessage()} disabled={chatLoading || !input.trim()}>{chatLoading ? "..." : "➤"}</button>
          </div>
          <div className="disclaimer">Answers are generated from your SAP O2C dataset only</div>
        </section>
      </main>
    </div>
  );
}
