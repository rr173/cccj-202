"""Typed event model + profile validation over expanded JSON-LD.

Supported profile (anything else is explicit, never silently dropped):
  * ObjectEvent / action OBSERVE
  * AggregationEvent / action ADD or DELETE, non-empty declared members
  * TransformationEvent with non-empty inputs and outputs
Every event must carry eventID, eventTime (with UTC offset), recordTime,
a non-empty sourceList and destinationList, and EPC or class quantity
members as required by its type.  Quantities are exact rationals; uom
must be declared in the unit table.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from fractions import Fraction

from .units import UnitTable, parse_quantity, fmt_fraction

NS = "https://ns.gs1.org/epcis/"

T_OBJECT = NS + "ObjectEvent"
T_AGGREGATION = NS + "AggregationEvent"
T_TRANSFORMATION = NS + "TransformationEvent"

P_EVENT_ID = NS + "eventID"
P_EVENT_TIME = NS + "eventTime"
P_RECORD_TIME = NS + "recordTime"
P_ACTION = NS + "action"
P_PARENT_ID = NS + "parentID"
P_EPC_LIST = NS + "epcList"
P_CHILD_EPCS = NS + "childEPCs"
P_QUANTITY_LIST = NS + "quantityList"
P_CHILD_QUANTITY_LIST = NS + "childQuantityList"
P_INPUT_EPC_LIST = NS + "inputEPCList"
P_INPUT_QUANTITY_LIST = NS + "inputQuantityList"
P_OUTPUT_EPC_LIST = NS + "outputEPCList"
P_OUTPUT_QUANTITY_LIST = NS + "outputQuantityList"
P_EPC_CLASS = NS + "epcClass"
P_QUANTITY = NS + "quantity"
P_UOM = NS + "uom"
P_SOURCE_LIST = NS + "sourceList"
P_DESTINATION_LIST = NS + "destinationList"
P_SOURCE = NS + "source"
P_DESTINATION = NS + "destination"

SUPPORTED_TYPES = {T_OBJECT, T_AGGREGATION, T_TRANSFORMATION}

# event statuses
OK = "ok"
UNSUPPORTED = "unsupported"
INVALID = "invalid"
UNINTERPRETABLE = "uninterpretable"


@dataclass
class QuantityDecl:
    epc_class: str
    quantity: Fraction
    uom: str
    base: Fraction
    base_unit: str
    dimension: str
    declared: object = None  # original declared JSON value, kept verbatim

    def to_json(self):
        return {
            "class": self.epc_class,
            "quantity": fmt_fraction(self.quantity),
            "declared": self.declared,
            "uom": self.uom,
            "base_quantity": fmt_fraction(self.base),
            "base_unit": self.base_unit,
            "dimension": self.dimension,
        }


@dataclass
class Item:
    """A transformation input/output item: named EPC or quantified class.

    list_name/index record the item's coordinates in the original event
    (e.g. ("inputQuantityList", 0)) so manifest lineage links can point
    at exact endpoints.
    """
    kind: str  # "epc" | "class"
    id: str
    quantity: QuantityDecl | None = None
    list_name: str | None = None
    index: int | None = None

    def to_json(self):
        if self.kind == "epc":
            out = {"kind": "epc", "id": self.id}
        else:
            out = {"kind": "class", "id": self.id}
            out.update(self.quantity.to_json())
        out["list"] = self.list_name
        out["index"] = self.index
        return out


@dataclass
class ParsedEvent:
    event_id: str
    etype: str  # "ObjectEvent" | "AggregationEvent" | "TransformationEvent"
    action: str | None
    event_time: str          # ISO, normalised to UTC
    event_us: int            # microseconds since epoch (sort/group key)
    record_time: str
    parent_id: str | None = None
    child_epcs: list = field(default_factory=list)
    child_quantities: list = field(default_factory=list)  # QuantityDecl
    epc_list: list = field(default_factory=list)
    quantity_list: list = field(default_factory=list)     # QuantityDecl
    inputs: list = field(default_factory=list)            # Item
    outputs: list = field(default_factory=list)           # Item
    sources: list = field(default_factory=list)
    destinations: list = field(default_factory=list)

    def mentioned_epcs(self):
        out = list(self.child_epcs) + list(self.epc_list)
        if self.parent_id:
            out.append(self.parent_id)
        for it in self.inputs + self.outputs:
            if it.kind == "epc":
                out.append(it.id)
        return out

    def mentioned_classes(self):
        out = [q.epc_class for q in self.child_quantities + self.quantity_list]
        for it in self.inputs + self.outputs:
            if it.kind == "class":
                out.append(it.id)
        return out

    def to_payload(self):
        """Normalised JSON payload persisted for replay (exact strings)."""
        return {
            "event_id": self.event_id,
            "etype": self.etype,
            "action": self.action,
            "event_time": self.event_time,
            "event_us": self.event_us,
            "record_time": self.record_time,
            "parent_id": self.parent_id,
            "child_epcs": list(self.child_epcs),
            "child_quantities": [q.to_json() for q in self.child_quantities],
            "epc_list": list(self.epc_list),
            "quantity_list": [q.to_json() for q in self.quantity_list],
            "inputs": [i.to_json() for i in self.inputs],
            "outputs": [i.to_json() for i in self.outputs],
            "sources": list(self.sources),
            "destinations": list(self.destinations),
        }


class ProfileError(ValueError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class UnsupportedError(ValueError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------- helpers

def _prop(node, iri):
    return node.get(iri, [])


def _single_value(node, iri):
    vals = _prop(node, iri)
    if len(vals) != 1:
        return None
    v = vals[0]
    return v.get("@value") if isinstance(v, dict) else None


def _single_id(node, iri):
    vals = _prop(node, iri)
    if len(vals) != 1:
        return None
    v = vals[0]
    return v.get("@id") if isinstance(v, dict) else None


def _list_items(node, iri):
    vals = _prop(node, iri)
    if not vals:
        return []
    if len(vals) == 1 and isinstance(vals[0], dict) and "@list" in vals[0]:
        return list(vals[0]["@list"])
    return list(vals)


def _id_list(node, iri):
    out = []
    for item in _list_items(node, iri):
        if isinstance(item, dict) and "@id" in item:
            out.append(item["@id"])
    return out


def _parse_time(text, what):
    if not isinstance(text, str):
        raise ProfileError(f"{what}: missing or not a string")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ProfileError(f"{what}: not ISO 8601: {text!r}") from exc
    if dt.tzinfo is None:
        raise ProfileError(f"{what}: must carry a UTC offset: {text!r}")
    return dt


def _to_utc(dt):
    return dt.astimezone(timezone.utc)


def _epoch_us(dt):
    return int(dt.timestamp() * 1_000_000)


def _quantity_elements(node, iri, units: UnitTable, what):
    decls = []
    for item in _list_items(node, iri):
        if not isinstance(item, dict):
            raise ProfileError(f"{what}: quantity element must be an object")
        epc_class = _single_id(item, P_EPC_CLASS)
        if not epc_class:
            raise ProfileError(f"{what}: quantity element missing epcClass")
        raw_q = _single_value(item, P_QUANTITY)
        if raw_q is None:
            raise ProfileError(f"{what}: quantity element missing quantity")
        try:
            qty = parse_quantity(raw_q, what=f"{what} quantity")
        except ValueError as exc:
            raise ProfileError(str(exc)) from exc
        if qty < 0:
            raise ProfileError(f"{what}: negative quantity declared")
        uom = _single_value(item, P_UOM)
        if not isinstance(uom, str) or not uom:
            raise ProfileError(f"{what}: quantity element missing uom")
        if not units.has(uom):
            raise ProfileError(f"{what}: uom {uom!r} not declared in unit table")
        base, base_unit, dim = units.to_base(qty, uom)
        decls.append(QuantityDecl(epc_class, qty, uom, base, base_unit, dim,
                                  declared=raw_q))
    return decls


def _endpoint_list(node, iri, inner_iri, what):
    out = []
    vals = _list_items(node, iri)
    for item in vals:
        if isinstance(item, dict):
            ref = _single_id(item, inner_iri)
            if ref:
                out.append(ref)
    return out


# ---------------------------------------------------------------- main entry

def parse_event(expanded_nodes, units: UnitTable):
    """Parse one expanded event node list -> ParsedEvent.

    Raises UnsupportedError / ProfileError; caller maps to statuses.
    """
    if not isinstance(expanded_nodes, list) or len(expanded_nodes) != 1:
        raise ProfileError("event must expand to exactly one node")
    node = expanded_nodes[0]
    if not isinstance(node, dict):
        raise ProfileError("event node must be an object")

    types = [t for t in node.get("@type", [])]
    known = [t for t in types if t in SUPPORTED_TYPES]
    if not known:
        raise UnsupportedError(
            "unsupported event type: " + (", ".join(types) if types else "<none>"))
    if len(known) > 1:
        raise ProfileError("event carries multiple supported types")
    etype_iri = known[0]
    etype = etype_iri[len(NS):]

    action = _single_value(node, P_ACTION)
    if etype == "ObjectEvent":
        if action != "OBSERVE":
            raise UnsupportedError(f"ObjectEvent action {action!r} not supported")
    elif etype == "AggregationEvent":
        if action not in ("ADD", "DELETE"):
            raise UnsupportedError(f"AggregationEvent action {action!r} not supported")

    event_id = _single_value(node, P_EVENT_ID)
    if not isinstance(event_id, str) or not event_id:
        raise ProfileError("missing eventID")

    event_dt = _parse_time(_single_value(node, P_EVENT_TIME), "eventTime")
    record_dt = _parse_time(_single_value(node, P_RECORD_TIME), "recordTime")
    event_utc = _to_utc(event_dt)

    sources = _endpoint_list(node, P_SOURCE_LIST, P_SOURCE, "sourceList")
    destinations = _endpoint_list(node, P_DESTINATION_LIST, P_DESTINATION, "destinationList")
    if not sources:
        raise ProfileError("missing or empty sourceList")
    if not destinations:
        raise ProfileError("missing or empty destinationList")

    ev = ParsedEvent(
        event_id=event_id,
        etype=etype,
        action=action,
        event_time=event_utc.isoformat(),
        event_us=_epoch_us(event_utc),
        record_time=_to_utc(record_dt).isoformat(),
        sources=sources,
        destinations=destinations,
    )

    if etype == "ObjectEvent":
        ev.epc_list = _id_list(node, P_EPC_LIST)
        ev.quantity_list = _quantity_elements(node, P_QUANTITY_LIST, units, "quantityList")
        if not ev.epc_list and not ev.quantity_list:
            raise ProfileError("ObjectEvent declares neither EPCs nor class quantities")

    elif etype == "AggregationEvent":
        parent = _single_id(node, P_PARENT_ID)
        if not parent:
            raise ProfileError("AggregationEvent missing parentID")
        ev.parent_id = parent
        ev.child_epcs = _id_list(node, P_CHILD_EPCS)
        ev.child_quantities = _quantity_elements(
            node, P_CHILD_QUANTITY_LIST, units, "childQuantityList")
        if not ev.child_epcs and not ev.child_quantities:
            # An empty-member DELETE must not be read as a no-op; the whole
            # shape is outside this profile.  Same for an empty ADD.
            raise UnsupportedError(
                f"AggregationEvent {action} with empty declared members")

    elif etype == "TransformationEvent":
        for i, epc in enumerate(_id_list(node, P_INPUT_EPC_LIST)):
            ev.inputs.append(Item("epc", epc, list_name="inputEPCList", index=i))
        for i, q in enumerate(_quantity_elements(node, P_INPUT_QUANTITY_LIST, units, "inputQuantityList")):
            ev.inputs.append(Item("class", q.epc_class, q, list_name="inputQuantityList", index=i))
        for i, epc in enumerate(_id_list(node, P_OUTPUT_EPC_LIST)):
            ev.outputs.append(Item("epc", epc, list_name="outputEPCList", index=i))
        for i, q in enumerate(_quantity_elements(node, P_OUTPUT_QUANTITY_LIST, units, "outputQuantityList")):
            ev.outputs.append(Item("class", q.epc_class, q, list_name="outputQuantityList", index=i))
        if not ev.inputs:
            raise ProfileError("TransformationEvent declares no inputs")
        if not ev.outputs:
            raise ProfileError("TransformationEvent declares no outputs")

    return ev
