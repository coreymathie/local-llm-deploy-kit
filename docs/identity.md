# Identity, roles and access control

The gateway authenticates two kinds of principal, applications holding API keys and people holding OIDC access
tokens, and maps both to roles. For document Q&A it decides which collections and documents each caller may
read before any passage is loaded. This document describes the identity model, the role set, the access
decision, and the endpoints that manage access. Design rationale: [ADR 0005](adr/0005-oidc-identity-and-roles.md).

## Principals and roles

API keys keep their original meaning. With `GATEWAY_OIDC_ENABLED=true` the gateway also accepts access tokens
(JWTs) from the organization's identity provider on the same `Authorization: Bearer` header, verifies them
locally against the issuer's JWKS, and maps the token's groups to roles.

| Role | Granted to | Allows |
|---|---|---|
| `admin` | admin API keys; IdP groups mapped to `admin` | Everything: keys, documents, audit, model pulls, all collections |
| `user` | other API keys; groups mapped to `user` | Chat, embeddings, model list; collections their access allows |
| `reader:<collection>` / `reader:*` | groups mapped to it | List and ask that collection only (no raw chat) |

```bash
GATEWAY_OIDC_ENABLED=true
GATEWAY_OIDC_ISSUER=https://idp.example.com/realms/corp
GATEWAY_OIDC_AUDIENCE=llm-gateway
GATEWAY_OIDC_GROUP_ROLES='{"llm-admins": ["admin"], "staff": ["user"], "hr-team": ["reader:hr-policies"]}'
```

`GET /v1/me` shows how the gateway sees the caller (kind, label, roles, groups, readable collections).
`GET /admin/users` lists token users with their usage. The admin page accepts an API key or a pasted access
token and shows only the cards the role allows. A valid token whose groups map to no role receives `403`. If
the IdP cannot be reached before its keys were ever fetched, token requests receive `503`.

Token verification is strict: an RS256/ES256 allow-list checked before key lookup, JWKS with rotation, and
`iss`/`aud`/`exp` checks. The full rule set, JWKS caching and outage behaviour are in ADR 0005; all OIDC
settings are in [configuration.md](configuration.md).

## The access decision

Access is decided per caller **before** any passage is loaded (`gateway/rag.py::access_for`):

1. **Collection.** The `admin` role reads everything; `reader:<c>` / `reader:*` roles open a collection.
   Otherwise, if the collection has an access list, the caller must match an entry. If it has none,
   `GATEWAY_COLLECTION_DEFAULT_ACCESS` decides: `open` admits any `user`-role caller (the v0.5 behavior);
   `restricted` admits nobody else (the default in the healthcare and finance profiles).
2. **Document.** A document with its own access list is visible only to callers matching an entry; a reader
   role does not override it. A document without one inherits the collection decision.

Entries are `group:<name>` (IdP groups, or groups given to an API key at creation), `key:<label>` and
`user:<username>`. Hidden documents are filtered in SQL (`gateway/rag.py::candidates`), so they are never scored,
sent to the model, cited, flagged or counted. A collection the caller cannot read answers exactly like one that
does not exist, so the API is not an existence oracle. Every `document_question` audit entry records the
decision (`access.basis`, documents visible and hidden) and the rule that admitted each source; refusals are
logged as `document_question_denied`.

Evidence: `test_acl.py::test_no_cross_group_leakage_in_answers_citations_flags_counts_or_prompts`,
`test_acl.py::test_hidden_passages_are_filtered_in_sql_before_scoring`,
`test_acl.py::test_oidc_users_are_filtered_by_their_token_groups`, and zero ACL leaks in every configuration of
the RAG eval ([benchmarks.md](benchmarks.md#retrieval-quality-measured-golden-set)).

## Managing access

```bash
curl -X POST localhost:8080/admin/keys -H "Authorization: Bearer $ADMIN" -H "Content-Type: application/json" \
  -d '{"label": "hr-bot", "groups": ["hr"]}'
curl -X PUT localhost:8080/admin/collections/handbook/acl -H "Authorization: Bearer $ADMIN" \
  -H "Content-Type: application/json" -d '{"principals": ["group:hr", "group:staff"]}'
```

Document access lists are set with `PUT /v1/collections/{c}/documents/{id}/acl`; the full endpoint list is in
[document-qa.md](document-qa.md#endpoints). Access-list changes are audited before and after, with the actor
(`collection_acl_changed`, `document_acl_changed`).

## Not implemented

SCIM provisioning, an authorization-code + PKCE login in the admin page (it accepts a pasted token), token
introspection for immediate revocation, and access lists synced from source systems (SharePoint, Drive, file
shares). See the README [roadmap](../README.md#roadmap).
