# Phase 5: Shared Workspace UI

## Experience

`/work` exposes a curated agent library and active/recent cases. `/work/{caseId}` shows mandate, scope, parallel agent runs, evidence panels, comments, and PM/CIO finalization.

## Frontend Contract

TanStack Query owns server-state caching and invalidation. Poll only while a case includes queued or running work. Local component state owns launch/comment/finalization form input.

## Access

Members see cases and can comment. PM/CIO see launch, cancel, and finalization controls. Admin does not gain investment-work permissions by virtue of operational access.

## Acceptance

Testing Library/MSW coverage includes launch, member restriction, active polling, source-unavailable output, failure state, keyboard paths, mobile layout, and reduced motion.
