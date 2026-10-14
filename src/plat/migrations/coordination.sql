
CREATE TABLE command_receipts (
	key VARCHAR(128) NOT NULL, 
	actor VARCHAR(128) NOT NULL, 
	request_hash VARCHAR(64) NOT NULL, 
	result JSONB NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	public_id VARCHAR(36) NOT NULL, 
	PRIMARY KEY (key), 
	UNIQUE (public_id)
)

;
ALTER TABLE plats ADD COLUMN public_id varchar(36) NOT NULL DEFAULT gen_random_uuid()::text;
ALTER TABLE plats ADD CONSTRAINT uq_plats_public_id UNIQUE(public_id);
ALTER TABLE criteria ADD COLUMN public_id varchar(36) NOT NULL DEFAULT gen_random_uuid()::text;
ALTER TABLE criteria ADD CONSTRAINT uq_criteria_public_id UNIQUE(public_id);
ALTER TABLE lots ADD COLUMN public_id varchar(36) NOT NULL DEFAULT gen_random_uuid()::text;
ALTER TABLE lots ADD CONSTRAINT uq_lots_public_id UNIQUE(public_id);

CREATE TABLE stage_records (
	id SERIAL NOT NULL, 
	plat_id INTEGER NOT NULL, 
	stage VARCHAR(24) NOT NULL, 
	version INTEGER NOT NULL, 
	content JSONB NOT NULL, 
	sha256 VARCHAR(64) NOT NULL, 
	actor VARCHAR(128) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	public_id VARCHAR(36) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(plat_id) REFERENCES plats (id), 
	UNIQUE (public_id)
)

;
CREATE INDEX ix_stage_records_plat_id ON stage_records (plat_id);

CREATE TABLE workflows (
	plat_id INTEGER NOT NULL, 
	stage VARCHAR(24) NOT NULL, 
	version INTEGER NOT NULL, 
	spec_hash VARCHAR(64), 
	owner VARCHAR(128) NOT NULL, 
	requirements JSONB NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	public_id VARCHAR(36) NOT NULL, 
	PRIMARY KEY (plat_id), 
	FOREIGN KEY(plat_id) REFERENCES plats (id), 
	UNIQUE (public_id)
)

;
ALTER TABLE attempts ADD COLUMN public_id varchar(36) NOT NULL DEFAULT gen_random_uuid()::text;
ALTER TABLE attempts ADD CONSTRAINT uq_attempts_public_id UNIQUE(public_id);

CREATE TABLE collaboration_items (
	id SERIAL NOT NULL, 
	plat_id INTEGER NOT NULL, 
	lot_id INTEGER, 
	kind VARCHAR(16) NOT NULL, 
	owner VARCHAR(128) NOT NULL, 
	author VARCHAR(128) NOT NULL, 
	state VARCHAR(16) NOT NULL, 
	version INTEGER NOT NULL, 
	body JSONB NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	public_id VARCHAR(36) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(plat_id) REFERENCES plats (id), 
	FOREIGN KEY(lot_id) REFERENCES lots (id), 
	UNIQUE (public_id)
)

;
CREATE INDEX ix_collaboration_items_plat_id ON collaboration_items (plat_id);
ALTER TABLE events ADD COLUMN public_id varchar(36) NOT NULL DEFAULT gen_random_uuid()::text;
ALTER TABLE events ADD CONSTRAINT uq_events_public_id UNIQUE(public_id);

CREATE TABLE execution_claims (
	lot_id INTEGER NOT NULL, 
	resource TEXT NOT NULL, 
	executor_id VARCHAR(64) NOT NULL, 
	host_id VARCHAR(64) NOT NULL, 
	token VARCHAR(36) NOT NULL, 
	generation INTEGER NOT NULL, 
	active BOOLEAN NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	public_id VARCHAR(36) NOT NULL, 
	PRIMARY KEY (lot_id), 
	FOREIGN KEY(lot_id) REFERENCES lots (id), 
	UNIQUE (public_id)
)

;
CREATE UNIQUE INDEX uq_active_resource ON execution_claims (resource) WHERE active IS true;
ALTER TABLE artifacts ADD COLUMN public_id varchar(36) NOT NULL DEFAULT gen_random_uuid()::text;
ALTER TABLE artifacts ADD CONSTRAINT uq_artifacts_public_id UNIQUE(public_id);
ALTER TABLE decisions ADD COLUMN public_id varchar(36) NOT NULL DEFAULT gen_random_uuid()::text;
ALTER TABLE decisions ADD CONSTRAINT uq_decisions_public_id UNIQUE(public_id);
ALTER TABLE findings ADD COLUMN public_id varchar(36) NOT NULL DEFAULT gen_random_uuid()::text;
ALTER TABLE findings ADD CONSTRAINT uq_findings_public_id UNIQUE(public_id);
ALTER TABLE log_chunks ADD COLUMN public_id varchar(36) NOT NULL DEFAULT gen_random_uuid()::text;
ALTER TABLE log_chunks ADD CONSTRAINT uq_log_chunks_public_id UNIQUE(public_id);
ALTER TABLE sessions ADD COLUMN public_id varchar(36) NOT NULL DEFAULT gen_random_uuid()::text;
ALTER TABLE sessions ADD CONSTRAINT uq_sessions_public_id UNIQUE(public_id);

CREATE TABLE tracked_sessions (
	id SERIAL NOT NULL, 
	plat_id INTEGER NOT NULL, 
	lot_id INTEGER NOT NULL, 
	attempt_id INTEGER, 
	installation VARCHAR(64) NOT NULL, 
	host_id VARCHAR(64) NOT NULL, 
	owner VARCHAR(128) NOT NULL, 
	provider VARCHAR(16) NOT NULL, 
	session_key VARCHAR(128) NOT NULL, 
	provider_session_id VARCHAR(128), 
	source VARCHAR(16) NOT NULL, 
	state VARCHAR(16) NOT NULL, 
	capabilities JSONB NOT NULL, 
	last_activity TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	public_id VARCHAR(36) NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_session_identity UNIQUE (installation, provider, session_key), 
	FOREIGN KEY(plat_id) REFERENCES plats (id), 
	FOREIGN KEY(lot_id) REFERENCES lots (id), 
	FOREIGN KEY(attempt_id) REFERENCES attempts (id), 
	UNIQUE (public_id)
)

;
CREATE INDEX ix_tracked_sessions_lot_id ON tracked_sessions (lot_id);
CREATE INDEX ix_tracked_sessions_plat_id ON tracked_sessions (plat_id);
ALTER TABLE transcripts ADD COLUMN public_id varchar(36) NOT NULL DEFAULT gen_random_uuid()::text;
ALTER TABLE transcripts ADD CONSTRAINT uq_transcripts_public_id UNIQUE(public_id);
ALTER TABLE verdicts ADD COLUMN public_id varchar(36) NOT NULL DEFAULT gen_random_uuid()::text;
ALTER TABLE verdicts ADD CONSTRAINT uq_verdicts_public_id UNIQUE(public_id);
ALTER TABLE attempts ADD COLUMN executor_id varchar(64);
ALTER TABLE attempts ADD COLUMN host_id varchar(64);
ALTER TABLE attempts ADD COLUMN claim_generation integer;
ALTER TABLE lots ADD CONSTRAINT uq_lot_key UNIQUE(plat_id, key);
CREATE UNIQUE INDEX uq_plat_anchor ON plats(origin_id, anchor) WHERE kind = 'plat';
