# macOS Paper Runtime Runbook

Unattended CN/US paper-trading runtime for `my-stock-agent`. The LaunchAgent
only wakes `run-once`; all business semantics live in the runtime.

## Login-session / Keychain limitation

- The agent runs in the **user GUI login session** (`gui/$UID`), so Keychain
  items stored in the login keychain are only readable while the user is logged
  in. A headless launchd user agent cannot read login-keychain secrets.
- Only two Keychain services are addressable, both read **only at invocation**:
  `com.dewforest.my-stock-agent.deepseek` and
  `com.dewforest.my-stock-agent.alpha-vantage`.
- The runtime never enumerates Keychain, never uses `-A`, and clears the secret
  reference immediately after provider invocation.

## Scoped kill switches

Three scopes block new decisions while preserving read/report/status paths:

- `GLOBAL` — halt both markets.
- `MARKET` — halt one market (value `US` or `CN`).
- `ACCOUNT` — halt one account.

Set via the runtime store; see `status` for current state.

## Reports

Reports are rebuilt deterministically from committed state under the
application-support report directory. Each report is a secret-free JSON envelope
plus a derived Markdown file, identified by a content digest.

## Recovery

- A crash is recovered by the append-only ledger and run-claim tracer; replay is
  byte-identical.
- Ambiguous LLM decisions are never recalled automatically — they surface as
  `NEEDS_RECONCILIATION` for an operator to resolve.

## Uninstall

```bash
python scripts/install_paper_runtime_launchagent.py uninstall
```

This boots the label out of the user GUI domain and removes the plist. It never
touches business state, stores, or reports.
