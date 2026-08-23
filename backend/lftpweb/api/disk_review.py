"""The disk review scan's own endpoint (docs/download-client-framework-spec.md §11, stage 4 of
#18) -- `POST /api/disk-review/scan`, manual trigger only (spec §11.3: an SSH walk over
potentially large trees must never ride a page load). Review-only: this router has no delete
endpoint at all -- stage 5 (spec §14) is the one that adds it.

Thin by design -- `core/disk_review.py.run_scan` does the actual work (SSH walk, client
contact, reconciliation); this module's only job is wiring the request's app state into that
function's injected seams, the same shape `api/settings_clients.py`'s own test-connection
endpoint already uses for `get_client_class`/`decrypt_secret`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from fastapi import APIRouter, Request

from lftpweb.core.clients import get_client_class
from lftpweb.core.crypto import decrypt_secret
from lftpweb.core.disk_review import run_scan
from lftpweb.core.engine import load_host_config
from lftpweb.models import (
    DiskReviewBrokenSeedOut,
    DiskReviewClientFailureOut,
    DiskReviewDebrisOut,
    DiskReviewScanResponse,
    DiskReviewSeedingEstateOut,
    DiskReviewSkippedBasePathOut,
)

router = APIRouter(prefix="/api/disk-review")


def _decrypt_client_secret(config_dir: str):
    def _inner(ciphertext: str) -> dict:
        # Mirrors `core/clientsync.py._process_instance`'s own decrypt-then-parse shape --
        # `decrypt_secret` raises `DecryptionError` on failure, `run_scan` catches whatever this
        # raises and reports the instance as a client failure for this pass (spec §4.2: an
        # undecryptable secret means "cannot report," not "reports nothing to claim").
        return json.loads(decrypt_secret(config_dir, ciphertext))

    return _inner


@router.post("/scan", response_model=DiskReviewScanResponse)
async def scan_for_review(request: Request) -> DiskReviewScanResponse:
    db = request.app.state.db
    config_dir = request.app.state.config_dir
    engine = request.app.state.engine
    postprocess = getattr(request.app.state, "postprocess", None)

    # `load_host_config` never raises on a decrypt failure -- it sets
    # `credentials_need_reentry` on the returned `HostConfig` instead (its own docstring).
    # `run_scan` checks that flag itself and marks every base path unavailable rather than
    # attempting a connection it already knows will raise `DecryptionNeededError`.
    host = await load_host_config(db, config_dir)

    outcome = await run_scan(
        db=db,
        pool=engine.pool,
        host=host,
        get_client_class=get_client_class,
        decrypt_client_secret=_decrypt_client_secret(config_dir),
        postprocess_in_flight_ids=(
            postprocess.in_flight_item_ids() if postprocess is not None else frozenset()
        ),
    )
    result = outcome.result

    return DiskReviewScanResponse(
        debris=[
            DiskReviewDebrisOut(
                root=d.root,
                rel_path=d.rel_path,
                abs_path=d.abs_path,
                size=d.size,
                mtime=d.mtime,
                inode=d.inode,
                nlink=d.nlink,
                link_paths=list(d.link_paths),
            )
            for d in result.debris
        ],
        seeding_estate=[
            DiskReviewSeedingEstateOut(
                root=s.root,
                rel_path=s.rel_path,
                abs_path=s.abs_path,
                size=s.size,
                claimed_by_client_id=s.claimed_by_client_id,
                claimed_by_client_name=s.claimed_by_client_name,
                claimed_transfer_id=s.claimed_transfer_id,
                claimed_transfer_name=s.claimed_transfer_name,
                claimed_content_path=s.claimed_content_path,
            )
            for s in result.seeding_estate
        ],
        broken_seeds=[
            DiskReviewBrokenSeedOut(
                client_id=b.client_id,
                client_name=b.client_name,
                transfer_id=b.transfer_id,
                transfer_name=b.transfer_name,
                content_path=b.content_path,
            )
            for b in result.broken_seeds
        ],
        skipped_base_paths=[
            DiskReviewSkippedBasePathOut(root=s.root, reason=s.reason)
            for s in result.skipped_base_paths
        ],
        client_failures=[
            DiskReviewClientFailureOut(
                client_id=f.client_id, client_name=f.client_name, reason=f.reason
            )
            for f in outcome.client_failures
        ],
        scanned_at=datetime.now(UTC).isoformat(),
    )
