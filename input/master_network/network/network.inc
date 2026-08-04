-- ============================================================================
-- network.inc -- ILLUSTRATIVE shared network block for the master model.
--
-- The actual executable demo does not parse this file. Its shared-network
-- pressure model is defined in ../simspec.json and includes trunk losses from
-- total network flow (prescribed external profiles + both simulated slaves),
-- branch losses from each slave-well rate, and hydrostatic head.
--
-- The keyword records below are intentionally only a structural sketch. They
-- have NOT been validated with Eclipse or OPM Flow and must be replaced by the
-- NETWORK/NODE/BRANCH definitions from the real master deck and simulator
-- version before a licensed run.
-- ============================================================================
NETWORK
NODEPROP
--  NAME       P_MIN   CAP     ...  (replace with simulator-valid records)
  'MANIFOLD'  40.0    1.0E12  0.0 0.0 0.0 0.0 0.0 0.0 0.0 /
/
-- BRANPROP / NODE / BRANCH / well-node connections belong here.
/
