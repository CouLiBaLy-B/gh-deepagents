# Skill: security-checklist

Run through this before every `finalize_patch`. Delegate to the `security`
sub-agent if any item is uncertain.

## Mandatory automated checks
- [ ] `scan_secrets` → 0 findings (gitleaks)
- [ ] `dependency_audit` → 0 high/critical CVEs introduced by this PR
- [ ] `lint_check` → 0 errors (warnings OK if justified)

## Manual review (per language)

### Python
- [ ] No `eval(`, `exec(`, `compile(` on user input
- [ ] No `pickle.loads` / `yaml.load` (use `yaml.safe_load`)
- [ ] `subprocess` calls: `shell=False`, args as list
- [ ] SQL: parameterised queries only — no f-strings inside `execute`
- [ ] Requests: `verify=True`, explicit `timeout=`
- [ ] Secrets read from env/vault, never hardcoded

### JS / TS
- [ ] No `dangerouslySetInnerHTML` on unsanitised input
- [ ] No `eval`, no `new Function`
- [ ] `child_process.exec` → prefer `execFile` with arg array
- [ ] SQL via parameterised query / ORM placeholder
- [ ] CSRF token on state-changing endpoints
- [ ] `npm audit` clean

### Go
- [ ] No `os/exec.Command` with string concatenation
- [ ] HTML rendering via `html/template`, not `text/template`
- [ ] SQL via `?` placeholders, not `fmt.Sprintf`

## Crypto
- [ ] No MD5/SHA-1 for security purposes (use SHA-256+)
- [ ] Random tokens via `secrets.token_urlsafe` / `crypto.randomBytes`
- [ ] Passwords: `argon2`/`bcrypt` with salt, never raw hash

## Network / IO
- [ ] No SSRF: validate URLs, deny private IPs unless explicit
- [ ] File uploads: size cap, content-type check, no path traversal
- [ ] CORS: explicit origin list, no `*` for credentialed requests

If ANY mandatory check fails → STOP, fix it, don't open the PR.
