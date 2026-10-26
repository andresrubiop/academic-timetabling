// The container healthcheck target. `force-dynamic` because a statically
// rendered health endpoint returns 200 from a cache after the process has
// stopped being able to do anything -- which is precisely the state the
// check exists to detect.
export const dynamic = "force-dynamic";

export function GET() {
  return Response.json({ status: "ok", service: "frontend" });
}
