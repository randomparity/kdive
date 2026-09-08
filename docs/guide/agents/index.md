# Connect an MCP client

KDIVE serves MCP over streamable HTTP at your deployment's `/mcp` endpoint. Use a client
that can send the OIDC bearer token supplied by your operator in an `Authorization` header.
The client needs network access to that endpoint; a loopback URL refers to the machine
running the client.

## Claude Code

Copy [mcp.json](mcp.json) to `.mcp.json` in your working project and replace the example URL
with your deployment's endpoint. The file contains an environment-variable reference,
not a token:

```json
{
  "mcpServers": {
    "kdive": {
      "type": "http",
      "url": "https://kdive.example.com/mcp",
      "headers": { "Authorization": "Bearer ${KDIVE_TOKEN}" }
    }
  }
}
```

Export the token in the shell that launches the client, then start Claude Code in that
project. Approve the project MCP server when prompted. See
[Claude Code's MCP documentation](https://code.claude.com/docs/en/mcp) for configuration,
environment-variable expansion, and reconnection controls.

```bash
export KDIVE_TOKEN="<oidc-access-token>"
claude
```

When the token expires, obtain another, update the client's environment, and reconnect.
Changing an unrelated shell's environment does not update an already-running client.
A `401` can indicate a missing, expired, or otherwise invalid token; ask the operator to
check issuer, audience, signature, and the configured OIDC endpoints in the
[configuration reference](../reference/config.md#oidc).

Other clients must meet the same transport, authentication, and network requirements.
Claude Desktop's [remote connector setup](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)
is managed by the client vendor and connects from its cloud infrastructure; do not assume
it can reach a local or private KDIVE endpoint. Confirm authentication compatibility with
your operator before choosing that route.

## Local development and demo endpoints

For a local-libvirt installation, follow [local setup](../../../examples/local-libvirt/README.md)
first. Its helper writes the client configuration into the selected kernel tree. From the
KDIVE checkout, mint the demo token with:

```bash
export KDIVE_TOKEN=$(examples/local-libvirt/mint-token.sh)
```

Then launch the client from that configured project. The helper's token is privileged;
keep the mock issuer local and use it only for the development deployment it accompanies.
Use the setup guide for endpoint, project, and token-lifetime overrides.

For a bundled Kubernetes demo, follow the
[Kubernetes runbook](../../operating/runbooks/kubernetes-deploy.md) for the port-forward and
`scripts/demo-token.sh` token helper. Keep the forward running while connected. Demo
credentials and reachability differ from a production deployment; use your operator's
endpoint and identity provider in production.

## Verify access and start work

Read [permissions](../safety-and-rbac.md) and the [agent workflow index](../agent-index.md).
Agent clients normally see a small core catalog: use `tools.search` to discover a tool and
`tools.invoke` to call it. A missing direct tool name does not establish that the operation
is unavailable or that your role is insufficient.

Follow the [core reproduce/verify path](../core-path.md) for the first Investigation,
Allocation, System, and Run. It owns the workflow; this page owns client connection.
