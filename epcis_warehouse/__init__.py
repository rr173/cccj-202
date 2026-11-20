"""Offline EPCIS 2.0 event processing warehouse.

Traceability over a closed set of JSON-LD events plus a declared unit
table.  Queryable by business time (eventTime) and receiving batch.

Non-goals (by profile): no inference of material consumption from
packaging relations, no recipes, no proportion allocation, no recall
optimisation, no quantity prediction.
"""

__version__ = "0.1.0"
