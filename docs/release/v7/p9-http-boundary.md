# v7 HTTP deployment boundary

Development checkpoint; remote deployment certification remains open.

Run desktop HTTP on `127.0.0.1`. The server admits its exact listening Host
authority and `localhost` at the same port. Public deployments must configure
`DAEM0NMCP_ALLOWED_HOSTS` as a comma-separated list of exact authorities, for
example `memory.example.com,memory.example.com:443`. Wildcards, URL paths,
credentials and ambiguous duplicate Host headers are rejected. Non-loopback
listeners require explicit hosts and production JWT verification.

Configure browser origins separately with `DAEM0NMCP_ALLOWED_ORIGINS`, for
example `https://app.example.com`. Linking a workspace, supplying a permitted
Host/Origin, or forwarding a client address does not grant workspace access.

Terminate public TLS at the reverse proxy. Keep the application listener on
loopback on the same host; if the proxy is elsewhere, firewall the backend to
that proxy and protect the internal connection. Forward Authorization unchanged
and set Host to the configured public authority. Never expose an unencrypted
backend directly to public clients. Configure the supported JWT verifier with
its HTTPS JWKS URI, expected issuer and audience.

Forwarded headers are ignored by default, including when the ambient Uvicorn
`FORWARDED_ALLOW_IPS` variable is permissive. If the proxy needs to supply the
public request scheme/address, explicitly set `DAEM0NMCP_TRUSTED_PROXY_IPS` to
its IP addresses, for example `127.0.0.1,::1`. Hostnames, network ranges and `*`
are rejected. The proxy must overwrite forwarding headers from public requests.
JWT identity remains the authorization input.

At the proxy, enforce header/body read deadlines, connection limits and a 2 MiB
request-body ceiling. Disable buffering for MCP streaming responses and retain
the `/mcp` path. Tool execution deadlines and background task admission remain
separate from proxy timeouts.

The application rejects duplicate JSON keys, non-finite numeric values, bodies
above 2 MiB, and nesting beyond 64 containers before SDK dispatch. The same JSON
rules apply to stdio. Host validation precedes request-body collection.

## Development verification

The focused HTTP/launcher/process run passed 36 tests and 27 subtests on Python
3.12 (`.tmp/p9-host-tests.log`). It includes actual HTTP rejection of an invalid
Host, duplicate Host headers, an invalid Origin and duplicate JSON, followed by
a successful MCP initialization and scoped briefing. Unit cases exercise exact
proxy IP configuration and rejection before body reads.

This does not certify an external HTTPS reverse proxy, the deployment firewall,
the full authorization/quota matrix, or real sandbox isolation. Those remain
release gates and require independent review and final-commit evidence.
