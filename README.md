# Odd Lots Transcript Emailer

Small standalone Python project that checks the Odd Lots RSS feed, fetches the published Omny transcript for each new canonical Odd Lots episode, and emails the transcript inline with Resend.

## Local Setup

Use Python 3.11 or newer.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

Create `.env` with:

```text
RESEND_API_KEY=
RESEND_FROM_EMAIL=
RESEND_TO_EMAIL=
```

Run a dry check without fetching transcripts, sending email, or updating state:

```powershell
.\.venv\Scripts\python.exe -m odd_lots run --dry-run
```

Run for real:

```powershell
.\.venv\Scripts\python.exe -m odd_lots run
```

You can also use the installed console command:

```powershell
odd-lots-transcript run --dry-run
```

## GitHub Actions

The workflow in `.github/workflows/odd-lots-emailer.yml` runs once per day at 11:30 UTC and can also be triggered manually. Add these repository secrets before enabling it:

- `RESEND_API_KEY`
- `RESEND_FROM_EMAIL`
- `RESEND_TO_EMAIL`

The workflow commits `sent_episodes.json` back to the repo when new episodes are processed. If a backlog run sends one or more emails and a later episode fails, successful sends are still committed before the workflow reports failure.

## First Run Behavior

The first real run sends only the latest canonical Odd Lots episode currently visible in the RSS feed, then records older visible canonical episodes as `skipped_initial_backfill` so they are not emailed later. Cross-promoted feed items are ignored.

After initialization, the app sends all new eligible episodes oldest-to-newest.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```
