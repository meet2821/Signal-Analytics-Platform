import "./globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Mono Edge · Signal Analytics",
  description: "Live monitoring, history and anomaly alerts for rolling-mill signals",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
