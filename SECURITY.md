# Security status

This is a public research-software snapshot with a local synthetic demo, not a security-reviewed production service.

- Credentials are generated locally or supplied through the environment. `.env.example` contains invalid placeholders; configuration must reject them. Never commit `.env`, database backups, student records or real timetables.
- The public Compose file publishes its web service only on `127.0.0.1`. It does not provide TLS. Do not treat the demo as a public deployment template.
- Role checks, scope assignments and jurisdiction enforcement are implemented. Coverage is incomplete: the source version documents whole-period schedule responses that are not fully narrowed to the caller's scope. Other domain-specific authorization assumptions still require review before confidential data is loaded.
- Human-only institutional decisions, approval, and applying a schedule change are distinct in the model. Tests of those invariants do not constitute a formal access-control audit.
- Test fixtures create and drop databases and clear test Redis state. Supply isolated `TEST_DATABASE_URL` and `TEST_REDIS_URL`; never reuse operational services.
- Unsupported imported constraints must be reviewed. A solver's success does not mean that an unimplemented policy was enforced.

The public edition excludes operational notes containing account details and excludes the original deployment history. No passwords or accounts from an existing deployment were reused or tested to produce this edition.

If a report contains confidential data, use the contact route on [andresrubiop.com](https://www.andresrubiop.com) instead of opening a public issue.
