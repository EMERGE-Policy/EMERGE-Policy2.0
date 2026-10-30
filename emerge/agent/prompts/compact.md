The conversation below is about to be removed from the context and replaced by the summary you write. Everything you want kept must be in the summary: it is the only part that survives.

Write a compact handover note for the assistant that will continue this work. Do not continue the conversation, do not answer its last message, and do not comment on the task.

Keep, in this order:

1. What the user wants. Their intent, stated preferences, corrections and constraints, including any change of direction and what it replaced.
2. What is done. Completed steps and the results that matter, with concrete values: file paths, identifiers, commands, counts, error messages.
3. What is not done. Open questions, decisions still pending, and the next step as it stood at the end.
4. Environment actions already taken. Every action that changed something outside the conversation, in the order it happened, with the exact targets. These cannot be undone, so a later assistant must know they happened.
5. Key facts learned. File locations, interfaces, configurations and conclusions that would be expensive to rediscover.

Rules:

- Write prose and short lists, not a transcript. No greetings, no meta-commentary.
- Preserve exact names, paths, numbers, and error text; paraphrase everything else.
- Drop small talk, repeated explanations and superseded plans.
- Images cannot be kept; note what an image showed if it mattered.
- If something is uncertain, say so explicitly instead of guessing.

Conversation to summarize:

{conversation}
