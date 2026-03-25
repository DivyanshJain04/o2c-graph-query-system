import { useState } from "react";
import { marked } from "marked";

export function MessageCard({ message, rejectionText, highlightSql }) {
  const [showSql, setShowSql] = useState(false);

  if (message.role === "user") {
    return (
      <div className="message-row user">
        <div className="avatar user">U</div>
        <div className="message-card user">{message.content}</div>
      </div>
    );
  }

  const payload = message.payload || {};
  const isRejection = String(payload.answer || "").trim() === rejectionText;
  const results = Array.isArray(payload.results) ? payload.results : [];
  const columns = results.length ? Object.keys(results[0]) : [];

  return (
    <div className="message-row assistant">
      <div className="avatar assistant">AI</div>
      <div className={`message-card assistant ${isRejection ? "assistant-rejection" : ""}`}>
        <div className="assistant-answer-title">Answer</div>
        <div
          className="assistant-answer markdown"
          dangerouslySetInnerHTML={{ __html: marked.parse(payload.answer || message.content) }}
        />
        {payload.sql ? (
          <div className="sql-section">
            <button className="collapse-btn" onClick={() => setShowSql((v) => !v)}>
              {showSql ? "▼" : "▶"} Generated SQL
            </button>
            {showSql ? (
              <pre className="sql-pre" dangerouslySetInnerHTML={{ __html: highlightSql(payload.sql) }} />
            ) : null}
          </div>
        ) : null}
        {results.length ? (
          <div className="results-block">
            <div className="assistant-answer-title">Results ({results.length})</div>
            <div className="table-wrap">
              <table className="results-table">
                <thead>
                  <tr>{columns.map((c) => <th key={c}>{c}</th>)}</tr>
                </thead>
                <tbody>
                  {results.map((row, i) => (
                    <tr key={i}>{columns.map((c) => <td key={`${i}-${c}`}>{String(row[c] ?? "")}</td>)}</tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}

