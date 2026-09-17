CREATE TABLE IF NOT EXISTS test_applications (
    user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    research_direction TEXT NOT NULL CHECK (research_direction IN ('model_plant','model_animal','clinical_pathology','other')),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
    share_research_data BOOLEAN NOT NULL DEFAULT FALSE,
    consent_version TEXT NOT NULL,
    consent_updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at TIMESTAMPTZ,
    reviewed_by TEXT REFERENCES users(user_id)
);
CREATE INDEX IF NOT EXISTS test_applications_review_idx ON test_applications(status, created_at);
ALTER TABLE registration_mail_jobs DROP CONSTRAINT IF EXISTS registration_mail_jobs_purpose_check;
ALTER TABLE registration_mail_jobs ADD CONSTRAINT registration_mail_jobs_purpose_check
    CHECK (purpose IN ('email_verify','email_change','test_application','test_decision'));
