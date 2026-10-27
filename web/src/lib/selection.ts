/** A new set with `id` added, or removed when it was already in `current` (a checkbox toggle). */
export function toggleInSet(
  current: ReadonlySet<string>,
  id: string,
): Set<string> {
  const next = new Set(current);
  if (!next.delete(id)) next.add(id);
  return next;
}

/** "Process 1 selected video" / "Process 3 selected videos": the count and the right noun form. */
export function countLabel(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}
