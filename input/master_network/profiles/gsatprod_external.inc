GSATPROD
-- ILLUSTRATIVE prescribed profiles for external subsea wells that are not
-- simulated by model_a or model_b. These rows are inputs TO master_network.
-- The dummy parser uses: WELL YEAR Q_LIQ_SM3D Q_GAS_SM3D P_WH P_BHP GSAT.
-- Replace this illustrative record layout with the exact Eclipse keyword
-- syntax used by the real master deck; the workflow treats the file as an
-- immutable prescribed network source.
--  WELL       YEAR  Q_LIQ_SM3D  Q_GAS_SM3D  P_WH_BAR  P_BHP_BAR  GSAT
    'EXT-P1'   2024  130.0       15600.0     41.0      280.0      0.30 /
    'EXT-P1'   2025  120.0       14400.0     40.9      279.5      0.29 /
    'EXT-P1'   2026  110.0       13200.0     40.8      279.0      0.28 /
    'EXT-P2'   2024   90.0       10800.0     40.7      275.0      0.27 /
    'EXT-P2'   2025   85.0       10200.0     40.6      274.7      0.27 /
    'EXT-P2'   2026   80.0        9600.0     40.5      274.4      0.26 /
/
