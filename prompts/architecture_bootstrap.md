You are generating an ARCHITECTURE.md document for a software project.
You have been given the results of a four-phase architectural analysis:
reconnaissance, flow trace, abstraction analysis, and synthesis.

Produce a complete, well-structured ARCHITECTURE.md in GitHub-flavoured
markdown. Use the following sections (include all that have data):

# Architecture: <system name>

## Overview
One paragraph. Domain, purpose, architectural style.

## Components
Each major component with its role. Use a table if there are many.

## Request Flow
Prose tracing the critical path identified in the flow trace phase.
Include a Mermaid sequence diagram if the flow is non-trivial.

## Data Layer
Databases, stores, and their purpose. Schema highlights if notable.

## External Dependencies
Third-party APIs, queues, and services the system depends on.

## Architectural Decisions
Key decisions and trade-offs. Link to ADRs where available.

## Known Issues & Technical Debt
Surface any HIGH or CRITICAL findings from the synthesis as a prioritised list.
Do not omit these — they are the reason this document exists.

## NFR Risks
Scalability, observability, security concerns from the analysis.

Rules:
- Write in present tense.
- Be specific — use actual file paths, component names, and tool names from the analysis.
- Do not invent information not present in the phase results.
- If a section has no data, omit it entirely rather than writing a placeholder.
- Output only the markdown document. No JSON, no preamble, no fences.
