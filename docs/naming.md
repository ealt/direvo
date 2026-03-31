# EDEN

**E**ric's **D**irected **E**volution **N**exus — the meeting point where
intelligent planning and evolutionary selection converge.

## Tagline

> Intelligent evolution.

## Short description

EDEN is an orchestration engine for directed code evolution. A planner proposes
experiments, parallel trials run in isolated environments, and results feed back
into the next round — an automated loop of diversify, evaluate, amplify.

## Elevator pitch

Most research automation tools are either a single-agent loop (try something,
check if it worked, repeat) or a static job runner (execute a fixed matrix of
experiments). EDEN is neither. It's an orchestration system that runs concurrent
research trials inside Docker, where a planner — human, AI, or hybrid — proposes
experiments via a shared database, and an orchestrator dispatches them as parallel
git worktrees under isolated Linux users. Each trial is evaluated against a
fitness function, and results flow back to the planner to inform the next
generation of proposals. The architecture mirrors directed evolution in the lab:
generate a library of variants, screen them, amplify the winners, repeat. The
planner brings the intelligence; EDEN brings the infrastructure to run the search
at scale.

## Etymology

The name operates on two levels.

**The acronym:** Eric's Directed Evolution Nexus — a nexus where experimental
proposals, parallel execution, and evaluative selection meet. The system is the
convergence point between a planner's intelligence and the outcomes of automated
trials.

**The allusion:** The Garden of Eden is where creation happens by design — an
omniscient creator, a deliberate act. Directed evolution is the opposite: creation
through iterative selection, not foresight. EDEN sits in the tension between
these two ideas. The planner brings intent and strategy (intelligent design), but
the mechanism is evolutionary — propose variants, run trials, select the fittest,
repeat. The tagline "Intelligent evolution" captures this tension directly: it
inverts "intelligent design," preserving the cadence but swapping the noun.

## Scientific lineage

Directed evolution is a Nobel Prize-winning technique (Frances Arnold, 2018)
for engineering proteins through iterative cycles of diversification, screening,
and amplification. A researcher generates a library of variants, evaluates them
against a fitness function, and uses the best results as the template for the
next round.

The parallel to EDEN's architecture is nearly 1:1:

| Directed Evolution          | EDEN                                        |
| --------------------------- | ------------------------------------------- |
| Library of variants         | Parallel trials in isolated git worktrees   |
| Screening / assay           | Eval script producing JSON metrics          |
| Amplification (next round)  | Planner reads results, proposes next batch  |
| Iterative rounds            | The propose / execute / evaluate loop       |
| Mutagenesis                 | Planner proposes modifications to the code  |
| Intelligent guidance        | Planner is strategic, not random            |

## Package name

The package is published and installed as `direvo` (**dir**ected **evo**lution),
which remains available across all relevant registries. After installation, both
`direvo` and `eden` work as CLI commands — users can think of the tool as EDEN
while the package name stays short, unique, and conflict-free.

```
pip install direvo
eden run --config .direvo/config.yaml
```

The name "eden" is heavily used across the software ecosystem (Eden AI, Eden
emulator, multiple PyPI packages), making it impractical as a package name. The
dual-name strategy avoids all registry conflicts while giving users the brand
name at the command line.

## Availability (package name: direvo)

| Registry     | Name      | Status    |
| ------------ | --------- | --------- |
| PyPI         | direvo    | Available |
| GitHub user  | direvo    | Available |
| GitHub org   | direvo    | Available |
| npm          | direvo    | Available |

Checked 2025-03-27.
