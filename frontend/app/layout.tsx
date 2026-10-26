import "./globals.css";
import "./styles/ui.css";

import { AppShell } from "../components/AppShell";

export const metadata = {
  title: "Academic AI Planning Platform",
  description: "University academic planning and timetabling",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    // `lang` is not decoration: it decides which voice a screen reader uses,
    // and the interface is in Spanish.
    <html lang="es">
      <body>
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
