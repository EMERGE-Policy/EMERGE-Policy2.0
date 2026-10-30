---
name: recovery-after-irreversible-failure
description: Use when an irreversible node ends without the effect you needed (a failure outcome, or stopped before it finished); describes how to sense the environment before replanning.
---

# Recovering after an irreversible failure

An irreversible action cannot be undone. When a node that acts on the environment ends without the
effect you needed — it finished with a failure outcome such as "grasp failed", or it was stopped
before it was done — the environment may already be partly changed. Do not guess, and do not run the
action again to "see what happens": the first attempt may have taken effect.

## 1. Confirm the scene with a sensing node

Use a read-only capability to observe the environment. Find one with the `catalog` tool: query the
capabilities that read state (camera, state query, file reader) rather than the ones that act.

- Create a small sensing node with `graph` and connect it to the input the node would have produced,
  so downstream nodes can start once the observation lands.
- Start it with `execution` and wait for the result.
- If no sensing capability covers what the action changed, ask the user to check directly before
  going further. Do not assume.

## 2. Compare the observation with the intended effect

Decide which of these is true:

- **Nothing happened.** The observation matches the state before the action. The action is safe to
  plan again, as a new node.
- **The action completed.** The observation matches the intended effect even though the node did
  not report success. Treat it as done and continue downstream.
- **The action partially completed.** Something moved or changed, but not as intended. The
  environment is in a state no planned node expected.
- **The state cannot be determined.** The sensing capability cannot see the affected part of the
  environment. Stop and ask the user.

## 3. Fix forward

Edit the graph to match the state you observed; never replay or rewind.

- Build on what exists. If the action completed, wire the existing result into the downstream
  nodes. If it partially completed, add a node that brings the environment to the state the rest of
  the plan assumes.
- Where the state is still uncertain, insert an explicit sensing-and-correction step before the nodes
  that depend on it, so the uncertainty is resolved by an observation rather than an assumption.
- Use `graph` to add or reconnect nodes and `execution` to start them. Keep `irreversible` on every
  node that acts on the environment.
- Tell the user what you observed, what you changed, and why. A partial action is a fact they need.

Never retry the failed action automatically, and never design a rollback. There is no rollback.
