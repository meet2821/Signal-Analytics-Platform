"use client";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { supabase } from "@/lib/supabase";

export default function LoginPage() {
  const router = useRouter();
  const [mode, setMode] = useState<"in" | "up">("in");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setInfo(null);
    try {
      if (mode === "in") {
        const { error } = await supabase.auth.signInWithPassword({ email, password });
        if (error) throw error;
        router.replace("/");
      } else {
        const { data, error } = await supabase.auth.signUp({ email, password });
        if (error) throw error;
        if (data.session) router.replace("/");
        else setInfo("Account created. Check your email to confirm it, then sign in.");
      }
    } catch (err: any) {
      setError(err.message || "Something went wrong");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <div className="brand" style={{ marginBottom: 16 }}>
        <span className="dot" /> MONO EDGE <span className="muted">Signal Analytics</span>
      </div>
      <form className="panel" onSubmit={submit}>
        <h2>{mode === "in" ? "Sign in" : "Create account"}</h2>
        <label>
          Email
          <input suppressHydrationWarning type="email" required value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" />
        </label>
        <label>
          Password
          <input
            suppressHydrationWarning
            type="password"
            required
            minLength={6}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete={mode === "in" ? "current-password" : "new-password"}
          />
        </label>
        {error ? <div className="error">{error}</div> : null}
        {info ? <div className="note">{info}</div> : null}
        <button suppressHydrationWarning className="btn" disabled={busy}>
          {busy ? "Please wait…" : mode === "in" ? "Sign in" : "Sign up"}
        </button>
        <button suppressHydrationWarning type="button" className="btn ghost" onClick={() => setMode(mode === "in" ? "up" : "in")}>
          {mode === "in" ? "Need an account? Sign up" : "Have an account? Sign in"}
        </button>
      </form>
    </div>
  );
}
