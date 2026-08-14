# Guía corregida: acoplamiento bidireccional por realización

Estado: implementación ejecutada y validada con OPM Flow 2026.04 y ERT 23.0.1.

Esta guía sustituye expresamente la guía anterior enviada por Telegram en los
mensajes `1462` y `1463`. La guía anterior describía un flujo secuencial y
unidireccional basado en `GSATPROD`; no representa la arquitectura activa.

## 1. Objetivo

Acoplar los dos ensembles de manera uno-a-uno:

```text
Model A realización N <-> NETWORK <-> Model B realización N
```

Requisitos implementados:

- ERT declara 100 realizaciones.
- Se pueden ejecutar subconjuntos, por ejemplo `0-2`.
- `Model A_N` interactúa solamente con `Model B_N`.
- Model B deja de ser un perfil precalculado.
- `GSATPROD` se elimina de la ruta primaria.
- El network devuelve contrapresión a los dos modelos.
- Model A, Model B y el network se resuelven con OPM Flow real.
- La interacción avanza por periodos de reporte usando restart.

## 2. Arquitectura activa

```text
ERT realización N
  |
  +-- Q0_MULT_MODEL_A[N] --> deck Flow de Model A_N
  +-- Q0_MULT_MODEL_B[N] --> deck Flow de Model B_N
  |
  +-- RUN_COUPLED
      |
      +-- cadena restart Model A_N --+
      |                              |
      +-- cadena restart Model B_N --+--> tasas simuladas actuales
                                     |
                                     v
                           master Flow NETWORK
                                     |
                      BHP para Model A y Model B
                                     |
                          relajar, repetir y converger
                                     |
                          aceptar restart y avanzar
```

El runpath ERT es la frontera de aislamiento:

```text
output/02_coupled_stepwise_verified/realization-N/iter-M/
```

Por diseño, ningún archivo de intercambio cruza entre realizaciones.

## 3. Diferencia respecto al diseño anterior

Diseño anterior, ya obsoleto:

```text
Model B corre hasta el final
  -> se genera GSATPROD
  -> Model A + NETWORK corren con ese perfil fijo
```

Problema: la contrapresión calculada por el network no puede modificar Model B.
Model B no participa en una iteración bidireccional; solamente genera una
condición impuesta.

Diseño corregido:

```text
rates A_N + rates B_N
  -> solve NETWORK
  -> BHP constraints A_N + B_N
  -> rerun current report step
  -> repeat until convergence
```

`GSATPROD` permanece únicamente en:

```text
configs/coupling.legacy-gsatprod.json
```

Ese archivo existe para regresión histórica y comparación. No es el default.

## 4. Algoritmo paso a paso

Para cada realización y periodo de reporte:

1. Model A y Model B reciben las restricciones BHP actuales.
2. Cada modelo ejecuta o continúa su cadena OPM Flow mediante `RESTART`.
3. Se extraen las tasas brutas de los dos modelos.
4. Se relajan las tasas que entran al network:

   ```text
   q_forwarded = q_previous + omega * (q_raw - q_previous)
   ```

5. El master renderiza y ejecuta un deck Flow con el `NETWORK` compartido.
6. El master extrae restricciones separadas:

   ```text
   coupling/network_constraints_model_a.csv
   coupling/network_constraints_model_b.csv
   ```

7. Se evalúa el residual de punto fijo sin relajar:

   ```text
   residual = max(
       abs(q_raw - q_previous) /
       max(abs(q_previous), epsilon)
   )
   ```

8. Si el residual es mayor que `0.005`, se repite el mismo ciclo.
9. Si converge, se acepta el estado restart y se avanza al siguiente periodo.
10. Si no converge dentro de 20 iteraciones, el forward model falla y ERT no
    acepta silenciosamente una realización parcial.

Parámetros activos:

```text
max_iterations = 20
relaxation      = 0.4
tolerance       = 0.005
```

## 5. Incertidumbre y emparejamiento

ERT usa dos variables activas independientes:

```text
Q0_MULT_MODEL_A
Q0_MULT_MODEL_B
```

Cada valor modifica el deck real de su modelo mediante `PERMX`, `PERMY` y
`PERMZ`. No se limita a metadata o a un archivo que el simulador ignore.

El network se mantiene nominal en este experimento de dos ensembles. Se retiró
`NETWORK_CHOKE` de la configuración primaria porque las tablas VFP activas eran
fijas y el parámetro muestreado no cambiaba el solve real. Mantenerlo habría
creado una incertidumbre ficticia.

Ejemplo verificado:

| Realización | Mult. A | PERMX A renderizado, mD | Mult. B |
|---:|---:|---:|---:|
| 0 | 0.924025 | 92.402500 | 0.935934 |
| 1 | 0.939670 | 93.967000 | 1.088120 |
| 2 | 0.928626 | 92.862600 | 1.157760 |

## 6. Configuración ERT

Archivo:

```text
ert/model/02_ensemble_coupled.ert
```

Contrato principal:

```ert
NUM_REALIZATIONS 100
RUNPATH ../../output/02_coupled_stepwise_verified/realization-<IENS>/iter-<ITER>
FORWARD_MODEL RUN_COUPLED
```

Un único `RUN_COUPLED` contiene Model A, Model B y el master de red. No se
seleccionan dos ensembles independientes que pudieran desalinearse.

## 7. Comandos reproducibles

Desde `ert/model`:

```bash
export PATH=/home/javier/projects/ert_fmu/.venv/bin:$PATH

ert lint 02_ensemble_coupled.ert

ert test_run \
  02_ensemble_coupled.ert \
  --disable-monitoring

ert ensemble_experiment \
  02_ensemble_coupled.ert \
  --realizations 0-2 \
  --current-ensemble twoway_selected_0_2 \
  --disable-monitoring
```

ERT puede informar:

```text
MIN_REALIZATIONS was set to the current number of active realizations (3)
```

Eso es esperado para el subconjunto. El archivo continúa declarando 100
realizaciones; solamente se activan tres.

Agregación desde la raíz del repositorio:

```bash
python3 ert/bin/scripts/collect_ensemble.py \
  --case-dir output/02_coupled_stepwise_verified \
  --iter 0
```

## 8. Evidencia real de realizaciones 0-2

El comando `ert ensemble_experiment` terminó con exit code `0`.

Resultados agregados:

| Real | Mult. A | Mult. B | Iteraciones | Residual final | A-P1 2024, sm3/d | B-P1 2024, sm3/d |
|---:|---:|---:|---:|---:|---:|---:|
| 0 | 0.924025 | 0.935934 | 12 | 0.004466082 | 2188.596 | 1293.750 |
| 1 | 0.939670 | 1.088120 | 13 | 0.003276808 | 2195.614 | 1408.235 |
| 2 | 0.928626 | 1.157760 | 13 | 0.003604951 | 2174.041 | 1460.327 |

Verificaciones efectuadas:

- tres runpaths seleccionados;
- tres archivos `OK`;
- convergencia `True` en los tres;
- residual menor que `0.005` en los tres;
- multiplicadores distintos por realización;
- decks Flow renderizados distintos;
- tasas acopladas distintas;
- ningún `network_choke.txt` en los runpaths activos;
- cero ocurrencias de `GSATPROD` o `SAT` en todos los
  `MASTER_FLOW.DATA` activos;
- 266 archivos PRT encontrados;
- los 266 contienen el bloque final `Error summary`;
- cero PRT con `Warnings`, `Errors` o `Problems` mayores que cero.

Archivos agregados:

```text
output/02_coupled_stepwise_verified/ensemble_results.csv
output/02_coupled_stepwise_verified/ensemble_summary.csv
```

## 9. Evidencia por realización

Cada `realization-N/iter-0` contiene:

```text
OK
COUPLED_REPORT.txt
coupling_config.json
coupling/
├── convergence_history.csv
├── slave_rates_model_a.csv
├── slave_rates_model_b.csv
├── network_constraints_model_a.csv
├── network_constraints_model_b.csv
├── flow_model_a/iteration-NNN/year-YYYY/
├── flow_model_b/iteration-NNN/year-YYYY/
└── flow_master/iteration-NNN/master_report.json
```

`COUPLED_REPORT.txt` declara:

```text
prescribed profiles : none (fully two-way)
slave backends      : model_a=flow, model_b=flow
master network pressure constraints -> both slaves
```

`master_report.json` registra:

- tasas solicitadas de Model A;
- tasas solicitadas de Model B;
- `prescribed_profile: {}`;
- tasas entregadas por Flow;
- presión de manifold;
- BHP para cada pozo de ambos modelos.

## 10. Archivos principales de implementación

```text
coupling.json
ert/model/02_ensemble_coupled.ert
ert/bin/jobs/RUN_COUPLED
ert/bin/scripts/run_coupled.py
ert/bin/scripts/collect_ensemble.py
spikes/003-opm-model-n-restart/opm_model_n_restart_adapter.py
spikes/003-opm-model-n-restart/MODEL_BASE.DATA.tmpl
spikes/003-opm-model-n-restart/MODEL_CONTINUE.DATA.tmpl
spikes/004-opm-flow-master/opm_flow_master_adapter.py
spikes/004-opm-flow-master/MASTER_FLOW_TWOWAY.DATA.tmpl
configs/coupling.legacy-gsatprod.json
tests/test_twoway_flow_no_gsatprod.py
```

## 11. Validación de software

Gates ejecutados:

```text
Suite completa: 101 tests OK, 1 skip esperado
Scoped Ruff: All checks passed
Git diff whitespace gate: OK
ERT lint: Found no errors
ERT selected realizations 0-2: exit code 0
ERT test_run final: exit code 0
PRT health: 266/266 con Error summary y todos 0/0/0
```

El test suite completo se ejecuta con:

```bash
python3 -m unittest discover -s tests -v
```

La revisión de Ruff de todo el repositorio incluye spikes históricos y WIP no
relacionados. El gate de este cambio usa la lista explícita de archivos de
acoplamiento indicada en el README para no modificar trabajo ajeno.

## 12. Diferencia frente a standalones2rc

La implementación activa es co-simulación de punto fijo por periodo de reporte.
No es comunicación nativa MPI dentro de cada ministep de Flow.

Referencia nativa:

```text
standalones2rc
  -> SLAVES
  -> GRUPMAST / GRUPSLAV
  -> master + slaves sincronizados por MPI
```

Se generó una topología nativa con dos slaves y se verificó que:

- el master inicia;
- `MPI_Comm_spawn` se invoca;
- MODA y MODB arrancan;
- ambos leen `GRUPSLAV`;
- ambos entran en el loop de simulación.

En este VPS el job no completa el handshake dinámico de Open MPI. Por ello no
se afirma que la ejecución nativa S2RC haya terminado correctamente.

## 13. Decisión de ownership del network

Para obtener una interacción simétrica y ejecutable, el `NETWORK` se extrajo a
un master neutral dedicado. Funcionalmente:

- A afecta la presión de red;
- B afecta la presión de red;
- la presión de red afecta A;
- la presión de red afecta B.

Sin embargo, Model A ya no es literalmente el master RC que contiene a la vez
su reservorio y el network. Si ese ownership exacto es obligatorio, se necesita
resolver el runtime nativo S2RC/MPI o rediseñar el acoplamiento con soporte RC
nativo.

## 14. Limitaciones honestas

- Los decks son modelos demostrativos pequeños, no modelos de campo.
- El intercambio es por periodo de reporte, no por Newton/ministep interno.
- La incertidumbre de red se mantiene nominal.
- El runtime nativo S2RC continúa bloqueado por MPI dynamic spawn en este host.
- Los PRT deben evaluarse con un gate fail-closed que exija el bloque final
  `Error summary`; si Flow se ejecuta con modos que omiten ese bloque, un exit
  code cero no debe presentarse como prueba `Warnings 0 / Errors 0 / Problems
  0`.

## 15. Conclusión

La corrección solicitada está implementada: los dos ensembles se acoplan
realización por realización, Model B participa como simulación Flow real,
`GSATPROD` se retiró de la ruta primaria y la contrapresión del network vuelve a
los dos reservorios durante la iteración de cada periodo de reporte.
