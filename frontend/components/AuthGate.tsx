"use client";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { supabase } from "@/lib/supabase";

const LINKS = [
  { href: "/", label: "Live" },
  { href: "/history", label: "History" },
  { href: "/alerts", label: "Alerts" },
];

/** Renders the app only for signed-in users; otherwise redirects to /login. */
export default function AuthGate({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const path = usePathname();
  const [email, setEmail] = useState<string | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let alive = true;
    // Local development only: backend must run with AUTH_DISABLED=1 as well.
    if (process.env.NEXT_PUBLIC_AUTH_DISABLED === "1") {
      setEmail("local-dev");
      setReady(true);
      return;
    }
    supabase.auth.getSession().then(({ data }) => {
      if (!alive) return;
      if (!data.session) router.replace("/login");
      else {
        setEmail(data.session.user.email ?? "user");
        setReady(true);
      }
    });
    const { data: sub } = supabase.auth.onAuthStateChange((_e, session) => {
      if (!session) router.replace("/login");
    });
    return () => {
      alive = false;
      sub.subscription.unsubscribe();
    };
  }, [router]);

  if (!ready) return <div className="center muted">Checking session…</div>;

  return (
    <>
      <header className="nav">
        <div className="brand">
          <span className="dot" /> MONO EDGE <span className="muted">Signal Analytics</span>
        </div>
        <nav>
          {LINKS.map((l) => {
            const active = l.href === "/" ? path === "/" : path.startsWith(l.href);
            return (
              <Link key={l.href} href={l.href} className={active ? "active" : ""}>
                {l.label}
              </Link>
            );
          })}
        </nav>
        <div className="user">
          <span className="muted">{email}</span>
          <button
            className="btn ghost"
            onClick={async () => {
              await supabase.auth.signOut();
              router.replace("/login");
            }}
          >
            Sign out
          </button>
        </div>
      </header>
      <main className="page">{children}</main>
    </>
  );
}
