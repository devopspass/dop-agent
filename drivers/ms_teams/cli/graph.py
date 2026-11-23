"""Microsoft Graph helpers.

Tokens come from the Teams-tab CDP cache (auth.tab_token).
"""
import urllib.error
import urllib.request

import auth
import http_util

BASE = "https://graph.microsoft.com/v1.0"


def _graph_token() -> str:
    return auth.tab_token(auth.RESOURCE_GRAPH)


def _calendar_token() -> str:
    return auth.tab_token(auth.RESOURCE_GRAPH, "Calendars")


def user_info(email: str) -> dict:
    status, data = http_util.call(
        _graph_token(), "GET",
        f"{BASE}/users/{email}?$select=displayName,mail,userPrincipalName,jobTitle",
    )
    if status != 200:
        raise RuntimeError(f"user_info failed: {status} {data}")
    return data


def user_photo(email: str) -> bytes:
    token = _graph_token()
    req = urllib.request.Request(
        f"{BASE}/users/{email}/photo/$value", headers={"Authorization": "Bearer " + token}
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        return resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"user_photo failed: {e.code}")


def joined_teams() -> list:
    status, data = http_util.call(_graph_token(), "GET", f"{BASE}/me/joinedTeams")
    if status != 200:
        raise RuntimeError(f"joined_teams failed: {status} {data}")
    return data.get("value", [])


def calendar_view(start_iso: str, end_iso: str, top: int = 100) -> list:
    token = _calendar_token()
    url = (
        f"{BASE}/me/calendarView?startDateTime={start_iso}&endDateTime={end_iso}"
        f"&$top={top}&$select=subject,isOnlineMeeting,onlineMeeting,organizer,start,end"
        "&$orderby=start/dateTime"
    )
    status, data = http_util.call(token, "GET", url)
    if status != 200:
        raise RuntimeError(f"calendar_view failed: {status} {data}")
    return data.get("value", [])


def create_event(subject: str, start_iso: str, end_iso: str, timezone: str = "UTC",
                  teams_meeting: bool = False) -> dict:
    token = _calendar_token()
    body = {
        "subject": subject,
        "start": {"dateTime": start_iso, "timeZone": timezone},
        "end": {"dateTime": end_iso, "timeZone": timezone},
    }
    if teams_meeting:
        body["isOnlineMeeting"] = True
        body["onlineMeetingProvider"] = "teamsForBusiness"
    status, data = http_util.call(token, "POST", f"{BASE}/me/events", body)
    if status not in (200, 201):
        raise RuntimeError(f"create_event failed: {status} {data}")
    return data


def delete_event(event_id: str) -> None:
    token = _calendar_token()
    status, data = http_util.call(token, "DELETE", f"{BASE}/me/events/{event_id}")
    if status != 204:
        raise RuntimeError(f"delete_event failed: {status} {data}")
