"use client";
import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { supabase } from "@/lib/supabase";

/** Visit /logout to sign out, even if the header button is not visible. */
export default function LogoutPage() {
  const router = useRouter();
  useEffect(() => {
    supabase.auth.signOut().finally(() => router.replace("/login"));
  }, [router]);
  return <div className="center muted">Signing out…</div>;
}
