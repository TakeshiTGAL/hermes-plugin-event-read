"""Read one CalDAV collection from Hermes."""


def register(ctx) -> None:
    if __package__:
        from .service import TOOLSET, Deps, list_events, read_event, utc_now
    else:
        from service import TOOLSET, Deps, list_events, read_event, utc_now

    def _deps():
        from agent.secret_scope import get_secret

        return Deps(
            url=get_secret("CALDAV_URL") or "",
            user=get_secret("CALDAV_USER") or "",
            password=get_secret("CALDAV_PASSWORD") or "",
            now=utc_now,
        )

    def _list(args, **_kwargs):
        return list_events(_deps(), args or {})

    def _read(args, **_kwargs):
        return read_event(_deps(), args or {})

    ctx.register_tool(
        name="list_events",
        toolset=TOOLSET,
        schema={
            "name": "list_events",
            "description": (
                "Read VEVENT items from the one CalDAV collection in CALDAV_URL. "
                "One call sends one calendar-query REPORT with Depth 1 and does not follow redirects. "
                "With no arguments the window is the next 7 days on this plugin's UTC clock, "
                "start included and end excluded. Pass start and end together as YYYYMMDDTHHMMSSZ, "
                "or pass no arguments. A ranged read does not change the saved uids. "
                "On a cron run, a no-argument call records uids only after they are saved. "
                "The first success announces nothing. Later runs announce a uid that is in the window "
                "and was not saved before, which includes an event that moved into the window, "
                "and a saved uid is not announced again. When the run cannot be judged as cron, "
                "a no-argument call says it cannot monitor and does not change the saved uids. "
                "A manual call does not change them. This tool does not create, edit, or delete events, "
                "and it does not take a delivery target."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start": {
                        "type": "string",
                        "description": "UTC start YYYYMMDDTHHMMSSZ. Pass it only together with end.",
                    },
                    "end": {
                        "type": "string",
                        "description": "UTC end YYYYMMDDTHHMMSSZ, exclusive. Pass it only together with start.",
                    },
                },
            },
        },
        handler=_list,
    )
    ctx.register_tool(
        name="read_event",
        toolset=TOOLSET,
        schema={
            "name": "read_event",
            "description": (
                "Read one VEVENT by uid from the same CalDAV collection. "
                "The uid is a property filter on one calendar-query REPORT, not a path. "
                "A CalDAV text match is a substring match, so the exact uid is picked out of "
                "whatever came back and a uid that merely contains it is not returned. "
                "No event with that uid is not an empty success. "
                "This read does not change the saved uids and does not create, edit, or delete events."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": {"type": "string", "description": "Event uid. This is not a URL path."},
                },
            },
        },
        handler=_read,
    )
