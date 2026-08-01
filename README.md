# RKLB Official Events Calendar

An automatically updated, subscribable calendar containing future Rocket Lab
events confirmed by official sources.

## Live addresses

Website:

`https://infiniteseymour.github.io/rklb-calendar/`

Apple Calendar subscription:

`webcal://infiniteseymour.github.io/rklb-calendar/RKLB_official_events.ics`

HTTPS calendar file:

`https://infiniteseymour.github.io/rklb-calendar/RKLB_official_events.ics`

## Current automation

The GitHub Actions workflow runs daily and:

1. Reads the **Upcoming Events** section of Rocket Lab Investor Relations.
2. Preserves the last successful result if the page is temporarily unavailable.
3. Merges optional manually confirmed events from `data/manual_events.json`.
4. Updates the calendar and website in `docs/`.
5. Commits changes to the repository.

## Official source

- Rocket Lab Investor Relations events:
  `https://investors.rocketlabcorp.com/events-presentations/events`

## Manual events

Only add an event after its date is confirmed by an official source.

Example:

```json
{
  "events": [
    {
      "title": "Example confirmed event",
      "start": "2026-12-01T17:00:00Z",
      "end": "2026-12-01T18:00:00Z",
      "all_day": false,
      "source_url": "https://example.com/official-announcement",
      "category": "Rocket Lab",
      "status": "CONFIRMED"
    }
  ]
}
```

## Local run

```bash
python -m pip install -r requirements.txt
python update_calendar.py
```

## Project stages

- Phase 1: Official Investor Relations upcoming events
- Phase 2: Official launch windows and Neutron milestones
- Phase 3: Optional SEC and additional investor-event monitoring

This is an unofficial community utility. Always verify time-sensitive details
against the linked official source.
