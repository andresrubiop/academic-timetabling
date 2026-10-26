import Link from "next/link";

// The landing page. Two doors, named, rather than a wall of prose: the
// dashboard is what a signed-in person wants, and the timetable is the thing
// the platform exists to produce.
export default function Home() {
  return (
    <div className="page">
      <h1 className="page__title">Academic AI Planning Platform</h1>
      <p className="page__lede">
        Planificación académica y generación de horarios. CP-SAT decide la
        factibilidad; las personas aprueban los cambios.
      </p>
      <div className="page__actions">
        {/* Sign-in first: the other two need a session, and sending somebody
            to a page that immediately bounces them here is a worse
            introduction than saying so. */}
        <Link className="button button--primary" href="/login">
          Iniciar sesión
        </Link>
        <Link className="button" href="/dashboard">
          Ver mi panel
        </Link>
      </div>
    </div>
  );
}
