/** Shell-only placeholder for a page whose real content lands in a later phase. */
export function PagePlaceholder({ title }: { title: string }) {
  return (
    <div className="flex h-full min-h-40 items-center justify-center rounded-lg border border-dashed border-zinc-300 text-zinc-400 dark:border-zinc-700 dark:text-zinc-600">
      {title}
    </div>
  )
}
