
CREATE TABLE IF NOT EXISTS plats (
	id SERIAL NOT NULL, 
	anchor VARCHAR(160) NOT NULL, 
	tickets VARCHAR[] NOT NULL, 
	related VARCHAR[] NOT NULL, 
	title TEXT NOT NULL, 
	kind VARCHAR(8) NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	budget_usd FLOAT NOT NULL, 
	origin_id VARCHAR(64) NOT NULL, 
	ticket_status VARCHAR(64), 
	ticket_summary TEXT, 
	ticket_url TEXT, 
	ticket_checked_at TIMESTAMP WITH TIME ZONE, 
	started_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	delivered_at TIMESTAMP WITH TIME ZONE, 
	closed_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id)
)

;
CREATE INDEX IF NOT EXISTS ix_plats_anchor ON plats (anchor);
CREATE INDEX IF NOT EXISTS ix_plats_kind ON plats (kind);

CREATE TABLE IF NOT EXISTS criteria (
	id SERIAL NOT NULL, 
	plat_id INTEGER NOT NULL, 
	lot_key VARCHAR(64), 
	ac_id VARCHAR(16) NOT NULL, 
	statement TEXT NOT NULL, 
	scope VARCHAR(8) DEFAULT 'lot' NOT NULL, 
	verify_in VARCHAR(64), 
	verify_cmd TEXT, 
	met_in_attempt_id INTEGER, 
	PRIMARY KEY (id), 
	FOREIGN KEY(plat_id) REFERENCES plats (id)
)

;
CREATE INDEX IF NOT EXISTS ix_criteria_plat_id ON criteria (plat_id);

CREATE TABLE IF NOT EXISTS lots (
	id SERIAL NOT NULL, 
	plat_id INTEGER NOT NULL, 
	key VARCHAR(64) NOT NULL, 
	repo VARCHAR(128) NOT NULL, 
	worktree_path TEXT NOT NULL, 
	state VARCHAR(16) NOT NULL, 
	attempt INTEGER NOT NULL, 
	depends_on VARCHAR[] NOT NULL, 
	lock_token VARCHAR(64), 
	heartbeat_at TIMESTAMP WITH TIME ZONE, 
	human_note TEXT, 
	base_ref VARCHAR(64), 
	config JSONB NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(plat_id) REFERENCES plats (id)
)

;
CREATE INDEX IF NOT EXISTS ix_lots_state ON lots (state);
CREATE INDEX IF NOT EXISTS ix_lots_plat_id ON lots (plat_id);

CREATE TABLE IF NOT EXISTS attempts (
	id SERIAL NOT NULL, 
	lot_id INTEGER NOT NULL, 
	phase VARCHAR(16) NOT NULL, 
	n INTEGER NOT NULL, 
	provider VARCHAR(16) NOT NULL, 
	model VARCHAR(64) NOT NULL, 
	role_binding JSONB NOT NULL, 
	exit_code INTEGER, 
	cost_usd FLOAT NOT NULL, 
	tokens_in INTEGER NOT NULL, 
	tokens_out INTEGER NOT NULL, 
	cost_estimated BOOLEAN NOT NULL, 
	permission_denials INTEGER NOT NULL, 
	pid INTEGER, 
	started_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	finished_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(lot_id) REFERENCES lots (id)
)

;
CREATE INDEX IF NOT EXISTS ix_attempts_lot_id ON attempts (lot_id);

CREATE TABLE IF NOT EXISTS events (
	id SERIAL NOT NULL, 
	plat_id INTEGER NOT NULL, 
	lot_id INTEGER, 
	kind VARCHAR(32) NOT NULL, 
	message TEXT NOT NULL, 
	payload JSONB NOT NULL, 
	ts TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(plat_id) REFERENCES plats (id), 
	FOREIGN KEY(lot_id) REFERENCES lots (id)
)

;
CREATE INDEX IF NOT EXISTS ix_events_plat_id ON events (plat_id);

CREATE TABLE IF NOT EXISTS artifacts (
	id SERIAL NOT NULL, 
	plat_id INTEGER NOT NULL, 
	attempt_id INTEGER, 
	kind VARCHAR(24) NOT NULL, 
	name VARCHAR(128) NOT NULL, 
	body TEXT NOT NULL, 
	sha256 VARCHAR(64) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(plat_id) REFERENCES plats (id), 
	FOREIGN KEY(attempt_id) REFERENCES attempts (id)
)

;
CREATE INDEX IF NOT EXISTS ix_artifacts_plat_id ON artifacts (plat_id);

CREATE TABLE IF NOT EXISTS decisions (
	id SERIAL NOT NULL, 
	plat_id INTEGER NOT NULL, 
	lot_id INTEGER, 
	attempt_id INTEGER, 
	parent_id INTEGER, 
	actor VARCHAR(8) NOT NULL, 
	actor_detail VARCHAR(64) NOT NULL, 
	kind VARCHAR(24) NOT NULL, 
	decision TEXT NOT NULL, 
	alternatives JSONB NOT NULL, 
	rationale TEXT NOT NULL, 
	inputs JSONB NOT NULL, 
	reversible BOOLEAN NOT NULL, 
	ts TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(plat_id) REFERENCES plats (id), 
	FOREIGN KEY(lot_id) REFERENCES lots (id), 
	FOREIGN KEY(attempt_id) REFERENCES attempts (id), 
	FOREIGN KEY(parent_id) REFERENCES decisions (id)
)

;
CREATE INDEX IF NOT EXISTS ix_decisions_plat_id ON decisions (plat_id);
CREATE INDEX IF NOT EXISTS ix_decisions_lot_id ON decisions (lot_id);

CREATE TABLE IF NOT EXISTS findings (
	id SERIAL NOT NULL, 
	attempt_id INTEGER NOT NULL, 
	origin_id VARCHAR(64) NOT NULL, 
	anchor VARCHAR(160) NOT NULL, 
	repo VARCHAR(128) NOT NULL, 
	fingerprint VARCHAR(64) NOT NULL, 
	severity VARCHAR(16) NOT NULL, 
	file TEXT NOT NULL, 
	line INTEGER, 
	claim TEXT NOT NULL, 
	required_fix TEXT NOT NULL, 
	resolved_in_attempt_id INTEGER, 
	dismissed_reason TEXT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(attempt_id) REFERENCES attempts (id)
)

;
CREATE INDEX IF NOT EXISTS ix_findings_identity ON findings (origin_id, anchor, repo, fingerprint);
CREATE INDEX IF NOT EXISTS ix_findings_fingerprint ON findings (fingerprint);
CREATE INDEX IF NOT EXISTS ix_findings_attempt_id ON findings (attempt_id);

CREATE TABLE IF NOT EXISTS log_chunks (
	id SERIAL NOT NULL, 
	attempt_id INTEGER NOT NULL, 
	seq INTEGER NOT NULL, 
	body TEXT NOT NULL, 
	ts TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(attempt_id) REFERENCES attempts (id)
)

;
CREATE INDEX IF NOT EXISTS ix_log_chunks_attempt_id ON log_chunks (attempt_id);

CREATE TABLE IF NOT EXISTS sessions (
	id SERIAL NOT NULL, 
	attempt_id INTEGER NOT NULL, 
	provider VARCHAR(16) NOT NULL, 
	provider_session_id VARCHAR(128) NOT NULL, 
	resumable_until TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(attempt_id) REFERENCES attempts (id)
)

;
CREATE INDEX IF NOT EXISTS ix_sessions_attempt_id ON sessions (attempt_id);

CREATE TABLE IF NOT EXISTS transcripts (
	id SERIAL NOT NULL, 
	attempt_id INTEGER NOT NULL, 
	body TEXT NOT NULL, 
	redacted BOOLEAN NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(attempt_id) REFERENCES attempts (id)
)

;
CREATE INDEX IF NOT EXISTS ix_transcripts_attempt_id ON transcripts (attempt_id);

CREATE TABLE IF NOT EXISTS verdicts (
	id SERIAL NOT NULL, 
	attempt_id INTEGER NOT NULL, 
	raw JSONB NOT NULL, 
	status VARCHAR(24), 
	verdict VARCHAR(24), 
	tests_passed INTEGER NOT NULL, 
	tests_failed INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(attempt_id) REFERENCES attempts (id)
)

;
CREATE INDEX IF NOT EXISTS ix_verdicts_attempt_id ON verdicts (attempt_id);
