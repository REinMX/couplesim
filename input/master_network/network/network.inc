-- ============================================================================
-- network.inc -- ILLUSTRATIVE subsea network block for the master model.
--
-- Placeholder for the real network section of your master deck. Example
-- record below is a NODEPROP sketch only: the full NETWORK section
-- (nodes, branches, well-to-node connections, choke models) must come from
-- your actual setup. The dummy simulator does not parse this file; it
-- solves a simplified network from simspec.json (manifold pressure +
-- riser friction + hydrostatics).
-- ============================================================================
NETWORK
NODEPROP
--  NAME       P_MIN   CAP   DENSITY  ...   (check the Eclipse manual items)
  'MANIFOLD'  40.0    1.0E12  0.0  0.0  0.0  0.0  0.0  0.0  0.0 /
/
-- BRANPROP / NODE / BRANCH ...  (from your real network deck)
/
