-- The derived signals live HERE, not in any renderer.
-- Grafana, a TUI and a future web archive are all thin clients over these.

CREATE OR REPLACE VIEW v_live_lots AS
SELECT
  p.id                                            AS plat_id,
  p.anchor                                        AS ticket,
  l.id                                            AS lot_id,
  l.key                                           AS lot,
  l.state,
  cur.phase,
  cur.provider || '/' || cur.model                AS agent,
  l.attempt,
  now() - cur.started_at                          AS elapsed,
  now() - l.heartbeat_at                          AS since_heartbeat,
  (now() - l.heartbeat_at) > interval '10 minutes' AS stale,
  l.attempt >= 2                                  AS retrying,
  l.attempt >= 3                                  AS last_chance,
  l.state = 'BLOCKED'                             AS needs_human,
  (SELECT coalesce(sum(cost_usd), 0) FROM attempts WHERE lot_id = l.id) AS cost
FROM lots l
JOIN plats p ON p.id = l.plat_id
LEFT JOIN LATERAL (
  SELECT * FROM attempts
  WHERE lot_id = l.id AND finished_at IS NULL
  ORDER BY started_at DESC LIMIT 1
) cur ON true
WHERE p.status NOT IN ('closed', 'aborted');

CREATE OR REPLACE VIEW v_plat_summary AS
SELECT
  p.id, p.anchor, p.title, p.status, p.budget_usd, p.started_at,
  count(l.id)                                           AS lots,
  count(*) FILTER (WHERE l.state = 'DONE')              AS lots_done,
  count(*) FILTER (WHERE l.state = 'BLOCKED')           AS lots_blocked,
  count(*) FILTER (WHERE l.state NOT IN ('DONE','PENDING','BLOCKED','ABORTED')) AS lots_active,
  (SELECT coalesce(sum(a.cost_usd), 0) FROM attempts a
     JOIN lots l2 ON l2.id = a.lot_id WHERE l2.plat_id = p.id) AS spent
FROM plats p LEFT JOIN lots l ON l.plat_id = p.id
GROUP BY p.id;

-- The decision tree, flattened with a depth so any renderer can indent it.
-- Read it as narrative, top to bottom; that beats a node graph every time.
CREATE OR REPLACE VIEW v_decision_tree AS
WITH RECURSIVE t AS (
  SELECT d.*, 0 AS depth, ARRAY[d.id] AS path
  FROM decisions d WHERE d.parent_id IS NULL
  UNION ALL
  SELECT c.*, t.depth + 1, t.path || c.id
  FROM decisions c JOIN t ON c.parent_id = t.id
)
SELECT plat_id, lot_id, attempt_id, id, parent_id, depth, path,
       actor, actor_detail, kind, decision, alternatives, rationale,
       inputs, reversible, ts,
       actor = 'system' AS observed   -- vs. self-reported. Render differently.
FROM t ORDER BY path;

-- Live update path: one trigger, no polling, no new infrastructure.
CREATE OR REPLACE FUNCTION plat_notify_event() RETURNS trigger AS $$
BEGIN
  PERFORM pg_notify('plat_events', NEW.id::text);
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_plat_notify_event ON events;
CREATE TRIGGER trg_plat_notify_event AFTER INSERT ON events
  FOR EACH ROW EXECUTE FUNCTION plat_notify_event();

-- Delivered plats. This is also, not coincidentally, the row shape that
-- work-items/INDEX.md wants: Tickets · Summary · Status · Started · Closed.
CREATE OR REPLACE VIEW v_delivered_plats AS
SELECT
  p.anchor, p.title, p.tickets AS roster, p.status,
  p.started_at::date AS started,
  p.closed_at::date  AS closed,
  count(l.id)        AS lots,
  (SELECT count(*) FROM attempts a JOIN lots l2 ON l2.id = a.lot_id
     WHERE l2.plat_id = p.id)                                        AS attempts,
  (SELECT count(*) FROM findings f JOIN attempts a ON a.id = f.attempt_id
     JOIN lots l3 ON l3.id = a.lot_id WHERE l3.plat_id = p.id)       AS findings,
  (SELECT coalesce(sum(a.cost_usd), 0) FROM attempts a
     JOIN lots l4 ON l4.id = a.lot_id WHERE l4.plat_id = p.id)       AS spend,
  -- The ledger's warning condition, as far as Plat can see it: a roster ticket
  -- that has never been delivered as a plat of its own. Plat does not know Jira
  -- status -- /mlg-work-ledger cross-checks that. This is the local half.
  EXISTS (
    SELECT 1 FROM unnest(p.tickets) tk
    WHERE NOT EXISTS (
      SELECT 1 FROM plats q WHERE q.anchor = tk AND q.closed_at IS NOT NULL)
  ) AS roster_gap
FROM plats p LEFT JOIN lots l ON l.plat_id = p.id
WHERE p.closed_at IS NOT NULL
GROUP BY p.id;
