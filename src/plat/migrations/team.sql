
CREATE TABLE team_members (
	id SERIAL NOT NULL, 
	name VARCHAR(128) NOT NULL, 
	token_hash VARCHAR(64) NOT NULL, 
	role VARCHAR(16) NOT NULL, 
	active BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	public_id VARCHAR(36) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (name), 
	UNIQUE (token_hash), 
	UNIQUE (public_id)
)

;
