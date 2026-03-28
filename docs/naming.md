# DirEvo

**Dir**ected **Evo**lution — the lab technique of steering evolution with
intent. Not blind mutation, not omniscient design, but an intelligent agent
guiding iterative experimentation toward a goal.

## Etymology

Directed evolution is a Nobel Prize-winning technique (Frances Arnold, 2018)
for engineering proteins through iterative cycles of diversification, screening,
and amplification. A researcher generates a library of variants, evaluates them
against a fitness function, and uses the best results as the template for the
next round.

The parallel to DirEvo's architecture is nearly 1:1:

| Directed Evolution          | DirEvo                                      |
| --------------------------- | ------------------------------------------- |
| Library of variants         | Parallel trials in isolated git worktrees   |
| Screening / assay           | Eval script producing JSON metrics          |
| Amplification (next round)  | Planner reads results, proposes next batch  |
| Iterative rounds            | The propose / execute / evaluate loop       |
| Mutagenesis                 | Planner proposes modifications to the code  |
| Intelligent guidance        | Planner is strategic, not random            |

The name captures what makes the system distinctive: it sits between pure
natural selection (random mutations, blind fitness pressure) and intelligent
design (an omniscient planner producing the optimal solution in one shot). Like
directed evolution in the lab, an intelligent agent steers the search — but
still needs to run experiments and learn from results.

## Tagline

> Steer the search.

## Short description

DirEvo is an orchestration engine for directed code evolution. A planner
proposes experiments, parallel trials run in isolated environments, and results
feed back into the next round — an automated loop of diversify, evaluate,
amplify.

## Elevator pitch

Most research automation tools are either a single-agent loop (try something,
check if it worked, repeat) or a static job runner (execute a fixed matrix of
experiments). DirEvo is neither. It's an orchestration system that runs
concurrent research trials inside Docker, where a planner — human, AI, or
hybrid — proposes experiments via a shared database, and an orchestrator
dispatches them as parallel git worktrees under isolated Linux users. Each trial
is evaluated against a fitness function, and results flow back to the planner to
inform the next generation of proposals. The architecture mirrors directed
evolution in the lab: generate a library of variants, screen them, amplify the
winners, repeat. The planner brings the intelligence; DirEvo brings the
infrastructure to run the search at scale.

## Availability

| Registry     | Name      | Status    |
| ------------ | --------- | --------- |
| PyPI         | direvo    | Available |
| GitHub user  | direvo    | Available |
| GitHub org   | direvo    | Available |
| npm          | direvo    | Available |

Checked 2025-03-27.
