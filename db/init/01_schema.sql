-- Schema mirroring the 8 JSON data files used by the entitlements, identities,
-- new-joiners, peer-affinity, policy, sod-test, and requests services.
-- Column names/types match the JSON fields directly.

CREATE TABLE entitlement_catalog (
    entitlement_id   TEXT PRIMARY KEY,
    entitlement_name TEXT NOT NULL UNIQUE,
    application      TEXT NOT NULL,
    owner            TEXT NOT NULL
);

CREATE TABLE entitlement_risk_scores (
    entitlement_name TEXT PRIMARY KEY REFERENCES entitlement_catalog (entitlement_name),
    application      TEXT NOT NULL,
    risk_score       INTEGER NOT NULL CHECK (risk_score BETWEEN 0 AND 100),
    risk_category    TEXT NOT NULL CHECK (risk_category IN ('Low', 'Medium', 'High', 'Critical'))
);

CREATE TABLE identities (
    employee_id  TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    department   TEXT NOT NULL,
    job_role     TEXT NOT NULL,
    job_level    TEXT NOT NULL,
    location     TEXT NOT NULL,
    manager_id   TEXT NOT NULL DEFAULT '',
    entitlements TEXT NOT NULL DEFAULT ''  -- semicolon-separated entitlement names, as in the JSON
);

CREATE TABLE new_joiners (
    employee_id TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    department  TEXT NOT NULL,
    job_role    TEXT NOT NULL,
    job_level   TEXT NOT NULL,
    location    TEXT NOT NULL,
    manager_id  TEXT NOT NULL,
    cost_center TEXT NOT NULL,
    start_date  DATE NOT NULL
);

CREATE TABLE peer_affinity_scores (
    id              SERIAL PRIMARY KEY,
    job_role        TEXT NOT NULL,
    department      TEXT NOT NULL,
    entitlement     TEXT NOT NULL REFERENCES entitlement_catalog (entitlement_name),
    peer_count      INTEGER NOT NULL CHECK (peer_count >= 0),
    total_peers     INTEGER NOT NULL CHECK (total_peers >= 1),
    affinity_score  INTEGER NOT NULL CHECK (affinity_score BETWEEN 0 AND 100),
    UNIQUE (job_role, entitlement)
);

CREATE TABLE policy_rules (
    policy_id   TEXT PRIMARY KEY,
    policy_name TEXT NOT NULL,
    type        TEXT NOT NULL CHECK (type IN ('ALLOW', 'DENY', 'HUMAN_APPROVAL')),
    rule        TEXT NOT NULL
);

CREATE TABLE sod_rules (
    sod_id        TEXT PRIMARY KEY,
    entitlement_1 TEXT NOT NULL REFERENCES entitlement_catalog (entitlement_name),
    entitlement_2 TEXT NOT NULL REFERENCES entitlement_catalog (entitlement_name),
    severity      TEXT NOT NULL CHECK (severity IN ('Low', 'Medium', 'High', 'Critical')),
    CHECK (entitlement_1 <> entitlement_2)
);

-- Mirrors requests/api/main.py's AccessRequest model field-for-field.
-- Empty-string defaults match the JSON's "" placeholders for not-yet-set fields.
CREATE TABLE access_requests (
    request_id        TEXT PRIMARY KEY,
    requester_id       TEXT NOT NULL DEFAULT '',
    requester_type     TEXT NOT NULL CHECK (requester_type IN ('EMPLOYEE', 'HR')),
    subject_id          TEXT NOT NULL DEFAULT '',
    subject_type        TEXT NOT NULL CHECK (subject_type IN ('IDENTITY', 'NEW_JOINER')),
    entitlement_id       TEXT NOT NULL DEFAULT '',
    entitlement_name     TEXT NOT NULL DEFAULT '',
    application          TEXT NOT NULL DEFAULT '',
    risk_score           INTEGER,
    risk_category        TEXT NOT NULL DEFAULT '',
    approval_required    BOOLEAN NOT NULL DEFAULT FALSE,
    policy_basis         TEXT NOT NULL DEFAULT '',
    sod_conflicts        TEXT NOT NULL DEFAULT '',
    approver_id          TEXT NOT NULL DEFAULT '',
    status               TEXT NOT NULL CHECK (status IN (
        'AUTO_GRANTED', 'PENDING_APPROVAL', 'APPROVED', 'REJECTED',
        'GRANTED', 'BLOCKED_NO_APPROVER', 'PROVISIONING_FAILED'
    )),
    justification        TEXT NOT NULL DEFAULT '',
    decision_note        TEXT NOT NULL DEFAULT '',
    created_at           TEXT NOT NULL DEFAULT '',
    decided_at           TEXT NOT NULL DEFAULT '',
    granted_at           TEXT NOT NULL DEFAULT ''
);
