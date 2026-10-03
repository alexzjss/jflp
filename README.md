# JFLP

A modular experimental pipeline for **Spectrum-Based Fault Localization (SBFL)** on Java software.

JFLP automates the complete workflow required to execute and evaluate fault-localization experiments over real-world Java bugs:

```text
benchmark
    ↓
checkout
    ↓
build
    ↓
fault localization
    ↓
spectrum collection
    ↓
ground truth
    ↓
SBFL metrics
    ↓
evaluation
    ↓
reports
```

The goal is to make fault-localization experiments **reproducible, extensible and comparable** across different Java benchmarks and projects.

## Why?

Spectrum-Based Fault Localization techniques use the execution behavior of passing and failing tests to estimate which program elements are most likely responsible for a failure.

In practice, however, evaluating SBFL techniques involves considerably more than calculating a suspiciousness formula.

A complete experiment must deal with:

- checking out specific defective versions;
- compiling legacy Java projects;
- identifying and executing test suites;
- collecting execution spectra;
- identifying the actual faulty code;
- handling build and execution failures;
- calculating suspiciousness scores;
- handling tied suspiciousness values;
- calculating ranking metrics;
- aggregating results across many bugs.

JFLP provides an automated workflow for these steps.

## Features

- Support for multiple Java bug benchmarks.
- Adapter-based benchmark architecture.
- Integration with Jaguar 2 for spectrum collection.
- Automatic ground-truth extraction from buggy/fixed versions.
- Offline calculation of multiple SBFL formulas.
- Line-level and class-level fault ranking.
- Explicit handling of suspiciousness ties.
- EXAM and Top-N evaluation.
- Automatic recovery from selected fault-localization crashes.
- Structured per-bug experiment artifacts.
- Aggregated CSV reports and plots.
- Reproducible experiment metadata.
- Safe Git worktree-based benchmark preparation.

## Supported benchmarks

Currently supported:

- Defects4J
- Bugs.jar / bugs-dot-jar

The benchmark layer is implemented through adapters, allowing additional Java datasets to be integrated without changing the core evaluation pipeline.

Potential future adapters include:

- Bears
- GrowingBugs
- BugSwarm

## Fault Localization

JFLP currently integrates with Jaguar 2 as its spectrum collection engine.

Jaguar produces execution information containing the values required to calculate SBFL suspiciousness formulas:

```text
cef
cep
cnf
cnp
```

Once the spectrum has been collected, additional SBFL techniques can be calculated without executing the benchmark again.

Currently supported formulas include:

- Ochiai
- Tarantula
- Jaccard
- DStar
- Op2
- Zoltar
- Barinel
- Kulczynski2

This separation between spectrum collection and metric calculation makes experiments considerably cheaper to reproduce and compare.

## Evaluation

For each fault, JFLP calculates suspiciousness rankings and evaluation metrics such as:

- best rank;
- average rank;
- worst rank;
- EXAM;
- Top-1;
- Top-3;
- class-level ranking.

Ties are handled explicitly.

For example, if several program elements receive the same suspiciousness score, the evaluation can report:

```text
rank_best
rank_avg
rank_worst
```

This prevents an arbitrary ordering of tied elements from artificially improving the reported performance of a technique.

Top-N evaluation uses the conservative interpretation of ties.

## Ground Truth

JFLP derives line-level ground truth by comparing the defective and corrected versions of a program.

For a conventional modification:

```text
buggy:

x = calculate(a);
return x;
```

```text
fixed:

x = calculate(a);
x = normalize(x);
return x;
```

the modified lines in the defective version can be identified from the diff.

Pure additions require special treatment because the inserted code does not exist in the defective version.

JFLP therefore uses neighboring lines as anchors in this situation.

This behavior is intentionally explicit because ground-truth construction is a methodological decision that can influence SBFL evaluation results.

Future versions may support alternative ground-truth strategies, including method-level localization.

## Experiment Artifacts

Each analyzed bug produces a self-contained result directory:

```text
results/
└── <benchmark>/
    └── <project>/
        └── <bug>/
            ├── jaguar_Ochiai.xml
            ├── truth.json
            ├── metrics.json
            ├── meta.json
            └── jaguar.log.gz
```

### `jaguar_Ochiai.xml`

Raw spectrum information collected by Jaguar.

### `truth.json`

Ground-truth fault locations derived from the buggy/fixed diff.

### `metrics.json`

SBFL rankings and evaluation metrics.

### `meta.json`

Experiment metadata, including:

- execution status;
- execution time;
- Java version;
- failed tests;
- excluded classes;
- analysis exceptions;
- benchmark information.

### `jaguar.log.gz`

Compressed execution log useful for diagnosing failed experiments.

## Experiment Status

Each bug receives an explicit execution status.

Examples include:

```text
ok
fault_not_covered
no_failing_tests
jaguar_crash
jaguar_timeout
build_failed
no_tests
no_truth
```

This is important when running large benchmark studies.

Instead of silently discarding failed experiments, JFLP preserves the reason why a bug did not reach the final evaluation stage.

The resulting funnel can therefore show how many benchmark versions successfully passed through each stage of the experiment.

## Architecture

```text
sbfl/
├── adapters/
│   ├── base.py
│   ├── bugsjar.py
│   └── defects4j.py
│
├── aggregate.py
├── classfile.py
├── cli.py
├── config.py
├── diffgt.py
├── jaguar.py
├── pipeline.py
├── proc.py
└── report.py
```

The core pipeline is intentionally separated from benchmark-specific logic.

A benchmark adapter is responsible for:

```text
list_bugs()
prepare()
```

The adapter prepares a bug and returns the execution context required by the fault-localization engine.

The rest of the pipeline can then operate independently of the benchmark.

## Usage

Install the Python dependencies:

```bash
pip install pandas matplotlib
```

Create the configuration file:

```bash
cp config.example.toml config.toml
```

Configure:

- Java installation;
- Jaguar library;
- benchmark repositories;
- working directories;
- Maven configuration.

Validate the environment:

```bash
python -m sbfl doctor
```

List available bugs:

```bash
python -m sbfl list --benchmark bugsjar
```

Run a single bug:

```bash
python -m sbfl run \
    --benchmark bugsjar \
    --project maven \
    --bug bugs-dot-jar_MNG-5742_6ab41ee8
```

Run a small sample:

```bash
python -m sbfl run \
    --benchmark bugsjar \
    --limit 5
```

Run a Defects4J project:

```bash
python -m sbfl run \
    --benchmark d4j \
    --project Lang
```

Recalculate metrics from previously collected spectra:

```bash
python -m sbfl analyze
```

Aggregate experiment results:

```bash
python -m sbfl aggregate
```

Generated aggregate results are stored in:

```text
results/_agregado/
```

Run the test suite:

```bash
python tests/test_core.py
```

Completed experiments are skipped automatically. Use `--force` when an experiment needs to be executed again.

## Reproducibility

Large-scale fault-localization experiments are sensitive to the execution environment.

Before running a complete benchmark, verify:

- Java version;
- Maven version;
- Jaguar version;
- benchmark version;
- test selection;
- available memory;
- execution timeouts.

Legacy Java projects may require older Maven and Java versions.

Defects4J should be executed in a Linux or WSL environment.

Jaguar's execution environment should also be isolated when running multiple experiments in parallel.

## Safe Repository Handling

Bugs.jar experiments use Git worktrees instead of modifying the original repository checkout.

This allows the pipeline to prepare different defective versions without relying on commands such as:

```bash
git reset --hard
git clean -fdx
```

which can destroy uncommitted files in a working repository.

## Testing

The project contains unit tests for the core experimental logic, including:

- diff parsing;
- ground-truth generation;
- SBFL ranking;
- tie handling;
- class-file inspection;
- Git integration;
- crash recovery;
- result aggregation.

The current test suite validates the pipeline logic using controlled test cases.

Full integration testing with Jaguar, Maven, Defects4J and Bugs.jar should be performed separately because it depends on the external benchmark and toolchain environment.

## Extending JFLP

To add a new Java benchmark, implement an adapter under:

```text
sbfl/adapters/
```

The adapter should provide:

```python
list_bugs(project, bug)
prepare(bug, log_path)
```

The preparation stage should return the information required by the fault-localization engine:

```text
project directory
compiled classes
compiled tests
classpath
test classes
ground-truth locations
```

Once registered, the benchmark can reuse the existing:

```text
fault localization
        ↓
spectrum parsing
        ↓
SBFL metrics
        ↓
evaluation
        ↓
aggregation
```

pipeline.

## Roadmap

### Benchmark support

- [x] Defects4J
- [x] Bugs.jar
- [ ] Bears
- [ ] GrowingBugs
- [ ] BugSwarm

### Fault localization

- [x] Jaguar integration
- [x] Ochiai
- [x] Tarantula
- [x] Jaccard
- [x] DStar
- [x] Op2
- [x] Zoltar
- [x] Barinel
- [x] Kulczynski2
- [ ] Additional localization engines

### Evaluation

- [x] Line-level ranking
- [x] Class-level ranking
- [x] EXAM
- [x] Top-N
- [x] Tie-aware ranking
- [ ] Method-level ground truth
- [ ] Method-level ranking
- [ ] Confidence intervals
- [ ] Statistical comparison between techniques

### Experiment infrastructure

- [x] Structured per-bug artifacts
- [x] Execution metadata
- [x] Failure classification
- [x] Result aggregation
- [ ] Parallel experiment execution
- [ ] Containerized execution
- [ ] Reproducible experiment manifests

## Research Questions

JFLP can be used as infrastructure for experiments such as:

- How do different SBFL formulas perform across Java bug benchmarks?
- How sensitive are SBFL rankings to the selected test scope?
- How does class-level localization compare with line-level localization?
- How frequently is the actual fault covered by the collected spectrum?
- How do ties affect Top-N and EXAM measurements?
- How do different benchmarks influence the observed performance of SBFL techniques?
- What proportion of benchmark bugs can be successfully analyzed by a given fault-localization engine?

## Project Status

This is an experimental research tool.

The core pipeline and evaluation logic are covered by automated tests. Before large-scale experiments, the integration with the selected Java benchmark and fault-localization engine should be validated on a small set of known bugs.
## Citation

If this project is used in academic work, add the corresponding citation here.
