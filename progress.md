# Progress

## 2026-07-17 - Phase 0/1 complete

- Workspace was empty and not under Git; initialized a new `main` repository.
- Read the user-requested harness methodology and the required document/presentation skills.
- Verified the official HyperFusion challenge page, registration form, full competition rules and intellectual-property terms.
- Confirmed registration closes at 2026-07-19 24:00 Asia/Shanghai; each company selects three teams for the cohort.
- Confirmed judging dimensions: AI innovation, business value, scalability and solution professionalism.
- Confirmed mandatory challenge outputs: multiple DOCX reports, design document, complete code, operation manual and full-flow multi-query demo video.
- Validated public access patterns for China Tendering & Bidding Net and China Government Procurement Network.
- Validated that Qianlima redirects unauthenticated search to free registration, making it suitable for the required authorized-login source adapter.
- Defined the product as “标擎 BidPilot” and the working team name as “聚标成擎”.

## Current

- Completed F01: the versioned Chinese query compiler parses all four official examples plus weekly delivery, reversed date ranges, defaults and invalid input. Eight tests pass.
- Completed the configuration, Pydantic domain model and transactional SQLite schema foundation.
- Fixed setup failure detection and verified a clean editable development install on Python 3.13.
- In progress: real source adapters and the evidence-first normalization pipeline.
- Completed F02/F03: CEC Bid and CCGP adapters include real search/list parsing, detail extraction, attachment capture, request throttling and bounded retries.
- Implemented F04 pending user authorization: Qianlima detects missing/expired free-member sessions and contains a tested parser for the authorized result surface.
- Completed F05/F06: strict date/region/topic filtering, relevance scoring, evidence-gated extractive/optional-LLM summaries, cross-site duplicate merging and lifecycle grouping.
- Completed F07: DOCX reports follow the required filename rule and include query scope, coverage disclosure, required item fields, source/attachment hyperlinks and evidence.
- Implemented F09 channels; local delivery is testable now, while Feishu webhook/app delivery needs user credentials.
- Sixteen automated tests pass. A real online run for “最近3个月安徽服务器招标信息” fetched five CEC candidates and retained only the matching安徽大学 server notice at relevance 83.
- In progress: orchestration service, persistent scheduler, API/CLI and Web UI.

## Known constraints

- No Git remote exists, so the harness push requirement cannot be satisfied until the user supplies a remote. Local atomic commits will still be created.
- A real Qianlima free-member session requires the user to complete registration/login; credentials and cookies must never be committed.
- Final form submission requires team composition, member identity/student proof and the user’s action-time confirmation.

## Next

1. Create the Python package, configuration, database schema and source abstractions.
2. Implement and verify the query compiler.
3. Implement and verify sources, normalization, report generation and scheduling.
4. Build the Web UI/API/CLI, then generate competition artifacts.
