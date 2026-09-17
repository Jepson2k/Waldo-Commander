# AI control via MCP

Waldo Commander ships a built-in [Model Context Protocol](https://modelcontextprotocol.io)
server so an LLM client (Claude Code, Claude Desktop, …) can read the robot's
state, author and run programs, and — when you allow it — drive the arm. The
server runs **inside** the Waldo Commander process; there is nothing separate to
launch.

## Enable the server

The server is **off by default**. Turn it on in the control panel's **Settings**
tab under **MCP server**:

- **Enabled** — start the server on the next app launch.
- **Host** — `127.0.0.1` (loopback) by default. Set `0.0.0.0` to expose it on
  your LAN.
- **Port** — `7400` by default.

Changing any of these shows a *"restart to apply"* hint; they bind when the
server starts. Restart Waldo Commander after enabling. The endpoint is:

```
http://<host>:<port>/mcp
```

i.e. `http://127.0.0.1:7400/mcp` with the defaults.

## Register it with Claude Code

The repository ships a project-scoped [`.mcp.json`](https://docs.claude.com/en/docs/claude-code/mcp)
pointing at the default endpoint, so a Claude Code session started in the project
directory discovers it automatically (approve it when prompted). To register it
by hand, or for a different host/port:

```bash
claude mcp add --transport http waldo-commander http://127.0.0.1:7400/mcp
```

Confirm it's connected:

```bash
claude mcp list
```

`waldo-commander` should report **connected** — only while the app is running
with the server enabled.

## How the LLM should work

The server's instructions steer the LLM to put **all** code it writes — even a
quick throwaway — into a **visible program** in the editor (via
`programs.new` / `programs.propose_edit`), so you can see the diff, watch the
dry-run path, and scrub the timeline before anything runs. Direct motion tools
(`motion.jog_*`, `motion.move_*`) are reserved for single ad-hoc nudges.

They're also pointed at the on-disk program library (`programs.list_library`,
the repo's `programs/` directory) and told to open a worked example to learn
the program-side motion API instead of guessing it.

## Control modes

You decide how much the LLM can do on its own, from the **AI control mode**
selector in the control panel's Settings tab, by clicking the mode chip at the
top of the screen, or by cycling with **Alt+M**. A perimeter glow appears
whenever an MCP client is connected — faint while you hold control, breathing
at full strength while the AI is driving — and its color tracks the mode
(emerald Inspect, sky Auto-edits, violet Autopilot).

| Mode | Program edits | Robot motion |
|------|---------------|--------------|
| **Inspect** | you approve each edit in the editor | you approve each move |
| **Auto-edits** | applied immediately (flashed so you see them) | you approve each move |
| **Autopilot** | applied immediately | runs automatically\* |

\* In simulator mode everything is automatic. On **real hardware**, the first
move of an AI session always asks for a one-time confirmation, even in Autopilot.

At any time the amber **Take control** button (top-right, shown while an AI
holds control) seizes control back for you and halts any motion the AI started.
`motion.halt` and status reads are never gated.

## World tools

The `world.*` tools edit the same collision world the 3D scene shows and the
backend enforces. Every mutation reassigns the scene's program layer, so the
backend push, the amber "not yet confirmed" styling, program recording and the
local collision checker all apply exactly as they do to a human's edit.
Mutations need the control lease (changing the enforced world is not
actuation, so no hardware consent is asked); reads never do. Shapes travel as
their 7-item wire rows — `[kind, params, pose, collision, margin, name,
physics]` in metres and radians — or as dicts with the same fields.

| Tool | Does |
|------|------|
| `world.get` / `world.export` | the world document: installation layer (the floor is one of its shapes), program layer, whether the program layer is confirmed by readback, and the installation proposal |
| `world.set_shapes`, `world.add_shape`, `world.update_shape`, `world.remove_shape` | edit the program layer (the installation layer is the robot config's and read-only here) |
| `world.import_world` | apply a world document's program layer; reports whether its installation entries match the live one |
| `world.library_list` / `library_save` / `library_load` / `library_delete` | the object library — world documents saved beside the programs |
| `world.place_object` | drop a one-shape library entry into the layer under a new name and pose, physics intact |
| `world.propose_installation` / `world.discard_installation_draft` | move program shapes into the installation proposal, or drop them from it |
| `world.export_installation_toml` | the proposal (or the program layer) as the robot config's `[[installation_shapes]]` TOML |

Installation authoring is config authoring: a proposed shape is drawn in
violet and checked locally, but the backend enforces it only once its robot
config declares it. Export the TOML, put it in the config, and the proposal
clears itself when readback shows the backend enforcing it. A backend without
contact simulation refuses a shape carrying `physics`, and the refusal reaches
the LLM as the tool error.
