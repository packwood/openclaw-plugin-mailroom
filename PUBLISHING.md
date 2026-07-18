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
6. Create a signed Git tag and GitHub release.
7. Validate the ClawHub package with a current ClawHub CLI:

   ```bash
   clawhub package validate .
   clawhub package publish . --family code-plugin --dry-run
   ```

8. Publish only after the dry run, repository visibility, issue tracker,
   security-advisory channel, and install documentation have all been checked.

New ClawHub releases may remain hidden until review and verification complete.
