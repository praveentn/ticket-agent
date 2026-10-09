import { useEffect, useState } from "react";
import { api } from "../api";
import type { Employee } from "../types";

const WHY: Record<string, string> = {
  E1001: "India · sees the India leave addendum; manager Priya approves her requests",
  E1002: "US · sees US benefits; not Finance, so internal AP docs are hidden",
  E1003: "Germany · Finance team member, can read Finance-internal documents",
  E2001: "Manager · can read the confidential compensation guide",
  E2002: "Manager (US)",
  E3001: "HR Business Partner · approves bank-detail changes",
  E3002: "IT Admin · can view any ticket",
};

export default function Login({ onLogin }: { onLogin: (token: string, e: Employee) => void }) {
  const [users, setUsers] = useState<Employee[]>([]);
  const [config, setConfig] = useState<{ mode: string; llm: string; models: string[] } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.get<{ mode: string; llm: string; models: string[] }>("/auth/config").then(setConfig).catch(() => {});
    api.get<Employee[]>("/auth/demo-users").then(setUsers).catch((e) => setError(e.message));
  }, []);

  const login = async (id: string) => {
    try {
      const r = await api.post<{ access_token: string; employee: Employee }>("/auth/dev-login", { employee_id: id });
      onLogin(r.access_token, r.employee);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <main className="login">
      <div className="login-card">
        <div className="brand big"><span className="logo" aria-hidden />Agentic Support Desk</div>
        <p className="muted">
          Local development sign-in. In production this screen is replaced by corporate SSO (OIDC). Pick an employee — each one has
          different roles, departments and countries, so retrieval permissions and approvals behave differently.
        </p>
        {config && (
          <p className="small">
            <span className="badge">{config.llm === "groq" ? `LLM: Groq (${config.models[0]})` : "LLM: offline heuristics"}</span>{" "}
            <span className="badge">auth: {config.mode}</span>
          </p>
        )}
        {error && <div className="banner error">{error}</div>}
        <div className="users">
          {users.map((u) => (
            <button key={u.id} className="user-card" onClick={() => login(u.id)}>
              <span className="avatar" aria-hidden>{u.name.split(" ").map((p) => p[0]).join("")}</span>
              <span>
                <strong>{u.name}</strong>
                <span className="small muted block">{u.title} · {u.department} · {u.country} · {u.role}</span>
                <span className="small block">{WHY[u.id]}</span>
              </span>
            </button>
          ))}
        </div>
      </div>
    </main>
  );
}
