# event-read

Read one CalDAV collection you run, with list_events and read_event. A cron job you create can tell you when an event uid turns up in the next week. This plugin starts no program of its own.

Disclosure — sends CALDAV_USER and CALDAV_PASSWORD as Basic to your CALDAV_URL; plain http sends the password unencrypted, and redirects are not followed; it starts no local program; on every surface, including the gateway, with no user list, anyone you allow there uses your password until hermes tools turns it off; you create the watch from the CLI with hermes cron create, with no daily cap, and each run is at least one model turn; under plugins.isolation: host the watch cannot run and posts a notice saying so on every run; on a judged run, problems are sent first, on a new cause, and after 23 hours, or every run when the data directory cannot be written; a call has no overall deadline, and one request gives up after 20 s with no data; it reads unsupported Hermes internals for the cron check and the write check, so an update can stop the watch; the job remains until hermes cron remove; state lives in <HERMES_HOME>/plugin-data/event-read. Not tested against a vendor-hosted calendar.

## What you set

`CALDAV_URL` is one http or https collection URL. It needs a host and a non-empty path. A path of `/` alone is accepted. A username in the URL, a query, a fragment, an empty path, or surrounding spaces is rejected before a connection. `CALDAV_USER` and `CALDAV_PASSWORD` are sent as Basic on every request. A carriage return or line feed in either is rejected before a request. The password is not written into the result, the error text, or a saved file.

Over plain http the password is sent unencrypted. Responses 301, 302, 303, 307, and 308 are not followed, including when they stay on the same host. Authorization is not sent to a second URL. http is not silently rewritten to https.

A request gives up after 20 seconds without data from the server. That is a per-request limit, not a deadline for the whole call. There is no overall deadline and no retry, so one call is one request that either answers or fails.

## What a call returns

`list_events` and `read_event` are the only tools. The toolset is `event_read`. There is no slash command and no plugin CLI. An empty collection and a window that matches nothing are empty results, not errors.

`read_event` looks up one uid with a property filter. It does not GET a path. A CalDAV text match is a substring match, so a server may answer `meet-1` with `meet-1` and `meet-12`; the exact uid is picked out of what came back, and the others are not returned. No event with that uid is `No event with that uid.` and is not an empty success. If the only rows that came back are other uids, that is still not found, and none is chosen.

Each event has a uid, summary, start, end, tzid, and repeats. A missing DTEND leaves end null. A one-day end or a zero-length end is not invented. A DATE value stays a date. A TZID is returned as written. A value that ends in Z has a null tzid. Times are not converted. Attachments are not fetched. No scheduling reply is sent. VTODO and VJOURNAL are not events.

Repeats are not expanded. One recurring event is one row, whose start is the master's DTSTART, and a later occurrence of a saved uid is not announced. Moving or cancelling a single occurrence is normal iCalendar: the calendar object then holds the master plus one component per changed occurrence, all with the same uid and each with its own RECURRENCE-ID (RFC 5545 section 3.8.4.4). Those components are grouped into the one row of their uid, with repeats true. The summary on that row is the master's, not the changed occurrence's. Two copies of the same uid are ambiguous only when the RECURRENCE-ID is also the same, or when both are masters. If a server returns a changed occurrence without its master, that occurrence's own start is used. On 2026-10-10 a local Radicale 3.8.2 server returned a weekly event with a moved occurrence, a weekly event with a cancelled occurrence (EXDATE), and an all-day weekly event, each as one row. This plugin sends `Depth: 1`. It does not say that every server requires that header.

Success is HTTP 207 whose root is a multistatus. Prefixes are ignored, and `text/xml` is accepted. An empty multistatus is an empty list. HTTP 200 is a failure even when the body is a multistatus, and so is HTML, plain text, or a 207 that is not a multistatus. A 401 whose body says forbidden stays a 401, not a 403 or an empty calendar. 400 and 404 are not swapped. If any inner status is not 200, the whole call fails. There is no retry. A timeout is a failure. XML nested more than 100 levels deep is a failure, not a calendar. The message names the cause, for example `The calendar could not be read (http_401). The seen uids were not changed.`

More than 50 events is not the whole list. A ranged list returns the first 50 in document order, with truncated true, and does not record them. A no-argument cron watch that sees more than 50 returns no events, says too many, and does not record them. Its next step is to shrink the collection until that window holds 50 or fewer; the next no-argument list_events then records them. That notice uses the same cadence as a read failure. A response that is too large to parse is an error and does not change the saved uids. An event with no uid fails the whole call and records nothing.

## The watch

Only a no-argument `list_events` on a cron turn that was saved successfully moves the seen uids. The window is this plugin's clock, in UTC, from now truncated to a second through seven days later. The start is included and the end is excluded.

The first success, when there was no announced problem just before it, records the uids and announces nothing. After that, a uid that is in the window and is not in the file is announced as now being in the window. That covers an event created today and an event that already existed outside the window and has moved into it; the plugin cannot tell those apart, and does not call either one newly created. A saved uid is not announced again when the summary or the time changes, when it is deleted, or when the same uid is created again. A deleted uid stays in the file. The saved set is a union, trimmed to the newest 5,000 uids, so a uid dropped by that trim can be announced again. Membership is tested with a set, so a full file does not slow a run. An empty window does not replace saved uids with an empty list. A first success that sees nothing records an empty list and does not announce. After that, a new uid is announced. Both sides empty is `No new event uid.` It is not an error.

A no-argument call that cannot be judged as cron does not send HTTP and does not touch the seen uids. It says it cannot monitor on every run, and it does not write that notice into `notices.json`. The saved uids stay as they are. That includes a separate process, a cron check that cannot be imported, a renamed check, an exception, and a result that is not true or false. A check that returns false is a manual call. A manual no-argument call reads the same seven-day window, notify is false, and it does not create or change the file. A later cron run can still announce a uid the manual call saw. A ranged list and `read_event` read and do not record, including when the run cannot be judged. On a confirmed cron turn those calls say `This call did not update the seen uids.`

A watch that cannot work is announced the first time, again when the cause changes, and again once at least 23 hours have passed since the last announcement of that cause. The 23 hours leave room for a daily job that fires a few seconds early. The same cause inside that window returns the same message with notify false, so a job every few minutes does not send the same line all day. This cadence covers a read failure, more than 50 events in the window, a `CALDAV_URL` that no longer matches the saved one, a state file that cannot be read, and missing settings. Recovery is reported once. A bad argument does not update that memory, and neither does a manual or ranged call.

State is two files in `<HERMES_HOME>/plugin-data/event-read`, each replaced from a temporary file in the same directory. `seen.json` holds the collection URL and the seen uids, and nothing else writes to it, so a problem never rewrites your uids. `notices.json` holds only the kind of the last announced problem and when it was announced. A `seen.json` that is not JSON is left in place and reported on that cadence; delete it yourself and the next success is a first record. An HTTP failure does not save an empty uid list. If the saved URL and `CALDAV_URL` differ, nothing is read, no file is overwritten, and no event is announced. If the write check refuses the path, a watch stops before HTTP, does not write somewhere else, and announces every run. If the data directory is there but cannot be written, the calendar is read, the save fails, and that failure is announced every run, because it has nowhere to remember that it already did.

## Cron

You create the job with Hermes. Hermes does not fire it until a gateway or the cron ticker is running. This plugin has no unschedule command and no default interval. Removing the plugin leaves the job until you run `hermes cron remove <id>`. When the profile is not default, put `-p` before the subcommand, for example `hermes -p work cron remove <id>`. A default profile has no `-p`. This plugin does not define `-p` or `--profile`.

`every 1h` below is an example you can type. It is not a default. `[SILENT]` is the Hermes token that suppresses a cron delivery, so the job only reaches you when the tool asks for it.

```
hermes cron create "every 1h" "Call the list_events tool with no arguments. If notify is true, reply with its message and nothing else. If notify is false, reply with only [SILENT]." --name event-read --deliver local
hermes cron remove <id>
```

```
hermes -p work cron create "every 1h" "Call the list_events tool with no arguments. If notify is true, reply with its message and nothing else. If notify is false, reply with only [SILENT]." --name event-read --deliver local
hermes -p work cron remove <id>
```

`--deliver local` sends you no message: Hermes only saves the run's output under `cron/output/` in the Hermes home, for you to read later. It is the CLI default. To be told without opening that file, create the job with a target you have already configured, such as `--deliver telegram` or `--deliver telegram:<chat_id>`. `deliver` is not a tool argument and is rejected as one. There is no daily cap. Each run is at least one model turn on your bill. A turn that calls the tool makes two or more model requests. `bot-chat` costs another model turn, because the receiving agent reads the output as a message.

The plugin asks for no Hermes approval. The tools are on for every surface, including the gateway, until you turn a surface off with `hermes tools`. There is no separate user list. Anyone you allow on the gateway can call the tools with your password. Under `plugins.isolation: host` the watch cannot run and posts a notice saying so on every run.

The cron check and the write check come from Hermes internals that are not a published interface. The data directory is the path above. If the cron check cannot be used, the watch says it cannot monitor on every run and does not send HTTP. If the write check refuses the path, the watch stops before HTTP and announces every run. If the data directory is there but cannot be written, the calendar is read, the save fails, and that failure is announced every run. None of these report an empty calendar.

Not tested against a vendor-hosted calendar.
