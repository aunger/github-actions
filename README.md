# github-actions

Composite GitHub Actions for repository upkeep: pull request and issue hygiene, dependency audits and CI test running.
Each action is a directory with its `action.yml`, its script and its tests.

Consumers call an action by path and a full commit SHA:

```yaml
uses: aunger/github-actions/<action>@<40-character-sha>
```
