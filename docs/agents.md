# Agents

tam starts and resumes agents from short command templates. An agent counts as installed when the program in its `start` command is on `PATH`; `tam agent` lists them.

## Built in

| Agent | Program | Resume |
|---|---|---|
| claude | `claude` | `claude --resume <id>`: the exact session, reported by a hook |
| codex | `codex` | `codex resume --last` |
| gemini | `gemini` | `gemini --resume latest` |
| qwen | `qwen` | `qwen --continue` |
| opencode | `opencode` | `opencode --continue` |
| crush | `crush` | `crush --continue` |
| copilot | `copilot` | `copilot --continue` |
| cursor | `cursor-agent` | `cursor-agent resume` |
| goose | `goose session` | `goose session --resume` |
| aider | `aider` | `aider --restore-chat-history` |
| amp | `amp` | none: Amp resumes by thread id only, so a restored pane starts a new thread |

All but Claude resume the most recent session in the pane's directory. Two panes running the same agent in one directory therefore resume the same conversation.

## Default agent

`⌥ a`, `Ctrl+b → A` and `tam new` without an agent name use the default agent, stored in `~/.config/tam/config.toml`:

```toml
default_agent = "claude"
```

`tam agent <name>` changes it. `Tab` in the directory picker uses another agent for one start.

## Adding or changing an agent

Add a block to `~/.config/tam/harnesses.toml`. A block with a built-in name overrides that agent's fields.

```toml
[harness.myagent]
start = "myagent --model fast"
resume = "myagent --continue"

# Always start Claude in a permission mode:
[harness.claude]
start = "claude --permission-mode acceptEdits -n {name}"
```

| Field | Meaning |
|---|---|
| `start` | Command for a new session. `{name}` is the pane's agent name. |
| `resume` | Command to continue it. With `{id}`, the id comes from the agent's hook (see below). Without `{id}`, the command runs as written once the agent has started in that pane. Without `resume`, `start` is used again. |

Arguments after `--` in `tam new myagent -- --flag` are added to both commands.

## Session ids from other agents

Claude reports its session id through a `SessionStart` hook that `tam setup` installs. An agent with its own hook mechanism can do the same by running:

```sh
tam hook session <session-id>
```

inside its tmux pane. Its `resume` template can then use `{id}`.
