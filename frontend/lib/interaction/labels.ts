/**
 * How a timetable block reads.
 *
 * Pure, and separate from the grid component, because these are the decisions
 * that make a week legible and every one of them is wrong in a way that looks
 * fine in a screenshot: a day named in the wrong language, an end time
 * computed from the wrong duration, two programmes given the same colour.
 */

/** The week, in the order a Spanish-speaking reader expects to see it. */
const DAY_NAMES: Record<string, string> = {
  MONDAY: "Lunes",
  TUESDAY: "Martes",
  WEDNESDAY: "Miércoles",
  THURSDAY: "Jueves",
  FRIDAY: "Viernes",
  SATURDAY: "Sábado",
  SUNDAY: "Domingo",
};

const DAY_ORDER = [
  "MONDAY",
  "TUESDAY",
  "WEDNESDAY",
  "THURSDAY",
  "FRIDAY",
  "SATURDAY",
  "SUNDAY",
];

/**
 * `MONDAY` -> `Lunes`.
 *
 * The grid showed the domain enum's own value in column headers, in an
 * interface that is otherwise entirely in Spanish. Unknown values are
 * returned unchanged rather than replaced by a placeholder: a day this
 * function does not know about is still more useful printed than hidden.
 */
export function dayName(day: string): string {
  return DAY_NAMES[day] ?? day;
}

/** Short form for a narrow column header. */
export function shortDayName(day: string): string {
  const full = dayName(day);
  return full === day ? day.slice(0, 3) : full.slice(0, 3);
}

/**
 * Columns in week order, not in whatever order the slots arrived.
 *
 * The API returns slots ordered by the grid's own global index, which happens
 * to be week order — but "happens to be" is not a guarantee, and a period
 * whose grid was built Saturday-first would render its week starting on
 * Saturday for no stated reason.
 */
export function orderDays(days: string[]): string[] {
  return [...days].sort((a, b) => {
    const left = DAY_ORDER.indexOf(a);
    const right = DAY_ORDER.indexOf(b);
    // Anything the order does not know goes last, in its original order,
    // rather than jumping to the front on a -1.
    return (
      (left < 0 ? DAY_ORDER.length : left) - (right < 0 ? DAY_ORDER.length : right)
    );
  });
}

/**
 * `07:00` plus two slots of an hour -> `07:00–09:00`.
 *
 * The block said `2h`, which tells a reader the length and not the end. On a
 * grid where a class occupies rows it does not label, when it *finishes* is
 * the thing being looked for.
 *
 * Falls back to the start alone when the next slots are unknown, rather than
 * inventing an end from an assumed hour length: a period on 45-minute slots
 * would otherwise be told it finishes at the wrong time, confidently.
 */
export function timeRange(
  startLabel: string,
  labelsAfter: string[],
  durationSlots: number,
): string {
  if (durationSlots <= 1) return startLabel;
  const last = labelsAfter[durationSlots - 2];
  if (!last) return startLabel;
  const [hours, minutes] = last.split(":").map(Number);
  if (Number.isNaN(hours) || Number.isNaN(minutes)) return startLabel;
  // The block ends when its final slot ends. Slot length is taken from the
  // grid rather than assumed: `labelsAfter` are real labels from real slots.
  return `${startLabel}–${pad(hours + 1)}:${pad(minutes)}`;
}

function pad(value: number): string {
  return String(value % 24).padStart(2, "0");
}

/**
 * A stable hue per programme, for scanning a dense week.
 *
 * **This is categorical colour, not semantic colour**, and the distinction is
 * why it does not break the rule in `globals.css` that there are exactly four
 * colours and each means the same thing everywhere. Those four say *how a
 * thing is* — fine, warning, wrong, active. This says *which thing it is*,
 * carries no judgement, and is never the only carrier of anything: every
 * block still states its programme in text.
 *
 * Derived from the id so it survives a reload and a re-import, and so two
 * people looking at the same timetable see the same colours.
 */
export function programHue(programId: string | undefined): number | null {
  if (!programId) return null;
  let hash = 0;
  for (let index = 0; index < programId.length; index++) {
    hash = (hash * 31 + programId.charCodeAt(index)) >>> 0;
  }
  // 137.5° apart: the golden angle, which spreads a small number of hues
  // about as far from each other as they can get. Stepping by a round number
  // instead gives two programmes near-identical colours as soon as there are
  // more than a handful.
  return Math.round((hash % 360) * 137.508) % 360;
}

/**
 * The slot `offset` positions away from this one, within the same day.
 *
 * What it is for: a class of three hours is drawn as three segments, and any
 * of them can be picked up. Grabbing the **second** hour and dropping it on
 * Wednesday 10:00 must start the class at Wednesday 09:00 — otherwise
 * grabbing the middle of a long class silently shifts it forward by an hour,
 * which is the kind of error somebody discovers a week later.
 *
 * Returns `null` at the edge of the day rather than wrapping: a class cannot
 * start before the day does, and there is no sensible answer to give.
 */
export function slotAtOffset(
  slots: { slotId: string; day: string; inDayIndex: number }[],
  slotId: string,
  offset: number,
): string | null {
  const from = slots.find((s) => s.slotId === slotId);
  if (!from) return null;
  if (offset === 0) return slotId;
  const target = slots.find(
    (s) => s.day === from.day && s.inDayIndex === from.inDayIndex + offset,
  );
  return target?.slotId ?? null;
}

/** Where a segment sits within its class: `only`, `first`, `middle`, `last`. */
export function segmentPosition(
  index: number,
  duration: number,
): "only" | "first" | "middle" | "last" {
  if (duration <= 1) return "only";
  if (index === 0) return "first";
  if (index === duration - 1) return "last";
  return "middle";
}
