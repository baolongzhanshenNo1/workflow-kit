/**
 * dsh-wfk — issue-driven workflow discipline for dsh coding agents.
 *
 * Registers one system-prompt section that binds the agent to the wfk
 * ledger protocol (workflow-kit): every change is an issue card, edits
 * happen in a worktree, merges require tiered-gate evidence, and
 * completion is decided by the completion matrix — never by assertion.
 * The section also names the vendored wfk.py so the agent can run the
 * tool without any separate install.
 * @module dsh-wfk
 */

import { fileURLToPath } from 'node:url'

/** Stable Cordis plugin name — must match the cordis.patch.yml insert row. */
export const name = 'wfk-protocol'

/** Only the prompt registry is used; no tools, no UI. */
export const inject = ['systemPrompt']

/** Directory containing the vendored wfk tool (wfk.py, wfk_init.py). */
const VENDOR_DIR = fileURLToPath(new URL('../vendor', import.meta.url))

const PROTOCOL = `This repository's development is governed by the wfk issue workflow (workflow-kit, MIT). Follow it for every code change.

Rules:
1. One change, one card. Before touching code: wfk new "<title>" --kind engine|measure|process|hygiene --scope <path> --verify "A<n>: <command> @step:g<n>". The write scope and the acceptance assertions are fixed at creation, not negotiated afterwards.
2. wfk approve <ID>, then wfk start <ID>: work on the feature branch in the worktree it creates. Never commit straight to the default branch.
3. Merge only through gates: g0 syntax, g1 path-selected tests, g1.5 revert-must-red (revert the card's changes and the selected tests must fail — still-green means the guard is void), g2 real-machine vision, g2.5 corpus check. Every passing layer leaves evidence bound to the commit fingerprint.
4. Saying "done" is not done. The completion matrix decides: wfk gate <ID> status all-green plus wfk merge <ID> — otherwise the work is not finished, no matter how confident you are.
5. The ledger is the truth. Read docs/issues/ first; any session can take over with wfk list.

Activation: if the working repository has docs/issues/README.md (the wfk protocol), the rules above apply. If it does not, propose wfk init once and let the user decide — never force it.

Tool (vendored, pure Python 3.11+ stdlib, zero dependencies): run "python" with "${VENDOR_DIR.replace(/\\/g, '/')}/wfk.py" as the script path — e.g. python "${VENDOR_DIR.replace(/\\/g, '/')}/wfk.py" list. Use it for every workflow command.`

/**
 * Register the protocol prompt section.
 * @param ctx - plugin context with the systemPrompt service.
 * @returns after the section is registered.
 */
export async function apply(ctx) {
  ctx.systemPrompt.section({
    name: 'wfk:protocol',
    order: 5200,
    interpolate: false,
    text: () => PROTOCOL,
  })
}
