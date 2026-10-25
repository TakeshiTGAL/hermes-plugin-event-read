---
name: caldav-calendar
description: "CalDAV calendar events and appointments. Before web_search, use tool_search to find the calendar tools and tool_call to run them."
---

For this kind of request, use tool_search to find this plugin's tools before web_search, then call them with tool_call. Write tool_search queries in English (for example: "calendar events").

First call, no approval: `list_events` with no arguments. That reads the next 7 days. Do not pass start or end unless the user named two UTC times. Do not create a cron job for this ask.

If the reply is not ok, do what next_step says once. Do not ask the user to rephrase, and do not invent a uid.

`read_event` takes one uid copied from `list_events` or from the user's words. Neither tool asks for approval. Answer in the language the user used.

Japanese requests use the same tools. Search in English, then answer in Japanese:

- Next week on the calendar. 「来週の予定」 → tool_search "calendar events" → `list_events` with no arguments <!-- ja-alias -->
- Today on the calendar. 「今日の予定」 → tool_search "calendar events" → `list_events` with no arguments <!-- ja-alias -->
- Tell me the plans. 「予定を教えて」 → tool_search "calendar events" → `list_events` with no arguments <!-- ja-alias -->
- Read this one event. 「この予定の中身」 → tool_search "calendar event" → `list_events` with no arguments, then pass one uid to `read_event` <!-- ja-alias -->
- Read the meet one event. 「meet-1 の中身」 → tool_search "calendar event" → `read_event` {"uid": "meet-1"} <!-- ja-alias -->
