-- Approval gate schema, version 1.

CREATE TABLE requests (
    id             TEXT PRIMARY KEY,
    workflow_id    TEXT NOT NULL,
    dedupe_key     TEXT,
    status         TEXT NOT NULL
                   CHECK (status IN ('pending','approved','rejected','expired','cancelled')),
    action_json    TEXT NOT NULL,
    action_digest  TEXT NOT NULL,
    summary        TEXT NOT NULL,
    requested_by   TEXT NOT NULL,
    created_at_ms  INTEGER NOT NULL,
    expires_at_ms  INTEGER NOT NULL,
    version        INTEGER NOT NULL DEFAULT 0,
    CHECK (expires_at_ms > created_at_ms),
    CHECK (length(summary) > 0),
    CHECK (length(action_digest) = 64
           AND action_digest NOT GLOB '*[^0-9a-f]*')
);

CREATE UNIQUE INDEX ux_requests_pending_dedupe
    ON requests(workflow_id, dedupe_key)
    WHERE status = 'pending' AND dedupe_key IS NOT NULL;

CREATE INDEX ix_requests_pending_deadline
    ON requests(status, expires_at_ms);

CREATE INDEX ix_requests_workflow
    ON requests(workflow_id, status, created_at_ms);

CREATE TABLE transitions (
    seq                 INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id          TEXT NOT NULL UNIQUE
                        REFERENCES requests(id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    from_status         TEXT NOT NULL CHECK (from_status = 'pending'),
    to_status           TEXT NOT NULL
                        CHECK (to_status IN ('approved','rejected','expired','cancelled')),
    actor               TEXT NOT NULL,
    reason              TEXT,
    at_ms               INTEGER NOT NULL,
    idempotency_key     TEXT,
    request_fingerprint TEXT,
    CHECK (idempotency_key IS NULL
           OR length(idempotency_key) BETWEEN 1 AND 200),
    CHECK (request_fingerprint IS NULL
           OR (length(request_fingerprint) = 64
               AND request_fingerprint NOT GLOB '*[^0-9a-f]*')),
    CHECK (
        (to_status = 'expired'
         AND idempotency_key IS NULL
         AND request_fingerprint IS NULL)
        OR
        (to_status IN ('approved','rejected','cancelled')
         AND idempotency_key IS NOT NULL
         AND request_fingerprint IS NOT NULL)
    )
);

CREATE TRIGGER requests_insert_pending
BEFORE INSERT ON requests
WHEN NEW.status <> 'pending'
BEGIN
    SELECT RAISE(ABORT, 'new requests must be pending');
END;

CREATE TRIGGER requests_no_replace
BEFORE INSERT ON requests
WHEN EXISTS (SELECT 1 FROM requests WHERE id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'requests cannot be replaced');
END;

CREATE TRIGGER requests_no_delete
BEFORE DELETE ON requests
BEGIN
    SELECT RAISE(ABORT, 'requests cannot be deleted');
END;

CREATE TRIGGER requests_immutable_fields
BEFORE UPDATE OF id, workflow_id, dedupe_key, action_json, action_digest,
                 summary, requested_by, created_at_ms, expires_at_ms
ON requests
BEGIN
    SELECT RAISE(ABORT, 'immutable request field');
END;

CREATE TRIGGER requests_status_transition
BEFORE UPDATE OF status ON requests
WHEN OLD.status <> 'pending'
   OR NEW.status NOT IN ('approved','rejected','expired','cancelled')
   OR NEW.version <> OLD.version + 1
BEGIN
    SELECT RAISE(ABORT, 'invalid request status transition');
END;

CREATE TRIGGER requests_version_requires_status
BEFORE UPDATE OF version ON requests
WHEN NEW.status = OLD.status
BEGIN
    SELECT RAISE(ABORT, 'version changes only with a status transition');
END;

CREATE TRIGGER transitions_no_update
BEFORE UPDATE ON transitions
BEGIN
    SELECT RAISE(ABORT, 'transitions is append-only');
END;

CREATE TRIGGER transitions_no_delete
BEFORE DELETE ON transitions
BEGIN
    SELECT RAISE(ABORT, 'transitions is append-only');
END;

CREATE TRIGGER transitions_validate_insert
BEFORE INSERT ON transitions
BEGIN
    SELECT RAISE(ABORT, 'transition already exists')
    WHERE EXISTS (
        SELECT 1 FROM transitions WHERE request_id = NEW.request_id
    );

    SELECT RAISE(ABORT, 'only pending requests can transition')
    WHERE NEW.from_status <> 'pending';

    SELECT RAISE(ABORT, 'invalid terminal status')
    WHERE NEW.to_status NOT IN ('approved','rejected','expired','cancelled');

    SELECT RAISE(ABORT, 'request status must match transition')
    WHERE NOT EXISTS (
        SELECT 1 FROM requests
        WHERE id = NEW.request_id AND status = NEW.to_status
    );

    SELECT RAISE(ABORT, 'user transition at or after deadline')
    WHERE NEW.to_status IN ('approved','rejected','cancelled')
      AND NEW.at_ms >= (
          SELECT expires_at_ms FROM requests WHERE id = NEW.request_id
      );

    SELECT RAISE(ABORT, 'expiry before deadline')
    WHERE NEW.to_status = 'expired'
      AND NEW.at_ms < (
          SELECT expires_at_ms FROM requests WHERE id = NEW.request_id
      );

    SELECT RAISE(ABORT, 'expiry must be system-generated')
    WHERE NEW.to_status = 'expired'
      AND (NEW.actor <> 'system:expiry'
           OR NEW.idempotency_key IS NOT NULL
           OR NEW.request_fingerprint IS NOT NULL);

    SELECT RAISE(ABORT, 'user transition needs key and fingerprint')
    WHERE NEW.to_status IN ('approved','rejected','cancelled')
      AND (NEW.idempotency_key IS NULL OR NEW.request_fingerprint IS NULL);

    SELECT RAISE(ABORT, 'invalid actor for user transition')
    WHERE NEW.to_status IN ('approved','rejected','cancelled')
      AND NEW.actor = 'system:expiry';
END;
