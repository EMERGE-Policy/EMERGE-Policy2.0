---
name: sense-before-act
description: Use before running any node that acts on the environment; describes how to add a sensing step to confirm the environment state first.
---

# Sense before you act

An action on the environment cannot be undone, so the state it acts on must be known at the moment it
runs. The state you observed while planning is already old by then: nodes run in parallel, earlier
stages move things, and the user may have changed the scene.

## Before the acting node starts

- Name the precondition in one sentence: "the object is at X", "the gripper is empty", "the file
  contains Y". If you cannot state it, you are not ready to act.
- Find a read-only capability that can check that precondition, using the `catalog` tool.
- Put a sensing node directly upstream of the action and connect the observed value to the action
  node's input. The action must wait for that value, not for an older one.
- Keep the sensing node free of side effects. It reads; it does not move anything.

## At the action boundary

- If a node supports a readiness check, use it: the action should fail closed when the precondition
  is false, not proceed on a stale assumption.
- Keep the observed value attached to the action so the trace records what the action saw.

## When the precondition is false

Do not run the action and do not "correct and then act" inside one node. Report the mismatch, then
replan: either add the step that establishes the precondition, or change the target. The graph is the
place to fix it; the environment is not.

Use `graph` to add the sensing node and its edge, `execution` to run the plan, and `catalog` to
confirm which capabilities can observe the precondition.
