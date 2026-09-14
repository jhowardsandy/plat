-- NOTE: views are dropped before creation on purpose. CREATE OR REPLACE VIEW
-- refuses to change a view's column list and leaves the STALE view installed,
-- which looks exactly like the new column silently not existing.

-- The derived signals live HERE, not in any renderer.
-- Grafana, a TUI and a future web archive are all thin clients over these.

DROP VIEW IF EXISTS v_live_lots CASCADE;
CREATE VIEW v_live_lots AS
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
  -- A flat threshold is wrong in both directions: a 95-minute indexing run is
  -- healthy while a 12-minute review is probably wedged. So it is relative to
  -- how long THIS phase usually takes for THIS repo, with a floor so a repo
  -- with no history still gets a signal.
  hist.median_s                                   AS typical_s,
  (l.heartbeat_at IS NOT NULL
   AND l.state NOT IN ('DONE','BLOCKED','PENDING','ABORTED')
   AND (now() - l.heartbeat_at) > greatest(
         interval '10 minutes',
         make_interval(secs => coalesce(hist.median_s, 0) * 3)))  AS stale,
  l.attempt >= 2                                  AS retrying,
  l.attempt >= 3                                  AS last_chance,
  l.state = 'BLOCKED'                             AS needs_human,
  (SELECT coalesce(sum(cost_usd), 0) FROM attempts WHERE lot_id = l.id) AS cost,
  -- true when ANY attempt's cost was estimated rather than reported
  (SELECT bool_or(cost_estimated) FROM attempts WHERE lot_id = l.id) AS cost_estimated,
  (SELECT coalesce(sum(permission_denials), 0) FROM attempts
     WHERE lot_id = l.id)                         AS permission_denials
FROM lots l
JOIN plats p ON p.id = l.plat_id
LEFT JOIN LATERAL (
  SELECT * FROM attempts
  WHERE lot_id = l.id AND finished_at IS NULL
  ORDER BY started_at DESC LIMIT 1
) cur ON true
LEFT JOIN LATERAL (
  SELECT percentile_cont(0.5) WITHIN GROUP (
           ORDER BY EXTRACT(EPOCH FROM (a.finished_at - a.started_at))) AS median_s
  FROM attempts a
  JOIN lots lh ON lh.id = a.lot_id
  WHERE lh.repo = l.repo AND a.phase = cur.phase
    AND a.finished_at IS NOT NULL AND a.provider <> 'supervisor'
) hist ON true
WHERE p.status NOT IN ('closed', 'aborted') AND p.kind = 'plat';

DROP VIEW IF EXISTS v_plat_summary CASCADE;
CREATE VIEW v_plat_summary AS
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
DROP VIEW IF EXISTS v_decision_tree CASCADE;
CREATE VIEW v_decision_tree AS
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
-- a work-tracking ledger wants: Tickets · Summary · Status · Started · Closed.
DROP VIEW IF EXISTS v_delivered_plats CASCADE;
CREATE VIEW v_delivered_plats AS
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
  -- The warning condition, as far as Plat can see it: a roster ticket that has
  -- never been delivered as a plat of its own. Plat cannot see your issue
  -- tracker's status; this is only the local half of that check.
  EXISTS (
    SELECT 1 FROM unnest(p.tickets) tk
    WHERE NOT EXISTS (
      SELECT 1 FROM plats q WHERE q.anchor = tk AND q.closed_at IS NOT NULL)
  ) AS roster_gap
FROM plats p LEFT JOIN lots l ON l.plat_id = p.id
WHERE p.closed_at IS NOT NULL AND p.kind = 'plat'
GROUP BY p.id;

-- Standalone reviews of branches someone already wrote. The cheapest way to get
-- cross-model review: no plan, no worktree, no coder.
DROP VIEW IF EXISTS v_reviews CASCADE;
CREATE VIEW v_reviews AS
SELECT
  p.anchor, p.title AS branch, l.repo,
  p.started_at,
  a.provider || '/' || a.model                       AS reviewer,
  v.verdict,
  (SELECT count(*) FROM findings f WHERE f.attempt_id = a.id)                    AS findings,
  (SELECT count(*) FROM findings f WHERE f.attempt_id = a.id
     AND f.severity = 'high')                                                    AS high,
  a.cost_usd, a.cost_estimated,
  round(EXTRACT(EPOCH FROM (a.finished_at - a.started_at))::numeric, 0)          AS secs
FROM plats p
JOIN lots l ON l.plat_id = p.id
JOIN attempts a ON a.lot_id = l.id
LEFT JOIN verdicts v ON v.attempt_id = a.id
WHERE p.kind = 'review'
ORDER BY p.id DESC;
