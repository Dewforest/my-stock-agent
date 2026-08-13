# Private runtime calendars

This directory contains documentation only. Derived exchange schedule data must not be committed here or anywhere else in the repository.

The annual-calendar authority investigation remains `PARTIAL`. Official pages and community libraries may support research and discrepancy detection, but neither may populate production `OFFICIAL` authority without all of the following:

- exact official source artifacts and byte digests;
- approved parser evidence;
- approved exchange-data license profile;
- approved private install profile;
- adjacent approved annual versions for every represented exchange.

Use `scripts/generate_private_runtime_calendar.py` only with a user-authorized local source artifact. Output is fixed to the current user's private runtime directory and cannot be selected by a CLI argument:

`~/Library/Application Support/my-stock-agent/calendars/`

The generator creates directories with mode `0700` and files with mode `0600`. It always marks generated schedules as `RESEARCH_REPORT/PARTIAL`; successful local generation does not activate the production session gate.

Filesystem threat boundary: descriptor-relative traversal with `O_NOFOLLOW` prevents accidental repository output, symlink traversal, and path-component replacement while opening or writing. It does not claim to resist a malicious process running as the same macOS user that deliberately renames the private runtime directory after it has been opened; such a process already has equivalent authority over the user's files.

Each generated schedule must explicitly classify every date in its declared coverage interval as exactly one trading session or closure. An annual market projection additionally requires full January 1 through December 31 coverage, every exchange represented for every year, and consecutive annual versions.

The code-level approved official schedule manifest is intentionally empty while Phase 0 authority remains partial. Therefore `RuntimeMarketSchedule` currently always fails closed. Private generation, a self-consistent digest, or self-declared approval fields cannot add an entry to that manifest or activate production scheduling.

Repository-local defensive ignore paths:

- `config/calendars/private/`
- `.runtime/calendars/`
