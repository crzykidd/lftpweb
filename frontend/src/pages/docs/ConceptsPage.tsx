import { Code, DocsPage, Jump, Note, P, Section, Table, UL, Warn, Where } from './prose'

/** Docs → Concepts (2026-08-13, prompts/2026-08-13-docs-section.md). Not a feature tour: every
 * section here is something that actually confused a real person running this app during the
 * 2026-08-13 live-testing rounds. Kept short on purpose — someone reading this is stuck, not
 * studying.
 *
 * Same rule as the quick start: every claim was read out of the code (`core/settle.py`,
 * `core/queue.py`, `core/autoqueue.py`, `core/local_delete.py`, `core/itemview.py`,
 * `core/postprocess.py`, `api/history.py`, `api/jobs.py`) before it was written here.
 * Architecture belongs in `DESIGN.md` and is deliberately not repeated.
 */
export function ConceptsPage() {
  return (
    <DocsPage
      title="Concepts"
      lede="The six things that actually trip people up, and what to do about each."
    >
      <Jump
        items={[
          { id: 'settle', label: 'Nothing downloaded for a minute' },
          { id: 'suppression', label: "An item won't re-download" },
          { id: 'blast-radius', label: 'Dismiss vs Clear vs Reset' },
          { id: 'icons', label: 'The lifecycle icons' },
          { id: 'copy-move', label: 'copy vs move' },
          { id: 'inherit', label: 'Inherit vs override' },
        ]}
      />

      <Section id="settle" title="Why nothing downloaded for a minute — the settle gate">
        <P>
          A release still being written to your seedbox looks byte-complete the moment whichever
          files arrived first are whole. Download it then and you import a third of a season,
          extract a truncated archive, and — on a <Code>move</Code> queue — delete the remote
          copy of a release that was never fully there. The settle gate is what stops that.
        </P>
        <P>
          Before an item counts as settled, a fingerprint of its whole remote subtree —{' '}
          <strong>file count, total bytes, and newest modification time</strong> — has to be
          identical across <strong>two consecutive scans</strong> <em>and</em> at least{' '}
          <strong>60 seconds</strong> of wall-clock time. Both, not either: the scan count alone
          can't tell a settled item from one on a queue that simply hasn't been scanned much yet.
        </P>
        <P>Two readings show up on a Files row's status chip while this is happening:</P>
        <UL>
          <li>
            <Code>Arriving · 3.4 GB</Code> — the remote side is <em>still changing</em>. Nothing
            has been confirmed unchanged even once yet, so there is no honest countdown to show;
            the byte count is what has landed on the seedbox so far, and it climbing is the
            progress signal.
          </li>
          <li>
            <Code>Waiting 1/2 · 35s</Code> — it has stopped changing and the clock is running:
            one of the two required matching scans so far, 35 of the 60 required seconds. Hover
            the chip for the same thing as a full sentence.
          </li>
        </UL>
        <P>
          While an item is settling, its <strong>Remote</strong> icon turns amber rather than
          green — the remote copy really is there, it just hasn't held still long enough to
          trust yet. The Local icon stays dim, because nothing has been queued.
        </P>
        <P>
          The gate applies in two places. It stops <strong>auto-queue</strong> from picking an
          item up, and — the half that matters more — it stops a finished download from being
          treated as <em>complete</em>: the item is held instead of marked{' '}
          <Code>DOWNLOADED</Code>, and no verification, extraction, relocation, or remote delete
          runs against it. <strong>Clicking Queue by hand overrides the first, never the
          second.</strong> The worst case of queueing a still-arriving item by hand is a wasted
          partial transfer that resumes later — never a bad import or a bad delete.
        </P>
        <Note>
          The gate is <strong>on by default</strong> and lives at{' '}
          <Where to="/settings/transfer">Settings → Transfer</Where>. It is a single on/off — the
          two-scan and 60-second thresholds are fixed and not tunable per install. Turning it off
          sheds up to about a minute of latency per transfer, and is only safe if your seedbox's
          landing path is atomic end to end.
        </Note>
      </Section>

      <Section id="suppression" title="Why an item will not re-download — auto-queue suppression">
        <P>
          Auto-queue deliberately refuses to pick an item up again once one of four things has
          happened to it. This is the single most common "why is it ignoring this" and it is
          almost always working as intended.
        </P>
        <Table
          head={['Reason', 'What caused it']}
          rows={[
            [
              <Code key="c">user_stopped</Code>,
              'You stopped the transfer — either before it started or while it was running.',
            ],
            [
              <Code key="c">retries_exhausted</Code>,
              'The transfer failed and will not be retried again on its own. Only two error classes are ever retried at all (host unreachable, TLS), so this also covers a failure lftpweb could not classify.',
            ],
            [
              <Code key="c">permanent_error</Code>,
              'The failure was one that will recur identically: auth failed, permission denied, the remote path is gone, or the disk is full.',
            ],
            [
              <Code key="c">deleted_local</Code>,
              'lftpweb deleted the local copy itself — a manual delete from Files, or the retention sweep.',
            ],
          ]}
        />
        <P>
          <strong>Suppression only ever stops auto-queue.</strong> A manual{' '}
          <strong>Queue</strong> click on the <Where to="/files">Files</Where> page is never
          filtered by it, and using <strong>Retry</strong> on a failed job from{' '}
          <Where to="/transfers">Transfers</Where> lifts it.
        </P>
        <P>
          A suppressed row whose local copy <em>lftpweb itself deleted</em>, and whose remote copy
          is still there, shows <strong>Re-Download</strong> instead of Queue. It is the same
          click — the different word is telling you this is a release you already had, back
          again, and that nothing will fetch it automatically.
        </P>
        <Note>
          Not every "removed" row is suppressed. If an item vanished from both sides on its own
          and lftpweb resolved it as gone, it is <em>not</em> suppressed and shows a plain{' '}
          <strong>Queue</strong>. And the site-wide{' '}
          <Where to="/settings/queues">Re-download items removed outside lftpweb</Where> setting
          governs only the case where <em>something else</em> — an <Code>*arr</Code> importer, a
          script, a human — took the local copy away. It never applies to a copy lftpweb deleted
          itself.
        </Note>
        <P>
          <strong>To make a path genuinely reusable, use Reset item tracking.</strong> Clearing
          History will not do it — see below.
        </P>
      </Section>

      <Section id="blast-radius" title="Dismiss vs Clear history vs Reset item tracking">
        <P>
          Three actions with similar names, sitting a few pixels apart, with completely different
          blast radii. This is the table to check before clicking one.
        </P>
        <Table
          head={['Action', 'Where', 'What it removes', 'What survives']}
          rows={[
            [
              <strong key="a">Dismiss</strong>,
              <Where key="b" to="/transfers">
                Transfers
              </Where>,
              'Nothing. It flags one failed or cancelled job as dismissed so it stops cluttering the Transfers list.',
              'Everything — the job is still in History, marked dismissed. Reversible in the sense that nothing was lost.',
            ],
            [
              <strong key="a">Clear history</strong>,
              <Where key="b" to="/history">
                History
              </Where>,
              'Transfer records and audit events — one row, everything matching your current filter, or everything. No category is protected, including remote-delete audit entries.',
              'Every item, every suppression flag, every local file. Clearing History changes nothing about what will or will not download next.',
            ],
            [
              <strong key="a">Reset item tracking</strong>,
              <Where key="b" to="/files">
                Files
              </Where>,
              "The item record itself and its whole subtree — plus its settle bookkeeping and archive-cleanup bookkeeping. Its transfer records go too, as an unavoidable consequence of the item row going.",
              'Your local files, untouched. Audit events stay in History but lose their link back to the item.',
            ],
          ]}
        />
        <P>
          Put plainly: <strong>Dismiss tidies a list. Clear history deletes records. Reset item
          tracking forgets a path</strong> — it makes lftpweb treat that path as brand new on the
          next scan, which is the only one of the three that changes future behaviour. That is
          exactly what you want after a suppressed, stopped, or permanently-failed item, and
          exactly what you do not want by accident.
        </P>
        <Warn>
          Resetting a path whose remote copy still exists, on a queue with auto-queue on, will
          start it downloading again on the next scan. Every reset panel computes and states the
          real numbers before you confirm — how many of the targets still exist remotely, whether
          auto-queue is on, and how soon the next scan is — rather than a generic warning. Read
          that line; it is accurate.
        </Warn>
        <P>
          Reset lives in one control on the Files page, below the file tree, with a scope
          selector — the rows you have selected, a whole queue, or a filename pattern — and
          Cancel always available. Every scope follows the same flow: choose a scope, review a
          preview of exactly what would be reset, then confirm. The whole-queue scope, the most
          destructive, additionally asks you to type the queue's name once you have reviewed
          that preview. Any target that is busy — mid-transfer, mid-post-processing, mid-delete —
          is skipped and reported rather than raced.
        </P>
      </Section>

      <Section id="icons" title="The lifecycle icons">
        <P>
          Every Files row carries four small icons: a <strong>cloud</strong> (Remote), a{' '}
          <strong>hard drive</strong> (Local), a <strong>shield</strong> (Verified), and a{' '}
          <strong>box</strong> (Extracted). Hover any of them for the specific fact behind it —
          sizes and timestamps, not just the name.
        </P>
        <P>
          Colour means: <strong>green</strong> done and good, <strong>amber</strong> in progress,{' '}
          <strong>red</strong> failed, <strong>dim</strong> not applicable or deliberately gone.
          Dim is never a fault.
        </P>
        <P>
          The distinction that makes the whole row readable:{' '}
          <strong>the two presence icons describe the world right now and can go dark again; the
          two milestone icons record something that happened and stay lit.</strong> A file that
          exists locally today may not tomorrow, so the hard drive can go dim. A file that was
          verified was verified — that shield does not un-light because the file later moved.
        </P>
        <P>The worked example, because it looks alarming and is not:</P>
        <Note>
          A completed item on a <Code>move</Code> queue that verified and extracted reads{' '}
          <strong>cloud dim, drive green, shield green, box green</strong>. The dim cloud is not
          an error — it is the queue doing its job. The remote copy was deleted <em>because</em>{' '}
          verification passed, and hovering the cloud says exactly that, with the time it
          happened.
        </Note>
      </Section>

      <Section id="copy-move" title="copy vs move">
        <P>
          <Code>copy</Code> downloads and never touches the seedbox. <Code>move</Code> does one
          extra thing, once, at the very end of post-processing: it deletes the item's remote
          copy. Nothing else about a <Code>move</Code> queue behaves differently — not the
          transfer, not extraction, not relocation.
        </P>
        <P>
          <strong>
            <Code>move</Code> forces verification on, regardless of the site-wide setting and
            regardless of any per-queue override.
          </strong>{' '}
          Verification is the sole gate on that irreversible delete, so it is not something a
          toggle elsewhere can switch off underneath you. In the queue form the Verify checkbox
          shows as ticked and locked, with the reason stated on it.
        </P>
        <P>
          If verification cannot produce evidence — no <Code>.sfv</Code>/<Code>.md5</Code>{' '}
          sidecar, and the whole-file-read fallback turned off — the remote delete is{' '}
          <strong>withheld and audited</strong>, not silently skipped. You will find it on the{' '}
          <Where to="/history">History</Where> page as a warning event.
        </P>
        <Warn>
          A <Code>move</Code> queue's remote path must be a hardlink pickup directory, never your
          torrent client's live seeding data. The delete is real and there is no undo.
        </Warn>
        <Note>
          One ordering quirk worth knowing: on a <Code>move</Code> queue the remote copy is
          deleted before extraction runs. A failed extraction therefore happens after the remote
          copy is gone. The downloaded archives are still on disk, so it is recoverable — but you
          recover it locally, not by re-downloading.
        </Note>
      </Section>

      <Section id="inherit" title="Inherit vs override on the post-processing toggles">
        <P>
          There are four post-processing steps — <strong>verify</strong>,{' '}
          <strong>extract</strong>, <strong>delete archives after extract</strong>, and{' '}
          <strong>move to final destination</strong> — and each one exists at two levels.
        </P>
        <P>
          <Where to="/settings/post-processing">Settings → Post-processing</Where> holds the{' '}
          <strong>site-wide default</strong>. Each queue's copy of the toggle, in{' '}
          <Where to="/settings/queues">Settings → Queues</Where>, is by default set to{' '}
          <strong>inherit</strong> that value — shown ticked or unticked to match, but locked,
          with a line saying what it currently resolves to. Change the site-wide value and every
          inheriting queue follows immediately.
        </P>
        <P>
          <strong>Override for this queue</strong> unlocks it. The override is seeded at whatever
          the value resolves to right now, so clicking it never changes what actually runs — it
          only stops the queue from tracking the site setting. <strong>Revert to inherit</strong>{' '}
          puts it back, and tells you what it will resolve to before you click, so you are not
          reverting to an invisible value.
        </P>
        <P>Two toggles are conditional, and say so in place:</P>
        <UL>
          <li>
            <strong>Delete archives after extract</strong> is unavailable unless extraction
            actually runs for that queue.
          </li>
          <li>
            <strong>Move to final destination</strong> is unavailable until the queue has a Final
            destination set.
          </li>
        </UL>
        <Note>
          Everything post-processing does defaults to off at both levels — a fresh install runs
          none of it. The one exception in the other direction is <Code>move</Code> mode's forced
          verification, above.
        </Note>
      </Section>
    </DocsPage>
  )
}
