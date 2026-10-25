"""Hardening: durable screening records, job leases, and a database publication guard.

Revision ID: 0002_hardening
Revises: 0001_naive

The guard does not contain the moderation policy (that is app/policy.py). It enforces that nothing
becomes `published` without a stored result covering every frame whose decision is `published`, or
an admin approval of a `held` result; that terminal states stay terminal; and that results and
review records are append-only.
"""

from alembic import op

revision = "0002_hardening"
down_revision = "0001_naive"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        -- Existing rows from the naive system: map job vocabulary; in-flight work is re-screened.
        UPDATE screening_jobs SET status = 'queued' WHERE status IN ('processing', 'waiting');
        UPDATE screening_jobs SET status = 'completed' WHERE status = 'done';

        ALTER TABLE media
          ADD COLUMN hold_reason TEXT
              CHECK (hold_reason IN ('needs_human_review', 'provider_unavailable', 'screening_failed')),
          ADD COLUMN frame_count INT GENERATED ALWAYS AS (jsonb_array_length(frames)) STORED;
        UPDATE media SET hold_reason = 'needs_human_review' WHERE status = 'held';
        ALTER TABLE media
          ADD CONSTRAINT chk_status CHECK (status IN ('pending', 'published', 'held', 'blocked')),
          ADD CONSTRAINT chk_type CHECK (type IN ('image', 'video')),
          ADD CONSTRAINT chk_source CHECK (source IN ('upload', 'generation')),
          ADD CONSTRAINT chk_frames_array CHECK (jsonb_typeof(frames) = 'array' AND jsonb_array_length(frames) >= 1),
          ADD CONSTRAINT chk_image_one_frame CHECK (type <> 'image' OR jsonb_array_length(frames) = 1),
          ADD CONSTRAINT chk_generation_prompt CHECK (source <> 'generation' OR prompt IS NOT NULL),
          ADD CONSTRAINT chk_held_reason CHECK ((status = 'held') = (hold_reason IS NOT NULL)),
          ADD CONSTRAINT fk_media_user_ws FOREIGN KEY (user_id, workspace_id) REFERENCES users (id, workspace_id),
          ADD CONSTRAINT uq_media_id_ws UNIQUE (id, workspace_id);

        ALTER TABLE screening_jobs
          ADD COLUMN cycle INT NOT NULL DEFAULT 1,
          ADD COLUMN cycle_failures INT NOT NULL DEFAULT 0,
          ADD COLUMN cycle_rate_limited INT NOT NULL DEFAULT 0,
          ADD COLUMN malformed_total INT NOT NULL DEFAULT 0,
          ADD COLUMN next_retry_at TIMESTAMPTZ,
          ADD COLUMN lease_token UUID,
          ADD COLUMN lease_expires TIMESTAMPTZ,
          ADD COLUMN callback_deadline TIMESTAMPTZ,
          ADD COLUMN last_error_class TEXT,
          ADD CONSTRAINT chk_job_status CHECK (status IN
              ('queued', 'claimed', 'waiting_callback', 'held_provider', 'completed', 'failed', 'cancelled')),
          ADD CONSTRAINT fk_job_media_ws FOREIGN KEY (media_id, workspace_id) REFERENCES media (id, workspace_id);
        CREATE UNIQUE INDEX uq_one_unresolved_job_per_media ON screening_jobs (media_id)
          WHERE status IN ('queued', 'claimed', 'waiting_callback', 'held_provider');
        CREATE INDEX ix_jobs_claim ON screening_jobs (status, next_retry_at, workspace_id, created_at);
        CREATE INDEX ix_jobs_lease ON screening_jobs (workspace_id, lease_expires) WHERE lease_expires IS NOT NULL;

        CREATE TABLE job_attempts (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            job_id UUID NOT NULL REFERENCES screening_jobs (id),
            attempt_number INT NOT NULL,
            cycle INT NOT NULL,
            lease_token UUID NOT NULL,
            started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at TIMESTAMPTZ,
            outcome TEXT,
            http_status INT,
            latency_ms INT,
            UNIQUE (job_id, attempt_number)
        );

        CREATE TABLE screening_results (
            media_id UUID PRIMARY KEY REFERENCES media (id),
            job_id UUID NOT NULL REFERENCES screening_jobs (id),
            provider_request_id TEXT,
            raw_response JSONB NOT NULL,
            frames_scored INT GENERATED ALWAYS AS (jsonb_array_length(raw_response -> 'frames')) STORED,
            decision TEXT NOT NULL CHECK (decision IN ('published', 'held', 'blocked')),
            reasons JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT chk_result_frames CHECK (jsonb_typeof(raw_response -> 'frames') = 'array')
        );

        CREATE TABLE media_reviews (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            media_id UUID NOT NULL,
            workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            action TEXT NOT NULL CHECK (action IN ('approve', 'reject')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            FOREIGN KEY (media_id, workspace_id) REFERENCES media (id, workspace_id),
            FOREIGN KEY (user_id, workspace_id) REFERENCES users (id, workspace_id)
        );

        CREATE FUNCTION admin_review_exists(p_media UUID, p_workspace TEXT, p_action TEXT) RETURNS BOOLEAN
        LANGUAGE sql STABLE AS $$
            SELECT EXISTS (
                SELECT 1 FROM media_reviews v JOIN users u ON u.id = v.user_id AND u.workspace_id = v.workspace_id
                WHERE v.media_id = p_media AND v.workspace_id = p_workspace AND v.action = p_action
                  AND u.role = 'admin')
        $$;

        CREATE FUNCTION media_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            result_decision TEXT;
            full_coverage BOOLEAN;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'pending' OR NEW.hold_reason IS NOT NULL THEN
                    RAISE EXCEPTION 'media_guard: new media must start pending';
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.status = OLD.status AND NEW.hold_reason IS NOT DISTINCT FROM OLD.hold_reason THEN
                RETURN NEW;
            END IF;
            IF OLD.status IN ('published', 'blocked') THEN
                RAISE EXCEPTION 'media_guard: % is terminal', OLD.status;
            END IF;
            IF NEW.status = 'pending' THEN
                RAISE EXCEPTION 'media_guard: media cannot return to pending';
            END IF;

            -- Generated columns are not available in BEFORE triggers: count frames directly.
            SELECT r.decision, r.frames_scored = jsonb_array_length(NEW.frames)
              INTO result_decision, full_coverage
              FROM screening_results r WHERE r.media_id = NEW.id;
            full_coverage := COALESCE(full_coverage, FALSE);

            IF NEW.status = 'published' THEN
                IF full_coverage AND result_decision = 'published' THEN RETURN NEW; END IF;
                IF full_coverage AND result_decision = 'held'
                   AND admin_review_exists(NEW.id, NEW.workspace_id, 'approve') THEN RETURN NEW; END IF;
                RAISE EXCEPTION 'media_guard: publishing % requires a complete published result or an admin-approved held result', NEW.id;
            ELSIF NEW.status = 'blocked' THEN
                IF full_coverage AND result_decision = 'blocked' THEN RETURN NEW; END IF;
                IF admin_review_exists(NEW.id, NEW.workspace_id, 'reject') THEN RETURN NEW; END IF;
                RAISE EXCEPTION 'media_guard: blocking % requires a complete blocked result or an admin rejection', NEW.id;
            ELSIF NEW.hold_reason = 'needs_human_review' THEN
                IF full_coverage AND result_decision = 'held' THEN RETURN NEW; END IF;
                RAISE EXCEPTION 'media_guard: a human-review hold on % requires a complete held result', NEW.id;
            END IF;
            RETURN NEW;  -- held(provider_unavailable | screening_failed): fail-closed, no result needed
        END $$;
        CREATE TRIGGER media_guard BEFORE INSERT OR UPDATE OF status, hold_reason ON media
            FOR EACH ROW EXECUTE FUNCTION media_guard();

        CREATE FUNCTION append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'append_only: rows in % cannot be changed or deleted', TG_TABLE_NAME;
        END $$;
        CREATE TRIGGER results_append_only BEFORE UPDATE OR DELETE ON screening_results
            FOR EACH ROW EXECUTE FUNCTION append_only();
        CREATE TRIGGER reviews_append_only BEFORE UPDATE OR DELETE ON media_reviews
            FOR EACH ROW EXECUTE FUNCTION append_only();
    """)


def downgrade() -> None:
    op.execute("""
        DROP TRIGGER reviews_append_only ON media_reviews;
        DROP TRIGGER results_append_only ON screening_results;
        DROP FUNCTION append_only();
        DROP TRIGGER media_guard ON media;
        DROP FUNCTION media_guard();
        DROP FUNCTION admin_review_exists(UUID, TEXT, TEXT);
        DROP TABLE media_reviews;
        DROP TABLE screening_results;
        DROP TABLE job_attempts;
        DROP INDEX ix_jobs_lease;
        DROP INDEX ix_jobs_claim;
        DROP INDEX uq_one_unresolved_job_per_media;
        ALTER TABLE screening_jobs DROP CONSTRAINT fk_job_media_ws, DROP CONSTRAINT chk_job_status,
          DROP COLUMN last_error_class, DROP COLUMN callback_deadline, DROP COLUMN lease_expires,
          DROP COLUMN lease_token, DROP COLUMN next_retry_at, DROP COLUMN malformed_total,
          DROP COLUMN cycle_rate_limited, DROP COLUMN cycle_failures, DROP COLUMN cycle;
        ALTER TABLE media DROP CONSTRAINT uq_media_id_ws, DROP CONSTRAINT fk_media_user_ws,
          DROP CONSTRAINT chk_held_reason, DROP CONSTRAINT chk_generation_prompt, DROP CONSTRAINT chk_image_one_frame,
          DROP CONSTRAINT chk_frames_array, DROP CONSTRAINT chk_source, DROP CONSTRAINT chk_type,
          DROP CONSTRAINT chk_status, DROP COLUMN frame_count, DROP COLUMN hold_reason;
    """)
