# GitHub Webhook Plugin

Receive GitHub webhooks and automatically notify Evonic agents to perform tasks based on repository events.

## Overview

This plugin exposes a webhook endpoint (`POST /webhook/github_webhook`) that GitHub can send events to. When an event is received, the plugin:

1. Verifies the request signature using HMAC-SHA256
2. Identifies the event type from the `X-GitHub-Event` header
3. Extracts relevant data from the payload
4. Renders a customizable prompt template with event data
5. Sends the rendered message to a designated Evonic agent

Each event type can be configured independently with its own target agent, prompt template, and input filters. If an agent ID is left empty, that event type is ignored.

## Supported Events

### Release (`release`)

Triggered when a new release is **published** on GitHub.

| Variable       | Description                      |
|---------------|----------------------------------|
| `{{tag_name}}` | Git tag name (e.g. `v1.2.0`)     |
| `{{name}}`     | Release title                    |
| `{{body}}`     | Full release notes (markdown)    |
| `{{html_url}}` | URL to the release on GitHub     |
| `{{repository}}` | Repository full name (e.g. `owner/repo`) |
| `{{action}}`   | Event action (`published`)       |

**Default behavior:** Notifies agent `adit` to create documentation for the release.

---

### Pull Request (`pull_request`)

Triggered when a pull request is **opened**, **reopened**, **closed**, or **edited**.

| Variable       | Description                          |
|---------------|--------------------------------------|
| `{{title}}`    | PR title                             |
| `{{body}}`     | PR description (markdown)            |
| `{{html_url}}` | URL to the PR on GitHub              |
| `{{state}}`    | PR state (`open` or `closed`)        |
| `{{repository}}` | Repository full name             |
| `{{action}}`   | Event action (`opened`, `reopened`, `closed`, `edited`) |

---

### Issues (`issues`)

Triggered when an issue is **opened**, **reopened**, **closed**, or **edited**.

| Variable       | Description                          |
|---------------|--------------------------------------|
| `{{title}}`    | Issue title                          |
| `{{body}}`     | Issue description (markdown)         |
| `{{html_url}}` | URL to the issue on GitHub           |
| `{{state}}`    | Issue state (`open` or `closed`)     |
| `{{repository}}` | Repository full name             |
| `{{action}}`   | Event action (`opened`, `reopened`, `closed`, `edited`) |

---

### Push (`push`)

Triggered when code is **pushed** to any branch in the repository.

| Variable          | Description                                  |
|-------------------|----------------------------------------------|
| `{{ref}}`          | Git ref (e.g. `refs/heads/main`)             |
| `{{commits_count}}` | Number of commits in the push               |
| `{{repository}}`   | Repository full name                         |
| `{{compare}}`      | GitHub compare URL for the pushed commits    |

---

### Workflow Run (`workflow_run`)

Triggered when a GitHub Actions workflow run completes (any status/conclusion).

| Variable            | Description                                      |
|---------------------|--------------------------------------------------|
| `{{workflow_name}}`  | Name of the workflow (e.g. `CI`)                 |
| `{{status}}`         | Run status (`completed`, `in_progress`, `queued`) |
| `{{conclusion}}`     | Run conclusion (`success`, `failure`, `neutral`, `cancelled`, `skipped`, `timed_out`) |
| `{{head_branch}}`    | Branch the workflow ran on                       |
| `{{head_sha}}`       | Commit SHA the workflow ran on                   |
| `{{html_url}}`       | URL to the workflow run on GitHub                |
| `{{repository}}`     | Repository full name                             |

---

### Check Suite (`check_suite`)

Triggered when a check suite is completed for a commit.

| Variable          | Description                                      |
|-------------------|--------------------------------------------------|
| `{{app_name}}`     | Name of the GitHub App that created the suite    |
| `{{status}}`       | Suite status (`completed`, `in_progress`, `queued`) |
| `{{conclusion}}`   | Suite conclusion (`success`, `failure`, `neutral`, `cancelled`, `skipped`, `timed_out`) |
| `{{head_branch}}`  | Branch the check suite ran on                    |
| `{{head_sha}}`     | Commit SHA the check suite ran on                |
| `{{html_url}}`     | URL to the check suite on GitHub                 |
| `{{repository}}`   | Repository full name                             |

---

### Commit Status (`status`)

Triggered when the status of a commit changes (e.g. a CI build passes or fails).

| Variable          | Description                                      |
|-------------------|--------------------------------------------------|
| `{{sha}}`          | Commit SHA                                       |
| `{{state}}`        | Status state (`success`, `failure`, `error`, `pending`) |
| `{{context}}`      | Status context (e.g. `ci/github-actions`)        |
| `{{description}}`  | Status description                               |
| `{{target_url}}`   | URL to the detailed status (CI build page)       |
| `{{repository}}`   | Repository full name                             |

## Input Filters

By default, every matching event triggers the agent. You can add **input filters** to control exactly which webhook payloads get through. Each event type has its own filter field (JSON textarea).

### Filter Format

Filters are a JSON array of objects. Each object specifies a field path, match type, and value:

```json
[
  {"field": "<dot-notation-path>", "match": "equals" | "regex", "value": "<match-value>"}
]
```

- **field**: A dot-notation path into the webhook payload (e.g. `pull_request.state`, `repository.full_name`)
- **match**: `equals` for exact string match, or `regex` for pattern matching
- **value**: The value or regex pattern to match against

All filters for an event type are **ANDed** — the event only fires if ALL filters pass. An empty filter config means "fire always" (current behavior).

### Filter Examples

#### Only trigger on new (opened) PRs

```json
[{"field": "action", "match": "equals", "value": "opened"}]
```

#### Only trigger on pushes to the main branch

```json
[{"field": "ref", "match": "regex", "value": "^refs/heads/main$"}]
```

#### Only trigger on open issues in a specific organization

```json
[
  {"field": "issue.state", "match": "equals", "value": "open"},
  {"field": "repository.full_name", "match": "regex", "value": "^my-org/"}
]
```

#### Only trigger on non-prerelease releases

```json
[{"field": "release.prerelease", "match": "equals", "value": "False"}]
```

#### Only trigger on failed workflow runs

```json
[{"field": "workflow_run.conclusion", "match": "equals", "value": "failure"}]
```

#### Only trigger on failed check suites

```json
[{"field": "check_suite.conclusion", "match": "equals", "value": "failure"}]
```

#### Only trigger on failed commit statuses

```json
[{"field": "state", "match": "equals", "value": "failure"}]
```

## Setup Guide

### Step 1: Generate a Webhook Secret

Generate a random secret string. This will be used to verify that incoming requests are genuinely from GitHub.

```bash
openssl rand -hex 32
```

Copy the output — you will need it for both GitHub and Evonic configuration.

### Step 2: Configure the Plugin in Evonic

1. Open Evonic and navigate to **Plugins** → **GitHub Webhook** → **Settings**
2. In the **General** section:
   - **Webhook Secret**: Paste the secret generated in Step 1
3. Configure each event section below:
   - **Agent ID**: Enter the Evonic agent ID that should receive notifications for this event (e.g. `adit`). Leave empty to ignore the event.
   - **Prompt Template**: Customize the message sent to the agent. Use `{{variable}}` placeholders to insert event data.
   - **Filters (JSON)**: Optional — add JSON filters to control which webhook payloads trigger the agent. See the [Input Filters](#input-filters) section above for format and examples. Leave empty to fire on all matching events.

4. Click **Save** to apply settings.

### Step 3: Determine Your Webhook URL

The webhook endpoint is:

```
https://<your-evonic-domain>/webhook/github_webhook/
```

Make sure to include the **trailing slash**. For example:

```
https://evonic.example.com/webhook/github_webhook/
```

> **Note:** Your Evonic server must be accessible from the internet for GitHub to deliver webhooks.

### Step 4: Configure GitHub Webhook

1. Go to your repository on GitHub
2. Navigate to **Settings** → **Webhooks** → **Add webhook**
3. Fill in the fields:

| Field            | Value                                              |
|------------------|----------------------------------------------------|
| **Payload URL**  | `https://<your-evonic-domain>/webhook/github_webhook/` |
| **Content type** | `application/json`                                 |
| **Secret**       | Paste the same secret from Step 1                  |
| **Which events** | Select **Let me select individual events**         |

4. Check the following events (uncheck any you don't need):
   - **Releases** — triggers on release publish
   - **Pull requests** — triggers on open, close, edit
   - **Issues** — triggers on open, close, edit
   - **Pushes** — triggers on code push
   - **Workflow runs** — triggers on workflow completion
   - **Check suites** — triggers on check suite completion
   - **Statuses** — triggers on commit status changes

5. Ensure **Active** is checked
6. Click **Add webhook**

### Step 5: Verify the Webhook

GitHub will send a `ping` event immediately after creating the webhook. To verify:

1. Go to **Settings** → **Webhooks** in your repository
2. Click on the webhook you just created
3. Check the **Recent Deliveries** section — you should see a `200` response with `{"msg": "pong"}`
4. If the delivery failed, click **Redelivery** to retry

## Testing

You can manually test the webhook by triggering events in your repository:

- **Release test**: Create a test release in your repository
- **PR test**: Open or close a pull request
- **Issue test**: Create or close an issue
- **Push test**: Push a commit to any branch
- **Workflow run test**: Trigger a GitHub Actions workflow
- **Check suite test**: Create a check run via the GitHub API
- **Status test**: Update a commit status via the GitHub API

Check the Evonic server logs to confirm the webhook is processed:

```bash
tail -f /var/log/evonic/evonic.log | grep github_webhook
```

## Security

- **HMAC-SHA256 Verification**: The plugin verifies the `X-Hub-Signature-256` header against the configured `WEBHOOK_SECRET`. Requests with invalid signatures are rejected with HTTP 403.
- **No secret configured**: If `WEBHOOK_SECRET` is empty, signature verification is skipped (logged as a warning). This is useful for local testing but should not be used in production.

## File Structure

```
plugins/github_webhook/
├── __init__.py       # Package marker
├── plugin.json       # Plugin metadata and configurable variables
├── routes.py         # Flask blueprint with webhook endpoint handlers
└── README.md         # This file
```
