GSATPROD
-- Optional static production profiles for model_hdn subsea wells, kept as
-- the "as today" reference. Used ONLY when model_hdn is set to
-- mode=static in coupling.json (then bin/run_coupled.py copies this file
-- into the realization runpath as coupling/gsatprod_model_hdn.inc).
-- In the default coupled mode, master_network rewrites coupling/gsatprod_model_hdn.inc
-- at every coupling iteration instead.
-- Format: WELL YEAR Q_LIQ_SM3D Q_GAS_SM3D P_WH_BAR P_BHP_BAR GSAT
--  WELL      YEAR  Q_LIQ_SM3D  Q_GAS_SM3D  P_WH_BAR  P_BHP_BAR  GSAT
    'H-P1'    2024  180.0       21600.0     41.2      282.5      0.31 /
    'H-P1'    2025  170.0       20400.0     41.0      282.2      0.30 /
    'H-P1'    2026  160.0       19200.0     40.9      281.9      0.30 /
    'H-P2'    2024  220.0       26400.0     41.8      283.4      0.33 /
    'H-P2'    2025  205.0       24600.0     41.6      283.0      0.32 /
    'H-P2'    2026  190.0       22800.0     41.4      282.7      0.31 /
/
