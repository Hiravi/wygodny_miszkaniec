"""Installed OTP stop planning contract and conservative native journey validation."""

from tools.router.normalize import instant, summarize

METHOD = "native-stop-pilot-v2"

LIMIT = 32

FIRST = 5

QUERY = """query($origin:PlanLabeledLocationInput!,$destination:PlanLabeledLocationInput!,
 $dateTime:PlanDateTimeInput!){planConnection(origin:$origin,destination:$destination,
 dateTime:$dateTime,first:5,searchWindow:"PT45M",
 modes:{direct:[WALK],transit:{transit:[{mode:BUS},{mode:TRAM}]}},
 preferences:{street:{walk:{speed:1.3333333333333333}}}){
 searchDateTime routingErrors{code} edges{node{start end legs{
 mode distance serviceDate headsign pickupType dropoffType
 from{name lat lon stop{gtfsId} stopPosition{...on PositionAtStop{position}}
 departure{scheduledTime}}
 to{name lat lon stop{gtfsId} stopPosition{...on PositionAtStop{position}}
 arrival{scheduledTime}}
 route{gtfsId shortName} trip{gtfsId directionId} legGeometry{points}
 stopCalls{stopLocation{...on Stop{gtfsId name lat lon}}
 schedule{time{...on ArrivalDepartureTime{arrival departure}}}}
 }}}}}"""


def verify(node, departure, platform):
    summary = summarize(node, departure)
    last = node["legs"][-1]["to"]
    if not last.get("stop") or last["stop"]["gtfsId"] not in platform["native_ids"]:
        raise ValueError("Effective platform differs from declared physical/namespace equivalents")
    if (
        abs(last["lon"] - platform["longitude"]) > 0.000001
        or abs(last["lat"] - platform["latitude"]) > 0.000001
    ):
        raise ValueError("Effective stop coordinate changed")
    for leg in node["legs"]:
        if leg["mode"] not in {"WALK", "BUS", "TRAM"} or not leg["legGeometry"]["points"]:
            raise ValueError("Unsupported mode or missing native path")
        if leg["mode"] == "WALK":
            continue
        if leg["pickupType"] != "SCHEDULED" or leg["dropoffType"] != "SCHEDULED":
            raise ValueError("Conditional boarding/alighting requires separate policy")
        if not leg["trip"]["gtfsId"] or not leg["serviceDate"] or not leg["route"]["gtfsId"]:
            raise ValueError("Missing scheduled trip provenance")
        start = leg["from"]["stopPosition"]["position"]
        end = leg["to"]["stopPosition"]["position"]
        calls = leg["stopCalls"]
        if end <= start or len(calls) != end - start + 1:
            raise ValueError("Native positions/call sequence disagree")
        if (
            calls[0]["stopLocation"]["gtfsId"] != leg["from"]["stop"]["gtfsId"]
            or calls[-1]["stopLocation"]["gtfsId"] != leg["to"]["stop"]["gtfsId"]
        ):
            raise ValueError("Boarding/alighting stop identities disagree")
        previous = instant(leg["from"]["departure"]["scheduledTime"])
        if instant(calls[0]["schedule"]["time"]["departure"]) != previous:
            raise ValueError("Native boarding departure clock disagrees")
        for call in calls[1:]:
            arrival = instant(call["schedule"]["time"]["arrival"])
            departure_at_stop = instant(call["schedule"]["time"]["departure"])
            if arrival < previous or departure_at_stop < arrival:
                raise ValueError("Non-monotonic native stop clock")
            previous = departure_at_stop
        if instant(calls[-1]["schedule"]["time"]["arrival"]) != instant(
            leg["to"]["arrival"]["scheduledTime"]
        ):
            raise ValueError("Native leg/stop arrival clock disagrees")
    return summary
