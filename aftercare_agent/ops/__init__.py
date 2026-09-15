"""Operational tooling: backup, restore drills, retention and reconciliation.

These modules copy and compare PostgreSQL data.  They do not run the Agent,
call a model or reach a supplier, and nothing in the request path imports them.
"""
