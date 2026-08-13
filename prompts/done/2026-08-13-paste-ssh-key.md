---
name: 2026-08-13-paste-ssh-key
status: done
created: 2026-08-13
model: sonnet
completed: 2026-08-13
result: >
  Migration 014 adds host.ssh_key_enc, encrypted like password_enc. asyncssh takes the
  decrypted key straight into memory (client_keys accepts a parsed SSHKey, confirmed against
  asyncssh 2.24.0) -- scanning never writes it to disk. lftp materialises it per-job on /run
  tmpfs alongside the rc file, mode 0600, unlinked with the job. Pasted key wins over key_path
  when both are set; key_path keeps working unchanged. A passphrase-protected or unparseable
  key is rejected at save time. CredentialRedactor now scrubs multi-line PEM blocks. A real
  key-auth round trip (paste -> encrypt -> decrypt -> asyncssh scan + lftp transfer) runs
  against the fake seedbox in tests/test_ssh_key_e2e.py. 794 tests passing (27 new), both
  lint gates clean, frontend build/lint clean.
---

# Task: Let the user paste an SSH private key, encrypted at rest, materialised to tmpfs

Today `auth_method: 'key'` stores only a **path** (`host.key_path`), and the user must mount the
key file into the container themselves. The API checks the field is non-empty and nothing else
— not that the file exists, is readable, or has sane permissions.

That fails in a confusing way: OpenSSH refuses a private key with loose permissions
(`UNPROTECTED PRIVATE KEY FILE`, wants `0600`) and **lftp shells out to `ssh` for transfers**,
while **scanning uses asyncssh**, which is more lenient. So a wrongly-permissioned key gives
you working scans and failing transfers with nothing pointing at the cause — the same shape as
the missing-`lftp`-binary bug from the first live deployment.

## What the user asked for, and the agreed design

Paste the key into Settings → Connection, encrypt at rest exactly like the seedbox password,
decrypt and write it to tmpfs on startup and on change.

**Store it in the `host` table encrypted with `core/crypto.py`, the same mechanism as
`password_enc`.** This was weighed against keeping the ciphertext in a separate file outside
the database, and the database won: one crypto mechanism instead of two, and — decisively —
a config backup round-trips the key, where a file excluded from backups would drop the user
into the "credentials need re-entry" state on restore.

Both options leave only *ciphertext* in a backup; `core/crypto.py`'s `secret.key` is provably
absent from `VACUUM INTO` backups (there is a test that byte-searches for it). So the security
difference is narrow. Record this reasoning in `docs/decisions.md` — someone will re-litigate it.

## Requirements

1. **Encrypt with `core/crypto.py`**, stored in `host` beside `password_enc`. Migration
   **014** (verify nothing has claimed it).

2. **Plaintext must never touch the writable layer.** The runtime image has a read-only root
   filesystem and a `/run` tmpfs; `core/lftp.py` already writes per-job rc files containing the
   seedbox password there at mode 0600. **Use that established path — do not invent a second
   secret-materialisation mechanism.**

3. **Check whether asyncssh needs a file at all.** `core/remote.py:480` passes
   `client_keys=[host.key_path]`, but asyncssh accepts key *material*, not only paths. If it
   does, scanning should use the decrypted key **in memory** and never write anything. Only
   lftp genuinely needs a file, because it shells out to `ssh -i <path>`. Verify against the
   installed asyncssh rather than assuming.

4. **Decide: per-process or per-job file.** If only lftp needs it, writing it alongside the
   existing per-job rc file (created and torn down per transfer) means the plaintext exists on
   tmpfs only while a transfer is actually running — strictly better than a file held for the
   process lifetime. Weigh that against the extra work per job and **state your choice with
   reasoning**. Either way it must be rewritten on **startup and on change**: `/run` is tmpfs
   and is empty after every restart, so "write it once when saved" silently breaks transfers
   after a container restart.

5. **Redaction.** `logsetup.CredentialRedactor` already scrubs credentials on the way into
   logs. A pasted key must be covered **including its multi-line PEM form** — a redactor that
   only matches single-line secrets will happily log a private key across 20 lines. Test that
   directly.

6. **Reuse `credentials_need_reentry`.** Phase 8 built this for a password that will not
   decrypt: `core/queue.py._admit` holds transfers and `core/engine.py.scan_queue` fails that
   queue's scan cleanly. A key that will not decrypt must ride the same flag, not a parallel
   one.

7. **Keep `key_path` working.** This is an additional option, not a replacement — anyone
   already mounting a key must be unaffected. Decide how the two coexist when both are set
   (the pasted key winning is the obvious answer; say so explicitly rather than leaving it to
   chance) and make the UI clear about which is in use.

8. **Validate on paste.** At minimum, that it parses as a private key. If it does not, say so
   at save time rather than at the next transfer. Consider surfacing whether it is
   passphrase-protected — a passphrase-protected key will fail non-interactively and that is
   worth catching immediately, not at 3am.

## Before you start

- `core/crypto.py` and how `password_enc` flows through `core/engine.py.load_host_config`.
- `core/remote.py` (asyncssh, `client_keys`) and `core/lftp.py` (rc file, `-i`, the `/run`
  tmpfs conventions and its 0600 handling).
- `core/logsetup.py`'s `CredentialRedactor`.
- `api/settings.py`'s host create/update, and `frontend/src/pages/settings/ConnectionTab.tsx`.
- `docker/entrypoint.sh` and the read-only-root/`/run` arrangement in `docker/Dockerfile`.

## Working tree check

`git status --porcelain`. If files you need are dirty, list them and ask.

## Tests

- Round trip: paste → encrypted at rest → decrypted → usable by both the asyncssh and lftp
  paths. Prove the ciphertext in the database is not the plaintext.
- **The plaintext key never appears on the writable layer** — assert on the actual path used.
- A restart re-materialises it (or the per-job path recreates it), so transfers work after one.
- A key that will not decrypt sets `credentials_need_reentry` and holds transfers rather than
  spawning doomed jobs.
- **The redactor scrubs a multi-line PEM.**
- An invalid or passphrase-protected key is rejected at save time with a clear message.
- `key_path` still works untouched.

## Conventions to honor

- `docs/decisions.md`, newest at top — the DB-vs-file reasoning, and the per-job-vs-per-process
  choice.
- `CHANGELOG.md` under `### Added`; `DESIGN.md` §8/§11 (standing approval to edit directly).
- Both lint gates: `uvx ruff@0.8.4 check --config ruff.toml .` **and**
  `uvx ruff@0.8.4 format --config ruff.toml --check .`.
- `npm run lint` / `npm run build`; `uv run pytest` with the fake seedbox up.
- **Never log the key, never include it in an error message or an API response.** A `GET` of
  host settings must not return it — mirror exactly how `password_enc` is handled today.
- **You cannot see the UI.** No browser here.

## When done

1. Update frontmatter (`status`, `completed`, `result`).
2. `git mv` into `prompts/done/` (or `prompts/failed/`).
3. **Do not commit.** Report back: file list, proposed one-line `feat:` message, whether
   asyncssh could take the key in memory, your per-job-vs-per-process choice and why, how the
   redactor handles PEM, what happens when both `key_path` and a pasted key are set, test
   count, lint results, and anything not fixed. Never `git add -A`, never push.
