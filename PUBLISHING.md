# Publishing

Mailroom should be published only from a repository whose complete Git history
is safe for public disclosure.

## Release checklist

1. Scan the working tree and every reachable Git object for secrets, private
   mailbox addresses, chat IDs, connection IDs, signatures, ledgers, and
   production rulepacks.
2. Run `npm ci && npm run test:all && npm run verify:package`.
3. Install the resulting tarball into a clean consumer directory and import the
   compiled runtime with its OpenClaw peer dependency installed.
4. Run `openclaw plugins doctor` and inspect the loaded runtime.
5. Update `CHANGELOG.md` and the package version.
6. Merge the release commit to `main`, then create and push an annotated
   `v<package-version>` tag. The tag must match `package.json` exactly.
7. Validate the ClawHub package with a current ClawHub CLI:

   ```bash
   clawhub package validate .
   clawhub package publish . --family code-plugin --dry-run
   ```

8. Push the tag. The protected `release.yml` workflow reruns verification,
   publishes through ClawHub trusted publishing, and creates the GitHub release.

The initial release must be published once with an authenticated ClawHub CLI.
Afterward, configure the repository workflow as the package's trusted publisher:

```bash
clawhub package trusted-publisher set @packwood/mailroom \
  --repository packwood/openclaw-plugin-mailroom \
  --workflow-filename release.yml
```

Manual publication after trusted publishing is configured is reserved for
recovery and requires an explicit override reason.

New ClawHub releases may remain hidden until review and verification complete.
