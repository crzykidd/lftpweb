import { Code, DocsPage, Note, P, Step, UL, Warn, Where } from './prose'

/** Docs → Quick start (2026-08-13, prompts/2026-08-13-docs-section.md): the real first-run
 * path, in the order you actually do it, with every step linking to the page it describes.
 *
 * **Every claim on this page was read out of the code before it was written**, not recalled --
 * roughly thirty commits landed on 2026-08-12/13 and several features were reshaped hours after
 * shipping. Where something could not be confirmed it is omitted rather than guessed. The
 * cautionary tale this project keeps: `docker/Dockerfile`'s comment claimed rar support for nine
 * phases while the image had no RAR decoder in it at all.
 */
export function QuickStartPage() {
  return (
    <DocsPage
      title="Quick start"
      lede="Six steps from a running container to a downloading queue. Each one links to the page it describes."
    >
      <Step n={1} title="Deploy the container">
        <P>
          The UI is on port <Code>8087</Code>. Three volumes matter, and two of them are easy to
          get backwards:
        </P>
        <UL>
          <li>
            <Code>/config</Code> — the SQLite database, logs, backups, and the encryption key
            used for stored credentials. Back this up; everything else is replaceable.
          </li>
          <li>
            <Code>/downloads</Code> — <strong>where downloads land.</strong> This is the path a
            queue's <em>Local path</em> points into: lftp writes here, and the reconciler scans
            here to work out what you already have.
          </li>
          <li>
            <Code>/staging</Code> — optional, and <strong>not</strong> a landing zone. It is the{' '}
            <em>destination</em> that post-processing relocates a finished, verified item{' '}
            <strong>to</strong>, after it has fully downloaded into <Code>/downloads</Code>. The
            field is called <em>Final destination</em> in{' '}
            <Where to="/settings/queues">Settings → Queues</Where> for exactly that reason. Leave
            it unset and items stay where they downloaded.
          </li>
        </UL>
        <P>
          <Code>PUID</Code>, <Code>PGID</Code>, and <Code>UMASK</Code> are honoured — the
          entrypoint applies them and drops root before the app starts. Set them to match your
          share's expected identity if your downloads live on NFS.
        </P>
      </Step>

      <Step n={2} title={<>Connect to the seedbox</>}>
        <P>
          <Where to="/settings/connection">Settings → Connection</Where>. Fill in the address,
          port (22 unless your seedbox says otherwise), and username, then pick an{' '}
          <strong>auth method</strong>:
        </P>
        <UL>
          <li>
            <strong>SSH key</strong> — either <em>paste the private key</em> into the form, or
            give a <em>Key path</em> to a key file you have mounted into the container. A pasted
            key is encrypted at rest and decrypted only in memory; passphrase-protected keys are
            rejected, so strip the passphrase or use a key path instead. If both are set the
            pasted key wins, and the form tells you which one is actually in use.
          </li>
          <li>
            <strong>SSH agent</strong> — an agent socket reachable from inside the container (
            <Code>SSH_AUTH_SOCK</Code>, or the platform default).
          </li>
          <li>
            <strong>Password</strong> — stored encrypted. Leave the field blank on a later save
            to keep the password you already stored; it is never pre-filled back into the form.
          </li>
        </UL>
        <P>
          <strong>Known-hosts policy</strong> decides what happens when the seedbox presents its
          host key. <em>Accept and pin on first use</em> (the default) trusts the key it sees the
          first time and refuses any different key afterwards. <em>Strict</em> only ever accepts
          a key you have already pinned — safest, but it will refuse to connect until something
          has pinned one. <em>Insecure</em> never verifies at all.
        </P>
        <P>
          Use <strong>Test connection</strong> before saving. It reports the failure class
          (auth, unreachable, host key) rather than a generic error.
        </P>
      </Step>

      <Step n={3} title="Create a queue">
        <P>
          <Where to="/settings/queues">Settings → Queues</Where>. A queue is one{' '}
          <em>remote path → local path</em> mapping with its own settings. Give it a name, the
          remote directory on the seedbox, and the local directory to mirror into.
        </P>
        <P>
          <strong>Sync mode is the consequential choice on this page.</strong>{' '}
          <Code>copy</Code> (the default) downloads and never touches the seedbox.{' '}
          <Code>move</Code> downloads, verifies, and then <strong>deletes the remote copy</strong>
          . That delete is irreversible and it happens on every item the queue finishes.
        </P>
        <Warn>
          Only point a <Code>move</Code> queue at a <strong>hardlink pickup directory</strong> —
          a directory your torrent client populates with links on completion. Point it at the
          torrent client's own seeding data directory and it will destroy your seeds. Saving a{' '}
          <Code>move</Code> queue requires ticking a confirmation that says so.
        </Warn>
        <P>
          <Code>sync</Code> appears in the dropdown but is disabled: propagating local deletes
          back to the seedbox is designed and not built.
        </P>
      </Step>

      <Step n={4} title="Let it scan">
        <P>
          Each queue is scanned on its own <strong>scan interval</strong>: <em>site default</em>{' '}
          (30 seconds unless <Code>LFTPWEB_SCAN_INTERVAL_S</Code> overrides it), 10s, 30s, 60s, or{' '}
          <em>none — on-demand only</em>. A scan is an SSH round trip running <Code>find</Code>{' '}
          over the whole remote tree, so 10s is real, continuous load on a shared seedbox.
        </P>
        <P>
          <strong>Rescan now</strong>, at the top of the <Where to="/files">Files</Where> page,
          forces a full pass across every queue immediately instead of waiting. Each queue's
          heading shows how long ago it was last scanned, and surfaces a scan error or warning
          right there rather than only in the log.
        </P>
        <Note>
          A queue set to <em>on-demand only</em> has no timer at all. Auto-queue only runs at the
          end of a scan pass, so on such a queue nothing is picked up automatically until
          something forces a scan.
        </Note>
      </Step>

      <Step n={5} title="Queue a transfer by hand">
        <P>
          On the <Where to="/files">Files</Where> page, each row has a <strong>Queue</strong>{' '}
          button; select multiple rows (shift-click for a range) to queue, stop, delete, or reset
          them in bulk. Watch progress on the row's own inline bar, or on the{' '}
          <Where to="/transfers">Transfers</Where> page. Progress is derived from bytes on disk
          versus the known remote size, so a stopped transfer resumes from its partial rather
          than restarting.
        </P>
        <P>
          A manual Queue click always wins: it is not filtered by auto-queue suppression, and it
          is not held back by the settle gate's eligibility check. It is <em>not</em> a way to
          skip the settle gate's completion check — see{' '}
          <Where to="/docs/concepts">Concepts</Where>.
        </P>
      </Step>

      <Step n={6} title="Then, optionally">
        <P>
          Everything below defaults to off. Turn on one at a time and watch what it does before
          adding the next.
        </P>
        <UL>
          <li>
            <strong>Auto-queue and patterns</strong> —{' '}
            <Where to="/settings/queues">Settings → Queues</Where>, per queue. Turn on
            auto-queue, then add <Code>select</Code> / <Code>skip</Code> /{' '}
            <Code>file_exclude</Code> patterns; the editor previews what each one would match
            against the tree you actually have. <em>Patterns-only</em> changes the meaning of
            having no <Code>select</Code> pattern from "match everything" to "match nothing".
          </li>
          <li>
            <strong>Post-processing</strong> —{' '}
            <Where to="/settings/post-processing">Settings → Post-processing</Where> holds the
            site-wide default for verify, extract, delete-archives-after-extract, and
            move-to-final-destination. Each queue then inherits or overrides those individually.
          </li>
          <li>
            <strong>The settle gate</strong> —{' '}
            <Where to="/settings/transfer">Settings → Transfer</Where>. This one is{' '}
            <em>on</em> by default, unlike everything else here. Read the settle-gate section
            under <Where to="/docs/concepts">Concepts</Where> before switching it off.
          </li>
        </UL>
        <Note>
          <strong>Local retention</strong> (delete local copies older than N days) and{' '}
          <strong>orphan temp-file cleanup</strong> exist and work, but have{' '}
          <strong>no Settings page yet</strong> — they are configured only through the API (
          <Code>PUT /api/settings/retention</Code>, which also has a dry-run{' '}
          <Code>POST /api/settings/retention/preview</Code>, and{' '}
          <Code>PUT /api/settings/orphan-temp-cleanup</Code>). Both default off and both run on
          an hourly sweep; neither has a "run now" trigger.
        </Note>
      </Step>
    </DocsPage>
  )
}
