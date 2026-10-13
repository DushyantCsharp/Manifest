# Reviewer UI

Next.js app for the human review queue. Not scaffolded yet; the contracts it will consume exist:

- GraphQL: `contracts/schema.graphql` (review queue connection, field approval and correction).
- REST: `src/generated/rest.d.ts`, generated from `contracts/openapi.json` by openapi-typescript.
  Regenerate with `make contracts` from the repo root; never edit it by hand.
