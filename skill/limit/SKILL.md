---
name: limit
description: Configure the local per-session Claude 5-hour usage governor.
disable-model-invocation: true
user-invocable: true
---

Use the local Token Governor. The command is intercepted by a UserPromptExpansion hook and is not sent to the model.

`/limit 50 45` — session budget 50%, ship at 45%.
`/limit status` — show account 5h usage, detected plan, session accounting and limits.
`/limit on|off|reset` — enable, disable, or reset this session's local counter.

The governor counts the main transcript and all child-agent transcripts. Its local code does not make inference calls. Any short `additionalContext` reminders it injects are part of normal Claude usage and therefore are included in transcript accounting.
