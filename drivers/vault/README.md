# HashiCorp Vault driver (metadata only)

Package driver (`drivers/vault/`): CLI, watcher script, skill template. Off by default; each enabled instance gets a skill
`vault-<instance>`. **Never reads secret values**: the client refuses `/data/`, `/creds/`, `/decrypt/`, `/sign/`, `/read`,
unwrap, `sys/raw`, cubbyhole and every endpoint not on a metadata allow-list, before any request is sent. No writes.

## Configure (`~/.dop-agent/config.yaml`)
```yaml
integrations:
  my-vault:
    driver: vault
    enabled: true
    base_url: https://vault.example.com:8200
    auth: { type: token, token: ${env:VAULT_TOKEN} }   # or ${file:/path} / ${keychain:svc}
    options: { namespace: team-a, tls_verify: "true", title: "Prod Vault" }   # title optional
```
Create the token with a read-only policy, e.g. `read,list` on `sys/health`, `sys/seal-status`, `sys/auth`, `sys/mounts`,
`sys/audit`, `sys/policies/acl/*`, `sys/capabilities-self`, `+/metadata/*`, `sys/leases/lookup*` (and no access to `*/data/*`).
Use a renewable periodic token for long-running watchers. Settings -> Integrations -> Test runs `cli.py test`.

Limit: `raw GET <x>/metadata[/..]` cannot tell a KV v1 secret named `metadata` from KV v2 metadata; use `ls`/`meta` (which check the mount version).

## CLI
`python cli/cli.py --instance NAME <command>`: `test`, `fetch`, `health`, `seal-status`, `auth-methods`, `mounts`, `policies`, `ls`,
`meta`, `whoami`, `caps`, `leases [prefix] [--within H] [--limit N] [--count]`, `audit`, `raw GET|LIST`. The token value is never printed (`lookup-self` output is scrubbed).

## Watchers (`watch/vault_watch.py`)
`vault-health` (sealed/unhealthy/unreachable + recovery), `vault-token-expiry` (`--days`, default 7), `vault-lease-expiry` (`--prefix` required, `--hours`, default 24).
State holds only states/accessors/lease ids.

Tests: `tests/test_vault_driver.py` (fake backend, no real Vault needed).
