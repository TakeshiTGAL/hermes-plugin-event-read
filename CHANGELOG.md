# Changelog

## 0.1.3

list_events and read_event do not register a hook and do not ask for approval. The watch still reads the public cron mark, and the seen-uid file is still not an approve line.

## 0.1.2

The README opens with the six steps of the first request: install, three settings, a calendar server that is already running, and `list_events` with no arguments. That read does not ask for approval and does not need a cron job. The skill turns a Japanese request into an English tool_search query and names that call. A missing uid and missing settings name the next call in the reply, in English and then Japanese.

The first request is list_events with no arguments. 日本語: 最初の頼みは引数なしの list_events。失敗した返事に、次の1手を英語の後に日本語で書く。

## 0.1.1

Adds the `caldav-calendar` skill. It tells the model to find the calendar tools with tool_search before web_search. On Hermes 0.21.6 and later, while the plugin is enabled, it adds one line to the skills index of every session. It does not set requires_toolsets, so it stays listed when Tool Search is left at its default, which hides this plugin's tools, and also on a surface where the tools were turned off with hermes tools. On Hermes 0.21.4 and 0.21.5 it is not listed and loads only by its full name.

start and end also accept a UTC date and time with /, ., or - between the date parts and T or a space before the time, such as 2026/10/20 09:00:00Z, as well as fullwidth digits and a lowercase t or z. The time with seconds and the trailing Z are still required, and a date alone is refused. A shifted time, a date that does not exist, and extra arguments are refused with a fix and an example, and the caller's value is not copied into the message.

Each tool description now opens with CalDAV calendar and what the call does. Under Tool Search's default, the first sentence is the part of the description the model sees before it searches.

The README describes the skill, where it is listed, and how to remove it. plugin.yaml carries the same description as the catalog entry.
