# secrets toolset

Secret tooling exposes only configured secret references so a platform operator can verify that an
integration is wired without retrieving its secret material. Read the tool schema for response
fields and filtering.

- `secrets.list` lists configured secret references for a `platform_operator`; it never returns
  resolved secret values.

A missing reference is configuration evidence, not a reason to place a credential in a tool call,
document, or log. Correct the secret source through the platform's approved configuration path.
