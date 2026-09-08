# Privacy Policy

**Last updated:** June 12, 2026

## The short version

Kindex is a local-first tool. Your data stays on your machine. We don't collect it, transmit it, or have access to it.

## What Kindex Is

Kindex is an open-source, local-first knowledge graph that runs entirely on your computer. It stores data in a local SQLite database in your home directory. It operates as a CLI tool and MCP server — there is no cloud service, no account system, and no server infrastructure.

## Data We Collect

None. Kindex does not collect, transmit, store, or process any user data on external servers. All data remains on your local filesystem under your control.

## Data You Create

When you use Kindex, you create a local knowledge graph containing nodes, edges, tags, and session metadata. This data is stored in:

- A SQLite database in `~/.kindex/` (or a project-local `.kin/` directory)
- Optional export files you explicitly create

This data never leaves your machine unless you explicitly copy, share, or publish it yourself.

## Third-Party Services

Kindex does not communicate with any third-party services for its core operation. It does not phone home, check for updates, or transmit telemetry.

If you configure optional embedding providers (e.g., Google Gemini), those API calls are made using your own API key and are governed by that provider's terms. Kindex does not intermediate, log, or cache these requests.

## MCP Host Context

When Kindex is used as an MCP plugin, tool responses are returned to the host agent session. That content is processed by the host provider, such as Anthropic for Claude Code, OpenAI for Codex, Google for Gemini CLI or Antigravity, or the provider behind your chosen MCP-capable client, according to that provider's privacy policy. Kindex has no visibility into or control over what happens after tool responses are returned to the host.

## Analytics and Tracking

The website (kindex.tools) does not use cookies, analytics, tracking pixels, or third-party scripts.

## Data Retention and Deletion

All data is local. Delete `~/.kindex/` and everything is gone. There is nothing to request from us because we don't have anything.

## Children's Privacy

Kindex does not collect personal information from anyone, including children under 13.

## Changes to This Policy

If Kindex ever adds cloud features or data collection, this policy will be updated before those features ship. Local-first is an architectural principle, not an accident.

## Contact

- Questions: [github.com/wandercom/kindex/issues](https://github.com/wandercom/kindex/issues)
- Source: [github.com/wandercom/kindex](https://github.com/wandercom/kindex)
- Web: [kindex.tools/privacy](https://kindex.tools/privacy)
