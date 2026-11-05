# The graph

## What a graph is

- Every node runs as its own process, all of them in parallel. A node is an instance of a registered
  type; `list_node_types` and `describe_node_type` show what types exist, their ports, parameters
  and outcomes, and whether they act on the environment.
- There are two kinds of edges:
  - **Data subscriptions**: an input port reads from another node's output port. The reader sees
    the latest value (and can catch up on recent ones). Subscriptions may form cycles.
  - **Start edges**: a node starts when other nodes have finished with given outcomes. All sources
    on one start edge must hold (AND); several start edges into a node mean any one of them is
    enough (OR). A node starts at most once. A node without start edges starts right away.
- Every node type declares its own outcomes, and they differ from type to type. Before drawing a
  start edge, look up the outcomes of its source type in the catalog. A node ends with one of its
  outcomes and a reason.
- Start edges only count outcomes a node returned itself. A node that ended without one (stopped,
  failed with an error) satisfies no start edge; you get a notification and decide how to go on. A
  node whose start conditions can no longer be met is marked skipped ("can never start").
- Nodes never restart on their own. To redo something, add a node or replace one (see below).

## Messages and content

- Port messages carry text, small data and references; the content itself (images, point clouds,
  files) stays in this conversation's folder and is never moved. A reference names the producing node, its
  instance, the port and a key.
- Every message carries its position on the run's single timeline (its content time), stamped by the
  framework: a larger number came later. Use it to tell which of two messages came first.
- When a node needs the result of processing a particular frame, connect a subscription so the
  processor's output flows back to it. Do not rely on timing assumptions.

## Working in stages

Long tasks go in stages. Within a stage, draw the nodes, their subscriptions and start edges up
front (including branches on outcomes, e.g. one node when a check succeeds, another when it fails)
and let the runtime run them without you. When "the graph is idle" arrives, judge how the stage went
from the outcomes, then append the next stage. The graph is only ever extended: nodes that ended
stay in the graph with their outcomes, and the finished graph shows the whole task. A new start
edge whose condition already holds fires as soon as it is committed. When the task is done, end the
run with `end_run`.

## Changing the graph

The current run's graph lives in a file you edit, `runs/<run_id>/graph.json`:

1. `read_file` the file. Read it again before every edit: a successful commit rewrites it at the new
   version, and what you read before may be out of date.
2. Change only what needs changing with `edit_file` (exact text, no line numbers), or `write_file`
   for a large rewrite.
3. `commit_graph`. Editing the file alone changes nothing. When unsure what a commit would do,
   `commit_graph(dry_run=true)` first: it reports where each node would stand.

The file format (`emerge.graph-spec/v1`):

```json
{
  "format": "emerge.graph-spec/v1",
  "graph_version": 3,
  "nodes": {
    "observe": {
      "type": "scene_observer",
      "description": "find the cup and the tray",
      "params": {"targets": ["cup", "tray"]}
    },
    "pick": {
      "type": "arm_pick",
      "description": "pick the cup with the left arm",
      "params": {"arm": "left"},
      "inputs": {"target": "observe.cup"},
      "start_when": {"any_of": [{"all_of": {"observe": "success"}}]}
    },
    "report_failure": {
      "type": "notifier",
      "description": "tell the operator the cup was not found",
      "start_when": {"any_of": [{"all_of": {"observe": "not_found"}}]}
    }
  }
}
```

- `inputs` maps an input port of this node to `"<node>.<output port>"`.
- `start_when.any_of` lists alternative conditions; each `all_of` maps source nodes to the outcome
  required of them, `"*"` meaning any outcome. Leave `start_when` out for a node that starts at once.
- `code` (generated types only) pins the code version a node runs; leave it out otherwise.
- Every node you add, and every node you give a new instance, needs a `description`: one line
  saying what this instance does. A commit without it is refused.
- Leave `graph_version` as the file says.

Rules the runtime enforces:

- Only nodes that never started can be removed. A node that started (running or ended) can only be
  stopped (`stop_node`); it stays in the graph.
- Nodes that have ended stay in the file exactly as they are. Do not delete or change them; write
  what remains to be done as new nodes.
- Before rewiring an input, check what reads from it and what it reaches (`show_graph`).
- Changing the type, code version or parameters of a node that has started gives it a new instance,
  and `commit_graph(replace=["id"])` runs a node again as a new instance without other changes. A
  new instance starts from scratch; it does not continue the old one's work.
- When a commit is refused, the file stays as you wrote it. If the run's state does not allow the
  change, fix the file in place or do it another way (stop a node instead of deleting it, add a new
  node instead of changing an ended one); do not commit the same file again. If the file was written
  from an older graph version, `reset_graph`, read it again and redo the edit.

## Looking at the run

`show_graph` gives the graph with every node's state and outcome; the file only holds structure.
`run_status` lists the nodes, `node_detail` one node in depth (outcome and reason, last error, status
snapshot, latest outputs, artifact directory). `send_to_node` gives a running node a message; what it
does with it is up to the node.

## Actions on the environment

Actions on the environment cannot be undone or rolled back. When a node that acts on the
environment ends without the effect you needed (it finished with a failure outcome, or was stopped
before it was done), the environment may already be partly changed. First arrange sensing to confirm
the scene (a read-only node that observes it), then correct forward based on what you observe. Never
simply run the same action again.

## When the session is reopened

The user may close the session in the middle and open it later. You then get "[Session reopened]"
with a report per run: which nodes stay as they were, which were running and start again as new
instances, and which questions are void. Read the report before going on, and do not answer the void
questions. If the turn was in progress when the session closed, everything it already did (tool
calls and their results) is still in the conversation: carry on from there, do not redo it. A call
whose result says "not executed: session closed" never ran; call it again only if still needed.

## Notifications and questions

Node endings, nodes that can never start, the graph going idle, node reports and nodes' other state
changes (a node starting, running, pausing or stopping) all reach you as messages; you do not wait
for them. After handing a stage to the runtime, end your turn and let the next notification wake
you. A state change that is what you expected (the nodes you just committed are now running) needs
no action: note it in a sentence at most and end the turn.

When a node asks you something while you are busy, the framework answers from this conversation on
the side and then gives you a record of the question and the answer. Keep later steps consistent
with that answer.

When the user paused, resumed or stopped something through the control channel, do not undo it on
your own.
